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
    pipe = isp.Isp(pattern="GRBG", gamma=1.0, highlights="off", method="bilinear")
    assert np.allclose(pipe.linear(raw), color.demosaic(raw, "GRBG"), atol=1e-2)


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


def test_clipped_highlights_come_out_neutral_not_tinted():
    """The sensor clips every channel at the same raw value; white balance then scales
    them apart and each clips again, which is what turns a blown highlight pink."""
    raw = np.full((16, 16), 400, np.uint16)
    raw[4:8, 4:8] = 1023                      # a blown patch, every Bayer site at the top
    pipe = isp.Isp(pattern="BGGR", black_level=170, gains=(1.11, 1.0, 1.44),
                   matrix="saturation", gamma=1.0)

    pipe.highlights = "off"
    off = pipe.process(raw)[6, 6]
    assert off[0] != off[1] or off[2] != off[1]          # the fault: a tint, not white
    for mode in ("white", "reconstruct"):
        pipe.highlights = mode
        assert tuple(pipe.process(raw)[6, 6]) == (255, 255, 255), mode
    # and an ordinary pixel is untouched by any of it
    untouched = isp.Isp(pattern="BGGR", black_level=170, gains=(1.11, 1.0, 1.44),
                        matrix="saturation", gamma=1.0, highlights="off")
    assert np.array_equal(pipe.process(raw)[12, 12], untouched.process(raw)[12, 12])


def test_one_clipped_channel_is_repaired_without_whitening_the_pixel():
    """A blue site at the ceiling in an otherwise dark frame is a bright blue speck, and
    saying so is the point of doing this per channel: red and green still hold good data."""
    raw = np.full((32, 32), 300, np.uint16)
    raw[8, 8] = 1023                                     # a (0,0) site: blue, under BGGR
    # Bilinear here on purpose: a gradient-corrected interpolation rings around an
    # isolated spike, which is its own story and not this one.
    pipe = isp.Isp(pattern="BGGR", gains=(1.2, 1.0, 1.5), matrix="none", gamma=1.0,
                   highlights="reconstruct", method="bilinear")
    out = pipe.process(raw)
    plain = isp.Isp(pattern="BGGR", gains=(1.2, 1.0, 1.5), matrix="none", gamma=1.0,
                    highlights="off", method="bilinear").process(raw)

    assert out[8, 8][2] == 255                           # blue: clipped, so at least full
    assert tuple(out[8, 8]) != (255, 255, 255)           # but the pixel is not whitened
    assert abs(int(out[8, 8][0]) - int(plain[8, 8][0])) <= 1      # red as measured
    assert abs(int(out[8, 8][1]) - int(plain[8, 8][1])) <= 1      # green as measured
    assert np.array_equal(out[8, 12], plain[8, 12])      # and the neighbours untouched


def test_the_mono_path_does_not_pay_for_highlight_repair():
    raw = np.full((8, 8), 1023, np.uint16)
    pipe = isp.Isp(gamma=1.0)
    pipe.demosaic = False
    assert pipe.process(raw).ndim == 2


def bayer(scene, pattern="BGGR"):
    """Sample a (h, w, 3) scene through a mosaic, the way the sensor does."""
    raw = np.zeros(scene.shape[:2], np.uint16)
    for i, ch in enumerate("RGB"):
        m = color.masks(pattern, raw.shape)[ch]
        raw[m] = np.clip(scene[..., i], 0, 1023).astype(np.uint16)[m]
    return raw


def test_gradient_corrected_interpolation_beats_bilinear_on_detail():
    """Fine detail is where bilinear invents colour. Malvar's correction is the curvature
    of the colour that was actually sampled, so it knows the scene is changing there."""
    h = w = 64
    x = np.arange(w)
    scene = np.empty((h, w, 3), np.float32)
    for i, phase in enumerate((0.0, 0.4, 0.8)):
        scene[..., i] = 500 + 300 * np.sin(x / 3.0 + phase)
    raw = bayer(scene)

    def error(out):
        out, ref = out[2:-2, 2:-2], scene[2:-2, 2:-2]     # a 5x5 filter cannot see the edge
        colour = np.abs((out.max(-1) - out.min(-1)) - (ref.max(-1) - ref.min(-1)))
        return np.abs(out - ref).mean(), colour.mean()

    plain = error(color.demosaic(raw, "BGGR"))
    better = error(color.demosaic_malvar(raw, "BGGR"))
    assert better[0] < plain[0] and better[1] < plain[1], (plain, better)
    # and it must leave the sampled channels exactly alone, as bilinear does
    for ch, i in (("R", 0), ("G", 1), ("B", 2)):
        m = color.masks("BGGR", raw.shape)[ch]
        assert np.allclose(color.demosaic_malvar(raw, "BGGR")[..., i][m], raw[m])


def test_malvar_is_exact_on_smooth_and_flat_scenes():
    # The correction is a second derivative, so it must vanish where there is no curvature.
    flat = np.full((32, 32, 3), 500, np.float32)
    out = color.demosaic_malvar(bayer(flat), "BGGR")
    assert np.allclose(out, 500.0)
    ramp = np.repeat(np.broadcast_to(np.linspace(100, 900, 32).astype(np.float32),
                                     (32, 32))[..., None], 3, axis=2)
    out = color.demosaic_malvar(bayer(ramp), "BGGR")[3:-3, 3:-3]
    assert np.abs(out - ramp[3:-3, 3:-3]).max() < 2.0     # only the rounding to uint16


def test_denoise_takes_the_colour_noise_and_leaves_the_detail():
    """Chroma is smooth almost everywhere luma is not, so blurring it removes speckle
    without softening anything. Measured in a flat patch, which is where noise lives --
    measure across an edge instead and the demosaic's own false colour swamps the result."""
    rng = np.random.default_rng(5)
    h = w = 64
    scene = np.empty((h, w, 3), np.float32)
    edge = np.where(np.arange(w) < w // 2, 700.0, 250.0)
    for i in range(3):
        scene[..., i] = edge
    raw = bayer(scene + rng.normal(0, 25, (h, w, 3)))

    def measure(mode):
        p = isp.Isp(pattern="BGGR", gamma=1.0, method="malvar", denoise=mode,
                    highlights="off")
        out = p.linear(raw)
        flat = out[8:56, 6:26]                    # inside the bright half, clear of the edge
        whole = out[2:-2, 2:-2].mean(-1)
        return ((flat - flat.mean(-1, keepdims=True)).std(),      # colour noise
                flat.mean(-1).std(),                              # luma noise
                abs(whole[:, w // 2 - 6].mean() - whole[:, w // 2 + 2].mean()))  # the edge

    off, chroma, both = (measure(m) for m in ("off", "chroma", "chroma+luma"))
    assert chroma[0] < 0.8 * off[0]              # colour noise down by a third or so
    assert chroma[1] > 0.97 * off[1]             # luma noise untouched: no softening
    assert chroma[2] > 0.99 * off[2]             # and the edge is exactly as high
    assert both[1] < 0.9 * off[1]                # the luma option does smooth luma
    assert both[2] > 0.99 * off[2]               # still without rounding off the edge


def test_sharpen_lifts_detail_without_touching_colour():
    h = w = 48
    scene = np.empty((h, w, 3), np.float32)
    edge = np.where(np.arange(w) < w // 2, 650.0, 300.0)
    for i in range(3):
        scene[..., i] = edge
    raw = bayer(scene)

    def measure(amount):
        p = isp.Isp(pattern="BGGR", gamma=1.0, method="malvar", sharpen=amount,
                    highlights="off")
        out = p.linear(raw)[2:-2, 2:-2]
        luma = out.mean(-1)
        acutance = np.abs(np.diff(luma, axis=1)).max()
        chroma = np.abs(out - out.mean(-1, keepdims=True)).mean()
        return acutance, chroma

    off = measure("off")
    for amount in ("light", "medium", "strong"):
        got = measure(amount)
        assert got[0] > off[0], amount                  # the edge is steeper
        assert got[1] <= off[1] + 0.5, amount           # and no colour was invented
    assert measure("strong")[0] > measure("light")[0]


def test_every_stage_together_stays_inside_the_frame_budget():
    raw = np.random.default_rng(6).integers(0, 1024, (320, 320)).astype(np.uint16)
    raw[80:160, 80:160] = 1023                          # give the highlight stage work too
    pipe = isp.Isp(pattern="BGGR", black_level=170, gains=(1.11, 1.0, 1.44),
                   matrix="saturation", gamma=None, highlights="reconstruct",
                   method="malvar", denoise="chroma+luma", sharpen="strong")
    for _ in range(3):
        pipe.process(raw)
    best = min(_timed(pipe, raw) for _ in range(5))
    assert best < 20.0, f"{best:.1f} ms a frame with everything on"
