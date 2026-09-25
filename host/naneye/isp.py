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

CALIBRATED = np.array([[1.6052, 0.0689, -0.7889],
                       [-0.6299, 1.9073, -0.6755],
                       [-0.5762, -0.2717, 1.2764]], np.float32)
MATRICES = {"none": IDENTITY, "saturation": SATURATION, "calibrated": CALIBRATED}

GAMMAS = {"1.0 (linear)": 1.0, "1.8": 1.8, "2.2": 2.2, "sRGB": None}
WHITE = 1023.0

# Raw level at or above which a pixel is taken to be clipped. The sensor's own ceiling
# measured 1018-1022 DN on this bench, so a few counts of margin costs nothing real.
SATURATED = 1015.0

# What to do with pixels that reached it.
#   "off"          leave them: the tint is visible, and so is where the clipping is
#   "white"        force them neutral: blunt, and it throws away partial clips
#   "reconstruct"  estimate the channels that ran out from the ones that did not, then
#                  roll what is left smoothly to white
HIGHLIGHT_MODES = ("off", "white", "reconstruct")
CLIP_AT = 0.995     # fraction of a channel's own ceiling at which it counts as clipped
KNEE = 0.88         # where the desaturation starts, as a fraction of the ceiling
BLOCK = 16          # the coarse grid the local hue is measured on
MIN_SAMPLES = 8     # unclipped pixels a block needs before its colour is trusted


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
                 white: float = WHITE, saturation: float = SATURATED,
                 highlights: str = "reconstruct"):
        self._pattern = pattern
        self._black = float(black_level)
        self._gains = tuple(float(g) for g in gains)
        self._matrix = matrix
        self._gamma = gamma
        self.white = float(white)
        # Raw level at which the sensor is considered clipped, and what to do about it.
        self.saturation = float(saturation)
        self.highlights = highlights
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
        # What each channel can possibly reach once the gain has been applied: the sensor
        # clips them all at the same raw value, and the gains scale that apart. This is the
        # number a channel has to hit before it counts as clipped -- the white point is a
        # different thing, and capping this by it made a blue gain of 1.44 look like sensor
        # clipping on every bright blue in the frame.
        self._ceil = np.array([max((self.saturation - self._black) * g, 1.0)
                               for g in self._gains], np.float32)
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
        self._n = np.empty(shape + (3,), np.float32)       # channel / its own ceiling
        self._clip = np.empty(shape + (3,), bool)
        self._whole = np.empty(shape, bool)
        # The coarse grid the local hue is measured on: the largest block up to BLOCK that
        # divides the frame, or the whole frame when nothing does.
        self._block = next((b for b in range(BLOCK, 0, -1)
                            if shape[0] % b == 0 and shape[1] % b == 0), 1)
        self._ready = True
        self._gain_dirty = True

    # --- the pipeline ------------------------------------------------------------------
    def linear(self, raw: np.ndarray) -> np.ndarray:
        """Everything up to gamma: (h, w, 3) float32 in raw units.

        Clipped highlights are forced to white here (see `highlight_clip`), so this is the
        display's linear stage rather than a measurement of the scene. What gets measured
        is the raw mosaic, which nothing in this class modifies.
        """
        self._prepare(raw.shape)
        if self._gain_dirty:
            self._rebuild_gain_map()
        lin = self._lin
        np.subtract(raw, self._black, out=lin, dtype=np.float32)
        np.multiply(lin, self._gain_map, out=lin)
        # Only the floor: a gain may legitimately carry a channel past the white point, and
        # the display window decides what reaches the screen. Clipping here would truncate
        # the channel with the largest gain and tint the result, which is the whole bug.
        np.maximum(lin, 0.0, out=lin)
        if not self.demosaic:
            return lin
        rgb, tmp = self._rgb, self._tmp
        for i, (ch, box) in enumerate((("R", color.box_full), ("G", color.box_cross),
                                       ("B", color.box_full))):
            np.multiply(lin, self._masks[ch], out=tmp)
            box(tmp, out=rgb[..., i])
            np.multiply(rgb[..., i], self._inv[ch], out=rgb[..., i])
        # One pass over the mosaic answers whether any of this is needed at all, and it
        # costs a twentieth of a millisecond against the several the stage itself takes.
        if self.highlights != "off" and raw.max() >= self.saturation:
            self._fix_highlights(rgb)
        if not self._identity_ccm:
            flat = rgb.reshape(-1, 3)
            np.matmul(flat, self._ccm.T, out=flat)
            np.maximum(rgb, 0.0, out=rgb)      # the matrix can go negative; the top is the
                                               # window's business, not ours
        return rgb

    def _fix_highlights(self, rgb: np.ndarray) -> None:
        """Repair pixels that ran out of sensor, in place and before the colour matrix.

        A blown pixel was equal in all three channels when the sensor clipped it; the white
        balance then scales them apart and each stops at its own ceiling, so what arrives is
        a tint. Two stages, and both only ever touch a pixel that actually lost a channel --
        a legitimately bright colour that did not clip is left exactly as measured.

        **Reconstruct.** Where some channels clipped and others did not, the survivors say
        how bright the pixel is, and a coarse map of the local colour -- built only from
        pixels where *every* channel survived, so the ratio between channels means something
        -- says what colour it should be. The clipped channels are set to that colour at that
        brightness, never below the ceiling they already reached, which is the sensor's word
        that they were at least that bright.

        **Roll off.** What is left is faded toward its own brightest channel, by how close
        the *surviving* channels are to running out too. A pixel with nothing left is fully
        faded, which is exactly neutral, so a blown highlight ends white; one with headroom
        in green is barely touched, so a warm highlight stays warm. Driving the fade from the
        survivors rather than from the result is the trick: do it the other way and the fade
        whitens the very pixels the reconstruction just saved.

        Everything after the clip test works on the list of damaged pixels rather than the
        frame, so the cost follows how much of the picture is actually blown. On a frame with
        nothing clipped it stops at the test.
        """
        n, clip = self._n, self._clip
        np.divide(rgb, self._ceil, out=n)
        np.greater_equal(n, CLIP_AT, out=clip)
        hurt = clip.any(axis=2)
        sel = np.flatnonzero(hurt.reshape(-1))
        if sel.size == 0:
            return                       # nothing ran out: the ordinary case, and cheap

        flat_n = n.reshape(-1, 3)
        flat_rgb = rgb.reshape(-1, 3)
        pn = flat_n[sel]                                   # (k, 3) the damaged pixels
        pclip = clip.reshape(-1, 3)[sel]
        kept = 3 - pclip.sum(axis=1)                       # channels still carrying data

        if self.highlights == "reconstruct":
            usable = (kept > 0) & (kept < 3)
            if usable.any():
                b, (h, w) = self._block, self._shape
                whole = np.logical_not(hurt, out=self._whole)
                wf = whole.view(np.uint8).astype(np.float32)
                num = (n * wf[..., None]).reshape(h // b, b, w // b, b, 3).sum(axis=(1, 3))
                cnt = wf.reshape(h // b, b, w // b, b).sum(axis=(1, 3))
                hue = num / np.maximum(cnt, 1.0)[..., None]
                np.maximum(hue, 1e-3, out=hue)
                # Look the local colour up per damaged pixel, by which block it is in: no
                # need to paint the coarse grid back over the whole frame.
                rows, cols = sel // w, sel % w
                block = (rows // b) * (w // b) + (cols // b)
                ph = hue.reshape(-1, 3)[block]
                trust = (cnt.reshape(-1)[block] >= MIN_SAMPLES) & usable
                intact = ~pclip
                scale = ((pn / ph) * intact).sum(axis=1) / np.maximum(kept, 1)
                want = np.maximum(ph * scale[:, None], 1.0)   # never below the ceiling
                np.copyto(pn, want, where=pclip & trust[:, None])

        # The fade, measured on the channels that survived: 0 while they have headroom, 1
        # once they are at their own ceilings or there are none left.
        t = np.max(pn * ~pclip, axis=1)
        t = np.clip((t - KNEE) * (1.0 / (1.0 - KNEE)), 0.0, 1.0)
        t[kept == 0] = 1.0
        if self.highlights == "white":
            t[:] = 1.0                                     # the blunt mode, for contrast

        np.multiply(pn, self._ceil, out=pn)
        mx = pn.max(axis=1, keepdims=True)
        pn += t[:, None] * (mx - pn)
        flat_rgb[sel] = pn

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
