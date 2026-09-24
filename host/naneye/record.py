"""Lossless frame recorder, streaming to disk.

    python -m naneye.record --source auto --frames 1000 --out capture/run1
    python -m naneye.record --source auto --seconds 600 --out capture/soak
    python -m naneye.record --source replay --frames 5 --out capture/demo

Writes, per run:
    frames.npy    (N, 320, 320) uint16 for 10-bit (uint8 for 8-bit)
    meta.csv      one row per frame: counter, timestamp, exposure, config, error counters
    run.json      capture settings and a summary, including frames dropped by the device
                  and frames lost on the PC
    stream.bin    optional (--raw): every packet header, for replay tooling

Frames go to disk as they arrive (frames.bin, then assembled into frames.npy at the end), so
memory use stays flat however long the run: a 10-minute recording at 49.5 MHz is about
4.3 GB. If the process dies, frames.bin and meta.csv hold everything received so far, and
frames.bin loads with np.fromfile(..., dtype).reshape(-1, 320, 320).

Every frame's own header travels with it, so a recording is self-describing: nothing about
exposure, gain, clock rate or dropped frames has to be remembered separately.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time

import numpy as np

from . import protocol
from .accounting import FrameAccounting
from .sources import open_source

META_FIELDS = ["index", "frame_counter", "timestamp_us", "exposure_pp", "exposure_us",
               "sclk_hz", "cfg0", "cfg1", "format", "flags", "rows_failed",
               "pixels_concealed", "frames_dropped", "min", "max", "mean"]
COPY_CHUNK = 256  # frames per chunk when assembling frames.npy


def start_device(source, args) -> None:
    """Configure and start the device, writing only: the recorder's own read loop picks up
    the replies, so it is already reading when the first frame arrives."""
    dev = source.device
    cmds = ["STOP", f"CLK {args.clock}", f"DEPTH {args.depth}"]
    if args.exposure is not None:
        cmds.append(f"EXP {args.exposure}")
    if args.led is not None:
        cmds += [f"LEDI {args.led}", "LED 1"]
    cmds.append("START")
    for c in cmds:
        dev.command(c)


def assemble_npy(raw_path: str, npy_path: str, n: int, shape, dtype) -> None:
    """frames.bin -> frames.npy, in chunks, so memory stays flat."""
    src = np.memmap(raw_path, dtype=dtype, mode="r", shape=(n, *shape))
    dst = np.lib.format.open_memmap(npy_path, mode="w+", dtype=dtype, shape=(n, *shape))
    for i in range(0, n, COPY_CHUNK):
        dst[i:i + COPY_CHUNK] = src[i:i + COPY_CHUNK]
    dst.flush()
    del src, dst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="replay")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--frames", type=int, default=100)
    ap.add_argument("--seconds", type=float, default=None,
                    help="record for this long instead of a frame count")
    ap.add_argument("--depth", type=int, default=10, choices=(8, 10, 12))
    ap.add_argument("--clock", type=int, default=49500000,
                    help="SCLK: 49500000 (default, ~35 fps), 24750000 or 12375000")
    ap.add_argument("--exposure", type=int, default=None,
                    help="rows_in_reset (0 = longest exposure)")
    ap.add_argument("--led", type=float, default=None, help="LED current in mA")
    ap.add_argument("--raw", action="store_true", help="also save every packet header")
    ap.add_argument("--keep-bin", action="store_true",
                    help="keep frames.bin next to frames.npy")
    ap.add_argument("--no-frames", action="store_true",
                    help="write meta.csv and run.json only: for soak tests of the link, "
                         "which need every header but not ~200 KB of pixels per frame")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    fmt = {8: protocol.FMT_GRAY8, 10: protocol.FMT_GRAY10,
           12: protocol.FMT_RAW12}[args.depth]
    source = open_source(args.source, depth=args.depth, clock_hz=args.clock, fmt=fmt,
                         start=False)
    print(f"source: {source.name}")
    live = hasattr(source, "device")
    if live:
        start_device(source, args)
    elif args.exposure is not None or args.led is not None:
        print("  (--exposure / --led ignored: not a live device)")

    raw_path = os.path.join(args.out, "frames.bin")
    bin_file = None if args.no_frames else open(raw_path, "wb")
    meta_file = open(os.path.join(args.out, "meta.csv"), "w", newline="")
    meta = csv.DictWriter(meta_file, fieldnames=META_FIELDS)
    meta.writeheader()
    raw_file = open(os.path.join(args.out, "stream.bin"), "wb") if args.raw else None

    acct = FrameAccounting()
    n = 0
    shape = dtype = None
    last = None
    started = not live       # a live device: ignore frames until START has answered
    seen_log = 0
    t_start = t_last = None
    deadline = None
    interrupted = False
    try:
        with source:
            for header, img in source.frames():
                if not started:
                    log = source.log
                    for line in log[seen_log:]:
                        if line.startswith("START failed"):
                            print(f"\n{line}")
                            return 1
                        if line.startswith("START ok"):
                            started = True
                    seen_log = len(log)
                    if not started:
                        continue
                if t_start is None:
                    t_start = time.time()
                    deadline = t_start + args.seconds if args.seconds else None
                    shape, dtype = img.shape, img.dtype
                acct.add(header)
                if bin_file:
                    img.tofile(bin_file)
                meta.writerow({
                    "index": n,
                    "frame_counter": header.frame_counter,
                    "timestamp_us": header.timestamp_us,
                    "exposure_pp": header.exposure_pp,
                    "exposure_us": round(header.exposure_us(), 3),
                    "sclk_hz": header.sclk_hz,
                    "cfg0": f"0x{header.cfg0:04X}",
                    "cfg1": f"0x{header.cfg1:04X}",
                    "format": header.format_name,
                    "flags": header.flags,
                    "rows_failed": header.rows_failed,
                    "pixels_concealed": header.pixels_concealed,
                    "frames_dropped": header.frames_dropped,
                    "min": int(img.min()),
                    "max": int(img.max()),
                    "mean": round(float(img.mean()), 3),
                })
                if raw_file:
                    raw_file.write(protocol.build_packet(header, b""))  # header only
                n += 1
                last = header
                t_last = time.time()
                if n % 35 == 0:
                    meta_file.flush()
                    done = (f"{time.time() - t_start:6.0f}/{args.seconds:.0f} s" if deadline
                            else f"{n}/{args.frames} frames")
                    print(f"\r{done}  lost on PC {acct.lost_on_pc}  dropped by device "
                          f"{acct.dropped_on_device}  failed rows {acct.rows_failed}  "
                          f"concealed {acct.pixels_concealed}", end="", flush=True)
                if (deadline and time.time() >= deadline) or (not deadline and n >= args.frames):
                    break
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted: keeping what was received")
    finally:
        if bin_file:
            bin_file.close()
        meta_file.close()
        if raw_file:
            raw_file.close()

    if n == 0:
        print("no frames captured")
        return 1

    elapsed = t_last - t_start   # first to last frame: excludes stopping the device
    if bin_file:
        assemble_npy(raw_path, os.path.join(args.out, "frames.npy"), n, shape, dtype)
        if not args.keep_bin:
            os.remove(raw_path)

    summary = {
        "source": source.name,
        "frames": n,
        "shape": [n, *shape],
        "dtype": str(np.dtype(dtype)),
        "depth": args.depth,
        "elapsed_s": round(elapsed, 3),
        "measured_fps": round((n - 1) / elapsed, 2) if elapsed > 0 else None,
        "interrupted": interrupted,
        "frames_saved": bin_file is not None,
        "dropped_by_device": acct.dropped_on_device,
        "lost_on_pc": acct.lost_on_pc,
        "counter_gaps": acct.missing,
        "rows_failed_total": acct.rows_failed,
        "pixels_concealed_total": acct.pixels_concealed,
        "sclk_hz": last.sclk_hz,
        "exposure_us": round(last.exposure_us(), 3),
        "cfg0": f"0x{last.cfg0:04X}",
        "cfg1": f"0x{last.cfg1:04X}",
        "cfa": last.cfa,          # MONO, or the Bayer pattern the frames carry
    }
    reader = getattr(getattr(source, "device", None), "reader", None)
    if reader is not None:
        summary["host_bad_crc"] = reader.bad_crc
        summary["host_resyncs"] = reader.resyncs
    with open(os.path.join(args.out, "run.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{n} frames -> {args.out}")
    print(f"  ({n}, {shape[0]}, {shape[1]}) {np.dtype(dtype)}, {elapsed:.1f}s, "
          f"{summary['measured_fps']} fps measured")
    print(f"  lost on PC {acct.lost_on_pc}, dropped by device {acct.dropped_on_device}, "
          f"{acct.rows_failed} failed rows, {acct.pixels_concealed} pixels concealed")
    if reader is not None:
        print(f"  host: {reader.bad_crc} packets failed CRC, {reader.resyncs} resyncs")
    if acct.missing or acct.rows_failed:
        print("  NOTE: frames were missing or rows failed validation; see meta.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
