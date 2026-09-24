"""The Bayer mosaic: patterns, demosaicing, white balance, and the header flag."""

import numpy as np
import pytest

from naneye import color, protocol


def mosaic(pattern, values, shape=(8, 8)):
    """A raw frame where every site of a colour holds that colour's value."""
    raw = np.zeros(shape, np.uint16)
    for ch, mask in color.masks(pattern, shape).items():
        raw[mask] = values[ch]
    return raw


def test_pattern_names_the_top_left_2x2():
    s = color.sites("BGGR")
    assert s["B"] == [(0, 0)] and s["R"] == [(1, 1)]
    assert sorted(s["G"]) == [(0, 1), (1, 0)]
    s = color.sites("GRBG")
    assert s["R"] == [(0, 1)] and s["B"] == [(1, 0)]


def test_every_pixel_is_covered_exactly_once():
    m = color.masks("RGGB", (6, 6))
    total = m["R"].astype(int) + m["G"].astype(int) + m["B"].astype(int)
    assert (total == 1).all()
    assert m["G"].sum() == 2 * m["R"].sum() == 2 * m["B"].sum()


@pytest.mark.parametrize("pattern", color.PATTERNS)
def test_a_flat_scene_comes_back_flat(pattern):
    raw = mosaic(pattern, {"R": 100, "G": 200, "B": 300})
    rgb = color.demosaic(raw, pattern)
    # Away from the border every pixel sees the full neighbourhood of each colour.
    inner = rgb[1:-1, 1:-1]
    assert np.allclose(inner, [100, 200, 300])
    # Edge replication keeps the border in range rather than dark or wrapped.
    assert rgb.min() >= 100 and rgb.max() <= 300


@pytest.mark.parametrize("pattern", color.PATTERNS)
def test_known_pixels_survive_unchanged(pattern):
    rng = np.random.default_rng(7)
    raw = rng.integers(0, 1024, (16, 16)).astype(np.uint16)
    rgb = color.demosaic(raw, pattern)
    for i, ch in enumerate("RGB"):
        mask = color.masks(pattern, raw.shape)[ch]
        assert np.allclose(rgb[..., i][mask], raw[mask]), ch


def test_demosaic_rejects_an_already_colour_image():
    with pytest.raises(ValueError):
        color.demosaic(np.zeros((4, 4, 3), np.uint16), "BGGR")


def test_grey_world_gains_neutralise_a_cast():
    raw = mosaic("BGGR", {"R": 100, "G": 200, "B": 400})
    gains = color.grey_world_gains(raw, "BGGR")
    assert gains == pytest.approx((2.0, 1.0, 0.5))
    balanced = color.apply_gains(color.demosaic(raw, "BGGR"), gains)
    assert np.allclose(balanced[1:-1, 1:-1], 200)


def test_grey_world_gains_are_clamped_and_safe_on_black():
    raw = mosaic("BGGR", {"R": 1, "G": 900, "B": 900})
    assert color.grey_world_gains(raw, "BGGR")[0] == 4.0      # not 900
    assert color.grey_world_gains(np.zeros((4, 4), np.uint16), "BGGR") == (1.0, 1.0, 1.0)


def test_green_diagonal_finds_the_greens():
    # A smooth scene, as real ones are: neighbouring pixels of the same colour see nearly
    # the same thing, which is the whole basis of the test. White noise would tell nobody
    # anything, and the function does not pretend otherwise.
    y, x = np.ogrid[:32, :32]
    scene = 400 + 150 * np.sin(x / 5.0) + 150 * np.cos(y / 7.0)
    for pattern, expect in (("BGGR", "anti"), ("RGGB", "anti"),
                            ("GRBG", "main"), ("GBRG", "main")):
        raw = np.broadcast_to(scene, (32, 32)).astype(np.uint16).copy()
        # Give red and blue a different response, so only the greens still agree.
        m = color.masks(pattern, raw.shape)
        raw[m["R"]] = (raw[m["R"]] * 0.2).astype(np.uint16)
        raw[m["B"]] = 900 - raw[m["B"]]
        where, margin = color.green_diagonal(raw)
        assert where == expect and margin > 0.2, pattern


def test_the_frame_header_carries_the_mosaic():
    assert protocol.Header().cfa == color.MONO
    for code, name in color.CFA_BY_CODE.items():
        flags = protocol.FLAG_CONCEALED | (code << protocol.FLAG_CFA_SHIFT)
        h = protocol.Header(flags=flags)
        assert h.cfa == name
        assert h.flags & protocol.FLAG_CONCEALED      # the other flags still read back
    packed = protocol.Header(flags=1 << protocol.FLAG_CFA_SHIFT).pack()
    assert protocol.Header.unpack(packed).cfa == "BGGR"
