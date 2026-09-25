"""Measure how the firmware's row transfers line up with the rows the sensor actually sends.

    uv run python tools/check_alignment.py                 # START, stream ~0.4 s

Triggers the Saleae on NanEye_EN, sends the start command, lets the firmware stream, then
compares, on pin 1 (what the Teensy receives):

  - where each LPSPI row burst (3936 clocks, gaps between) starts, and
  - where each sensor row starts (8 training words, then the '1' start bit of pixel 0).

A correctly phased link has every burst start exactly on a sensor row start. The offset,
in clocks, says how far off the firmware's phase count is: a multiple of 12 is whole pixel
periods, anything else a bit slip.
"""

import argparse
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

from serial.tools import list_ports  # noqa: E402

from naneye import decode  # noqa: E402
from naneye.saleae import Logic  # noqa: E402
from naneye.transport import Device  # noqa: E402

CH_SDI, CH_EN, CH_SCLK = 0, 1, 3
ROW_BITS = 3936


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="START")
    ap.add_argument("--clock", type=int, default=12375000)
    ap.add_argument("--after", type=float, default=0.4)
    ap.add_argument("--rate", type=float, default=250e6)
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "saleae", "alignment"))
    args = ap.parse_args()

    logic = Logic()
    port = None
    t0 = time.time()
    while not port and time.time() - t0 < 15:
        port = next((p.device for p in list_ports.comports() if p.vid == 0x16C0), None)
        time.sleep(0.3)
    chans = [CH_SDI, CH_EN, CH_SCLK]
    with Device(port) as dev:
        time.sleep(0.3)
        dev.serial.reset_input_buffer()
        dev.ask("STOP", timeout=3)
        dev.ask("POWER 0")
        dev.ask(f"CLK {args.clock}")
        time.sleep(0.3)
        cap = logic.start_triggered(chans, rate=args.rate, trigger=CH_EN, after_s=args.after)
        time.sleep(1.0)
        for r in dev.ask(args.start, timeout=10):
            print(r)
        logic.wait(cap)
        dev.ask("STOP", timeout=3)
        dev.ask("POWER 0")

    d = logic.export_digital(cap, args.out, chans)
    print(f"capture {cap} left open in Logic 2")

    sr = d[CH_SCLK].edges(True)
    # the bit launched after rising edge k is stable just before rising edge k + 1
    bits = d[CH_SDI].level_at(np.append(sr[1:], sr[-1]) - 2e-9).astype(np.uint8)
    gap_after = np.flatnonzero(np.diff(sr) > 150e-9)
    burst_start = np.concatenate([[0], gap_after + 1])
    burst_len = np.diff(np.concatenate([burst_start, [len(sr)]]))
    rows = burst_start[burst_len == ROW_BITS]
    print(f"{len(sr):,} clocks, {len(rows)} row-sized bursts")

    sensor_rows = np.asarray(decode.find_row_starts(bits))
    if sensor_rows.ndim > 1:  # (starts, ...) tuple form
        sensor_rows = sensor_rows[0]
    print(f"{len(sensor_rows)} sensor row starts found")
    if not len(rows) or not len(sensor_rows):
        return
    idx = np.searchsorted(sensor_rows, rows)
    idx = np.clip(idx, 1, len(sensor_rows) - 1)
    before = sensor_rows[idx - 1]
    after = sensor_rows[idx]
    off = np.where(rows - before < after - rows, rows - before, rows - after)
    vals, counts = np.unique(off, return_counts=True)
    order = np.argsort(-counts)[:8]
    print("burst start - nearest sensor row start (clocks): " +
          ", ".join(f"{int(vals[i])} x{int(counts[i])}" for i in order))
    k = rows[len(rows) // 2]
    s = "".join(map(str, bits[k:k + 132]))
    print("bits at a mid-capture burst start: " + " ".join(s[i:i + 12] for i in range(0, 132, 12)))


if __name__ == "__main__":
    main()
