"""The ISP: each stage does what it says, the fast kernels match the readable ones, and
the whole pipeline stays inside its frame budget."""

import time

import numpy as np
import pytest

from naneye import color, isp


def mosaic(pattern, values, shape=(16, 16)):
    raw = np.zeros(shape, np.uint16)
    for ch, mask in color.masks(pattern, shape).items():
        raw[mask] = values[ch]
    return raw


def test_the_fast_kernels_match_the_readable_ones():
    rng = np.random.default_rng(11)
    a = rng.random((17, 23)).astype(np.float32) * 1000
    assert np.allclose(color.box_full(a), color._box(a, color._K_FULL), atol=1e-2)
    assert np.allclose(color.box_cross(a), color._box(a, color._K_CROSS), atol=1e-2)


def test_the_demosaic_matches_the_reference_implementation():
    rng = np.random.default_rng(12)
    raw = rng.integers(0, 1024, (32, 32)).astype(np.uint16)
    fast = isp.Isp(pattern="GRBG", gamma=1.0).linear(raw)
    assert np.allclose(fast, color.demosaic(raw, "GRBG"), atol=1e-2)


def test_black_level_is_subtracted_and_never_goes_negative():
    raw = np.full((8, 8), 100, np.uint16)
    lin = isp.Isp(black_level=40, gamma=1.0).linear(raw)
    assert np.allclose(lin, 60)
    assert isp.Isp(black_level=200, gamma=1.0).linear(raw).min() == 0


def test_white_balance_gains_land_on_the_right_colours():
    raw = mosaic("BGGR", {"R": 100, "G": 100, "B": 100})
    lin = isp.Isp(pattern="BGGR", gains=(2.0, 1.0, 3.0), gamma=1.0).linear(raw)
    r, g, b = lin[8, 8]
    assert (r, g, b) == pytest.approx((200, 100, 300))


def test_auto_white_balance_corrects_a_cast_and_respects_the_black_level():
    raw = mosaic("BGGR", {"R": 150, "G": 250, "B": 450})
    pipe = isp.Isp(pattern="BGGR", black_level=50, gamma=1.0)
    pipe.gains = pipe.auto_white_balance(raw)
    assert pipe.gains == pytest.approx((2.0, 1.0, 0.5))
    assert np.allclose(pipe.linear(raw)[8, 8], 200)


def test_the_colour_matrix_is_applied_and_clipped():
    raw = mosaic("BGGR", {"R": 500, "G": 500, "B": 500})
    neutral = isp.Isp(pattern="BGGR", matrix="none", gamma=1.0).linear(raw)[8, 8]
    boosted = isp.Isp(pattern="BGGR", matrix="saturation", gamma=1.0).linear(raw)[8, 8]
    # A neutral patch stays near neutral through a saturation matrix (rows sum to ~1).
    assert np.allclose(neutral, 500)
    assert boosted.max() - boosted.min() < 60
    # A saturated red goes further out, not further in.
    red = mosaic("BGGR", {"R": 900, "G": 200, "B": 200})
    out = isp.Isp(pattern="BGGR", matrix="saturation", gamma=1.0).linear(red)[8, 8]
    assert out[0] > 900 - 1e-3 or out[0] == pytest.approx(1023, abs=1)
    assert out[2] <= 200
    assert out.min() >= 0


def test_gamma_lifts_the_shadows_and_keeps_the_ends():
    linear = isp.gamma_lut(1.0)
    curve = isp.gamma_lut(2.2)
    srgb = isp.gamma_lut(None)
    for lut in (linear, curve, srgb):
        assert lut[0] == 0 and lut[-1] == 255
    mid = len(curve) // 2
    assert curve[mid] > linear[mid] and srgb[mid] > linear[mid]
    assert np.all(np.diff(curve.astype(int)) >= 0)          # monotonic


def test_process_returns_display_ready_pixels():
    rng = np.random.default_rng(13)
    raw = rng.integers(0, 1024, (16, 16)).astype(np.uint16)
    pipe = isp.Isp()
    out = pipe.process(raw)
    assert out.shape == (16, 16, 3) and out.dtype == np.uint8
    pipe.demosaic = False
    assert pipe.process(raw).shape == (16, 16)


def test_settings_changes_take_effect_on_the_next_frame():
    raw = mosaic("BGGR", {"R": 400, "G": 400, "B": 400})
    pipe = isp.Isp(pattern="BGGR", gamma=1.0)
    assert np.allclose(pipe.linear(raw)[8, 8], 400)
    pipe.gains = (2.0, 1.0, 1.0)            # the cached gain map must be rebuilt
    assert pipe.linear(raw)[8, 8][0] == pytest.approx(800)
    pipe.black_level = 400
    assert pipe.linear(raw).max() == 0
    pipe.pattern = "RGGB"                   # and the masks with it
    assert pipe.pattern == "RGGB"


def test_a_frame_costs_far_less_than_a_frame_period():
    raw = np.random.default_rng(14).integers(0, 1024, (320, 320)).astype(np.uint16)
    pipe = isp.Isp(black_level=40, gains=(1.6, 1.0, 1.9), matrix="saturation", gamma=2.2)
    for _ in range(3):
        pipe.process(raw)
    best = min(_timed(pipe, raw) for _ in range(5))
    # 35 fps leaves 28 ms. Anything near that means the fast path stopped being fast;
    # the margin is wide so the check survives a loaded machine.
    assert best < 14.0, f"{best:.1f} ms a frame"


def _timed(pipe, raw):
    t0 = time.perf_counter()
    pipe.process(raw)
    return (time.perf_counter() - t0) * 1000.0
