"""The streaming recorder, end to end on replayed frames."""

import csv
import json

import numpy as np

from naneye import record


def run(tmp_path, *extra):
    out = tmp_path / "run"
    assert record.main(["--source", "replay", "--out", str(out), *extra]) == 0
    return out


def test_frames_npy_meta_and_summary_agree(tmp_path):
    out = run(tmp_path, "--frames", "7")
    frames = np.load(out / "frames.npy")
    assert frames.shape == (7, 320, 320) and frames.dtype == np.uint16
    assert frames.max() <= 1023
    rows = list(csv.DictReader(open(out / "meta.csv")))
    assert [int(r["index"]) for r in rows] == list(range(7))
    assert abs(float(rows[3]["mean"]) - frames[3].mean()) < 0.01   # same frame, same order
    summary = json.load(open(out / "run.json"))
    assert summary["frames"] == 7 and summary["shape"] == [7, 320, 320]
    assert summary["lost_on_pc"] == 0 and summary["dropped_by_device"] == 0
    assert summary["frames_saved"] is True
    assert not (out / "frames.bin").exists()       # assembled, then removed


def test_keep_bin_leaves_the_raw_stream_loadable(tmp_path):
    out = run(tmp_path, "--frames", "3", "--keep-bin")
    raw = np.fromfile(out / "frames.bin", dtype=np.uint16).reshape(-1, 320, 320)
    assert np.array_equal(raw, np.load(out / "frames.npy"))


def test_no_frames_writes_metadata_only(tmp_path):
    out = run(tmp_path, "--frames", "4", "--no-frames")
    assert not (out / "frames.npy").exists() and not (out / "frames.bin").exists()
    assert len(list(csv.DictReader(open(out / "meta.csv")))) == 4
    assert json.load(open(out / "run.json"))["frames_saved"] is False


def test_raw_pixel_periods(tmp_path):
    # The diagnostic format: whole 12-bit pixel periods, 328 per row including the training
    # words, which is what the bring-up tools read.
    out = run(tmp_path, "--frames", "2", "--depth", "12")
    frames = np.load(out / "frames.npy")
    assert frames.dtype == np.uint16 and frames.shape == (2, 320, 320)
