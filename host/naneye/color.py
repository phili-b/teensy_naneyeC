"""The colour NanEyeC's Bayer mosaic: patterns, demosaicing, and white balance.

A colour NanEyeC streams exactly what a mono one does — one value per pixel, no marker of
any kind — so the mosaic is something the bench knows and the sensor does not say. The
firmware is told once (`CFA BGGR`), keeps it in EEPROM and puts it in every frame header,
so a recording made today still knows what it is next year.

The pattern is named by the top-left 2x2 of the array **as received**, reading row 0 first:

    BGGR  ->  B G      GRBG  ->  G R
              G R                B G

The datasheet (6.3.1) says the first pixel read out, (1,1), is the bottom-left one and has
a blue filter, which makes the received array **BGGR**. Measured on the bench: the two
green sites are the (0,1)/(1,0) diagonal, which is what BGGR (or RGGB) requires.

Everything here works on the raw 10-bit values and returns float arrays in the same scale,
so nothing is clipped or quantised before the viewer decides on its contrast.
"""

from __future__ import annotations

import numpy as np

MONO = "MONO"
PATTERNS = ("BGGR", "GBRG", "GRBG", "RGGB")
ALL = (MONO,) + PATTERNS

# Wire encoding: header flag bits 4-6, mirroring firmware/src/usb_proto.h.
CFA_BY_CODE = {0: MONO, 1: "BGGR", 2: "GBRG", 3: "GRBG", 4: "RGGB"}
CODE_BY_CFA = {v: k for k, v in CFA_BY_CODE.items()}


def sites(pattern: str) -> dict:
    """{'R': [(dy, dx), ...], 'G': [...], 'B': [...]} for one pattern."""
    if pattern not in PATTERNS:
        raise ValueError(f"not a Bayer pattern: {pattern!r}")
    out = {"R": [], "G": [], "B": []}
    for i, ch in enumerate(pattern):
        out[ch].append((i // 2, i % 2))
    return out


def masks(pattern: str, shape) -> dict:
    """{'R': bool array, 'G': ..., 'B': ...} marking where each colour was sampled."""
    h, w = shape
    y, x = np.ogrid[:h, :w]
    out = {}
    for ch, positions in sites(pattern).items():
        m = np.zeros((h, w), bool)
        for dy, dx in positions:
            m |= ((y & 1) == dy) & ((x & 1) == dx)
        out[ch] = m
    return out


# 3x3 neighbourhood sums, edges handled by replication. Kept to shifts of a padded copy:
# no SciPy, and at 320x320 it costs well under a millisecond.
def _box(a: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="edge")
    out = np.zeros_like(a, dtype=np.float32)
    for dy in range(3):
        for dx in range(3):
            k = kernel[dy, dx]
            if k:
                out += k * p[dy:dy + a.shape[0], dx:dx + a.shape[1]]
    return out


_K_CROSS = np.array([[0, 1, 0], [1, 4, 1], [0, 1, 0]], np.float32)   # for green
_K_FULL = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float32)    # for red and blue


# The same two kernels again, written as slice arithmetic instead of nine shifts. They are
# what the ISP actually runs -- three times faster, and worth the duplication only because
# test_isp.py holds them to the reference _box() above on random data.
def box_full(a: np.ndarray, out=None) -> np.ndarray:
    """[1,2,1] x [1,2,1], separable: two passes of three terms instead of nine."""
    p = np.empty_like(a)
    p[:, 1:-1] = a[:, :-2] + a[:, 2:]
    p[:, 0] = a[:, 0] + a[:, 1]          # edge replication
    p[:, -1] = a[:, -2] + a[:, -1]
    p += 2.0 * a
    q = out if out is not None else np.empty_like(a)
    q[1:-1] = p[:-2] + p[2:]
    q[0] = p[0] + p[1]
    q[-1] = p[-2] + p[-1]
    q += 2.0 * p
    return q


def box_cross(a: np.ndarray, out=None) -> np.ndarray:
    """[[0,1,0],[1,4,1],[0,1,0]]: five terms, no separable form to exploit."""
    q = out if out is not None else np.empty_like(a)
    q[:] = 4.0 * a
    q[:, 1:-1] += a[:, :-2] + a[:, 2:]
    q[:, 0] += a[:, 0] + a[:, 1]
    q[:, -1] += a[:, -2] + a[:, -1]
    q[1:-1] += a[:-2] + a[2:]
    q[0] += a[0] + a[1]
    q[-1] += a[-2] + a[-1]
    return q


def _interpolate(values: np.ndarray, mask: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Weighted mean of the known samples in each 3x3 neighbourhood.

    Dividing by the weight of the samples that actually exist does two useful things: it
    reproduces known pixels exactly (no other same-colour site falls inside a 3x3 window)
    and it needs no special case at the border.
    """
    known = values.astype(np.float32) * mask
    weight = _box(mask.astype(np.float32), kernel)
    return _box(known, kernel) / np.maximum(weight, 1e-6)


def demosaic(raw: np.ndarray, pattern: str = "BGGR") -> np.ndarray:
    """Bilinear demosaic of a raw Bayer frame -> (h, w, 3) float32, R, G, B.

    Bilinear is the honest choice here: it is what "basic demosaic" means, it is a few
    hundred microseconds at 320x320, and its weakness (zippering on hard edges) is visible
    and easy to explain, unlike the plausible-looking guesses of cleverer methods.
    """
    if raw.ndim != 2:
        raise ValueError(f"expected one plane, got shape {raw.shape}")
    m = masks(pattern, raw.shape)
    return np.stack([
        _interpolate(raw, m["R"], _K_FULL),
        _interpolate(raw, m["G"], _K_CROSS),
        _interpolate(raw, m["B"], _K_FULL),
    ], axis=-1)


def channel_means(raw: np.ndarray, pattern: str) -> dict:
    """Mean of each colour's own samples, with no interpolation in the way.

    Taken through strided views of the four sub-lattices rather than boolean masks: the
    views cost nothing, and auto white balance calls this on every frame.
    """
    out = {}
    for ch, positions in sites(pattern).items():
        total = sum(float(raw[dy::2, dx::2].mean()) for dy, dx in positions)
        out[ch] = total / len(positions)
    return out


def grey_world_gains(raw: np.ndarray, pattern: str) -> tuple:
    """(r, g, b) gains that make the frame's average grey. Green is left at 1.0."""
    mean = channel_means(raw, pattern)
    g = mean["G"]
    if g <= 0:
        return (1.0, 1.0, 1.0)
    return tuple(float(np.clip(g / mean[ch], 0.25, 4.0)) if mean[ch] > 0 else 1.0
                 for ch in ("R", "G", "B"))


def apply_gains(rgb: np.ndarray, gains) -> np.ndarray:
    return rgb * np.asarray(gains, np.float32)


def green_diagonal(raw: np.ndarray) -> tuple:
    """Which diagonal carries the greens, and how clearly: ('main'|'anti', margin).

    The two green sites see nearly the same scene through the same filter, so their
    sub-images track each other more closely than either tracks red or blue. 'main' means
    (0,0) and (1,1) are green — that is GRBG or GBRG — and 'anti' means (0,1) and (1,0),
    which is BGGR or RGGB. It cannot tell red from blue: nothing in a single frame can.
    """
    def corr(a, b):
        a = a.astype(np.float64).ravel()
        b = b.astype(np.float64).ravel()
        a, b = a - a.mean(), b - b.mean()
        d = np.sqrt((a @ a) * (b @ b))
        return float(a @ b / d) if d > 0 else 0.0

    main = corr(raw[0::2, 0::2], raw[1::2, 1::2])
    anti = corr(raw[0::2, 1::2], raw[1::2, 0::2])
    return ("main" if main > anti else "anti", abs(main - anti))


def to_display(raw: np.ndarray, pattern: str, gains=None, white: float = 1023.0):
    """Demosaic and white balance, returning float RGB scaled like the raw values."""
    rgb = demosaic(raw, pattern)
    if gains is not None:
        rgb = apply_gains(rgb, gains)
    return np.clip(rgb, 0, white)
