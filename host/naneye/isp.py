"""A small, fast image signal processor: mosaic in, 8-bit RGB out.

The pipeline is the conventional one, in the order that lets each stage stay cheap:

    black level  ->  white balance  ->  demosaic  ->  colour matrix  ->  gamma

Black level and white balance are a single affine on the mosaic, one multiply-add per
pixel, because both are per-colour constants and the mosaic is a quarter the data of the
demosaiced image. Demosaicing is bilinear. The colour matrix is one 3x3 matrix multiply,
which NumPy hands to BLAS. Gamma is a lookup table, so it costs an array index rather than
a power per pixel.

**Speed is the goal, not fidelity.** This is a viewer for a measurement camera: the raw
10-bit mosaic is what gets recorded and measured, and this code exists so a person can see
what the camera is pointing at at 35 fps. Every stage takes the cheap option, and each one
can be switched off. Budget at 320x320: about 2 ms a frame on this bench, against the
28 ms a frame that 35 fps allows.

What is *not* here, deliberately: lens shading, noise reduction, sharpening, defect
correction, local tone mapping. None of them would make a measurement more true.
"""

from __future__ import annotations

import time

import numpy as np

from . import color

# A colour matrix taken from a colour chart is a calibration this project has not done, so
# the default is honest: none. The alternative is a mild saturation lift, which makes the
# preview look like a camera rather than like a mosaic, and says nothing true about colour.
IDENTITY = np.eye(3, dtype=np.float32)
SATURATION = np.array([[1.35, -0.25, -0.10],
                       [-0.20, 1.45, -0.25],
                       [-0.10, -0.35, 1.45]], np.float32)
MATRICES = {"none": IDENTITY, "saturation": SATURATION}

GAMMAS = {"1.0 (linear)": 1.0, "1.8": 1.8, "2.2": 2.2, "sRGB": None}
WHITE = 1023.0


def gamma_lut(gamma, size: int = 1024, out_max: int = 255) -> np.ndarray:
    """A 0..size-1 -> 0..out_max table. `gamma=None` gives the sRGB transfer curve."""
    x = np.linspace(0.0, 1.0, size, dtype=np.float32)
    if gamma is None:
        y = np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)
    elif gamma == 1.0:
        y = x
    else:
        y = np.power(x, 1.0 / gamma)
    return np.clip(y * out_max + 0.5, 0, out_max).astype(np.uint8)


class Isp:
    """Mosaic to 8-bit RGB, with every stage optional and nothing allocated per frame.

    The tables that depend only on the settings — the gain tile, the demosaic weights, the
    gamma curve — are built when a setting changes, not when a frame arrives.
    """

    def __init__(self, pattern: str = "BGGR", black_level: float = 0.0,
                 gains=(1.0, 1.0, 1.0), matrix: str = "none", gamma=2.2,
                 white: float = WHITE):
        self._pattern = pattern
        self._black = float(black_level)
        self._gains = tuple(float(g) for g in gains)
        self._matrix = matrix
        self._gamma = gamma
        self.white = float(white)
        self.demosaic = True
        self.last_ms = 0.0
        self._shape = None
        self._tile = None          # (2, 2) per-site gain, from the pattern and the gains
        self._ready = False        # the size-dependent tables are built
        self._gain_dirty = True
        self._lut = None
        self._rebuild()

    # --- settings ----------------------------------------------------------------------
    # Each setter invalidates only what it actually changes. Auto white balance writes new
    # gains on every frame, and rebuilding the masks and buffers for that was costing more
    # than the whole rest of the pipeline.
    def _set(self, name, value, gain=False, lut=False, shape=False):
        if getattr(self, name) == value:
            return
        setattr(self, name, value)
        if gain:
            self._gain_dirty = True
        if lut:
            self._lut = gamma_lut(self._gamma, size=1024)
        if shape:
            self._ready = False
        self._ccm = MATRICES.get(self._matrix, IDENTITY)
        self._identity_ccm = np.array_equal(self._ccm, IDENTITY)

    pattern = property(lambda s: s._pattern,
                       lambda s, v: s._set("_pattern", v, gain=True, shape=True))
    black_level = property(lambda s: s._black, lambda s, v: s._set("_black", float(v)))
    gains = property(lambda s: s._gains,
                     lambda s, v: s._set("_gains", tuple(float(g) for g in v), gain=True))
    matrix = property(lambda s: s._matrix, lambda s, v: s._set("_matrix", v))
    gamma = property(lambda s: s._gamma, lambda s, v: s._set("_gamma", v, lut=True))

    def _rebuild(self):
        self._lut = gamma_lut(self._gamma, size=1024)
        self._ccm = MATRICES.get(self._matrix, IDENTITY)
        self._identity_ccm = np.array_equal(self._ccm, IDENTITY)
        self._gain_dirty = True
        self._ready = False

    def _rebuild_gain_map(self):
        """The per-pixel gain: the 2x2 of per-colour gains, tiled over the frame."""
        tile = np.ones((2, 2), np.float32)
        for ch, gain in zip("RGB", self._gains):
            for dy, dx in color.sites(self._pattern)[ch]:
                tile[dy, dx] = gain
        self._tile = tile
        h, w = self._shape
        self._gain_map = np.ascontiguousarray(
            np.tile(tile, (h // 2 + 1, w // 2 + 1))[:h, :w])
        self._gain_dirty = False

    def _prepare(self, shape):
        if self._shape == shape and self._ready:
            return
        self._shape = shape
        m = color.masks(self._pattern, shape)
        self._masks = {k: v.astype(np.float32) for k, v in m.items()}
        # Reciprocal weights, so the hot path multiplies instead of dividing, and buffers
        # that live as long as the frame size does: nothing is allocated per frame.
        self._inv = {
            "R": 1.0 / color.box_full(self._masks["R"]),
            "G": 1.0 / color.box_cross(self._masks["G"]),
            "B": 1.0 / color.box_full(self._masks["B"]),
        }
        self._lin = np.empty(shape, np.float32)
        self._tmp = np.empty(shape, np.float32)
        self._rgb = np.empty(shape + (3,), np.float32)
        self._idx = np.empty(shape + (3,), np.uint16)
        self._ready = True
        self._gain_dirty = True

    # --- the pipeline ------------------------------------------------------------------
    def linear(self, raw: np.ndarray) -> np.ndarray:
        """Everything up to gamma: (h, w, 3) float32 in raw units, for measurement."""
        self._prepare(raw.shape)
        if self._gain_dirty:
            self._rebuild_gain_map()
        lin = self._lin
        np.subtract(raw, self._black, out=lin, dtype=np.float32)
        np.multiply(lin, self._gain_map, out=lin)
        np.clip(lin, 0.0, self.white, out=lin)
        if not self.demosaic:
            return lin
        rgb, tmp = self._rgb, self._tmp
        for i, (ch, box) in enumerate((("R", color.box_full), ("G", color.box_cross),
                                       ("B", color.box_full))):
            np.multiply(lin, self._masks[ch], out=tmp)
            box(tmp, out=rgb[..., i])
            np.multiply(rgb[..., i], self._inv[ch], out=rgb[..., i])
        if not self._identity_ccm:
            flat = rgb.reshape(-1, 3)
            np.matmul(flat, self._ccm.T, out=flat)
            np.clip(rgb, 0.0, self.white, out=rgb)
        return rgb

    def apply_curve(self, lin: np.ndarray) -> np.ndarray:
        """Gamma-curve a linear float32 image (raw units) into 8-bit.

        `lin` is scaled and clipped **in place**: it is meant to be the buffer `linear()`
        just returned, or a stretched copy of it, and copying it again to be polite would
        cost more than the curve does.
        """
        if self.white != 1023.0:
            np.multiply(lin, 1023.0 / self.white, out=lin)
        np.clip(lin, 0.0, 1023.0, out=lin)
        if lin.ndim == 3 and lin.shape == self._idx.shape:
            idx = self._idx                       # the preallocated index buffer
            np.copyto(idx, lin, casting="unsafe")
        else:
            idx = lin.astype(np.uint16)
        return self._lut[idx]

    def process(self, raw: np.ndarray) -> np.ndarray:
        """Mosaic -> 8-bit RGB (or 8-bit grey when demosaicing is off). Times itself."""
        t0 = time.perf_counter()
        out = self.apply_curve(self.linear(raw))
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return out

    # --- helpers -----------------------------------------------------------------------
    def auto_white_balance(self, raw: np.ndarray):
        """Grey-world gains from one frame, black level taken into account first."""
        mean = color.channel_means(raw, self._pattern)
        mean = {k: max(v - self._black, 1e-3) for k, v in mean.items()}
        g = mean["G"]
        return tuple(float(np.clip(g / mean[ch], 0.25, 4.0)) for ch in ("R", "G", "B"))

    def measure_black_level(self, raw: np.ndarray, percentile: float = 1.0) -> float:
        """A stand-in black level from the frame itself.

        A real one comes from a dark frame with the lens covered; this is the darkest
        percentile of whatever is in view, which is only as good as the scene having
        something black in it. It is offered because it is usually better than zero.
        """
        return float(np.percentile(raw, percentile))
