"""Measure the NanoBerry illumination ring and draw the documentation's LED figures.

    uv run --with matplotlib python tools/led_figures.py capture  # DAC writes on the wire
    uv run --with matplotlib python tools/led_figures.py sweep    # light vs LED current
    uv run --with matplotlib python tools/led_figures.py plot     # figures from both

Two measurements:

  capture  the three GPIOs of the LTC2630 DAC plus the LT3473 enable, while the firmware
           is told `LEDI 5`, `LED 1`, `LEDI 10`, `LED 0`. Shows the 24-bit write frames,
           their timing, and that the boost rail is switched in the right order
  sweep    the sensor itself as the photometer: mean image level against LED current at a
           fixed exposure, which is the only end-to-end proof that the light comes on

Channel map for this bench wiring (see docs/hardware.md): D4 LED_DAC_CS_N at Teensy pin 4,
D5 LED_DAC_SDI at pin 5, D6 LED_DAC_SCK at pin 6, D7 LED_VCC_ON at pin 3. Those are the
same probes that sit on VCC_SENSOR and the sensor end of the link for wire_figures.py, so
only one of the two tools can be captured at a time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

from naneye.saleae import Logic, read_digital_bin  # noqa: E402

CAP = os.path.join(ROOT, "build", "led")
OUT = os.path.join(ROOT, "docs", "images")
D_CS, D_SDI, D_SCK, D_VCC_ON = 4, 5, 6, 7
SWEEP_JSON = os.path.join(CAP, "sweep.json")

# What the capture asks the firmware to do, in order.
SEQUENCE = ["LEDMAX 20", "LEDI 5", "LED 1", "LEDI 10", "LED 0"]
SWEEP_MA = [0, 1, 2, 5, 10, 15, 20]
SWEEP_ROWS_IN_RESET = 1  # the longest exposure, where the LED's contribution is largest


def _device():
    from naneye.transport import Device

    dev = Device.open_first()
    time.sleep(0.3)
    dev.serial.reset_input_buffer()
    return dev


# --- capture ---------------------------------------------------------------------------
def capture(logic):
    """One capture holding every kind of DAC write the firmware makes."""
    with _device() as dev:
        dev.ask("STOP", timeout=3)
        dev.ask("LED 0")
        time.sleep(0.2)
        cap = logic.start_timed([D_CS, D_SDI, D_SCK, D_VCC_ON], rate=50e6, seconds=5.0)
        time.sleep(0.2)
        for cmd in SEQUENCE:
            print(f"  {cmd} -> {dev.ask(cmd)[0]}")
            time.sleep(0.4)
        logic.wait(cap)
    logic.export_digital(cap, CAP, [D_CS, D_SDI, D_SCK, D_VCC_ON])
    logic.save(cap, os.path.join(CAP, "led.sal"))
    print(f"capture in {CAP}")


def frames_of_capture():
    """Decode the capture into DAC write frames: one per CS_N low period."""
    tr = {c: read_digital_bin(os.path.join(CAP, f"digital_{c}.bin"))
          for c in (D_CS, D_SDI, D_SCK, D_VCC_ON)}
    cs, sdi, sck = tr[D_CS], tr[D_SDI], tr[D_SCK]
    falls, rises = cs.edges(False), cs.edges(True)
    out = []
    for f, r in zip(falls, rises):
        rising = sck.edges(True)
        rising = rising[(rising > f) & (rising < r)]
        # SDI is set while SCK is low and captured by the DAC on the rising edge.
        bits = sdi.level_at(rising - 5e-9)
        value = int("".join(str(int(b)) for b in bits), 2) if len(bits) == 24 else -1
        out.append({"t0": f, "t1": r, "sck": rising, "bits": bits, "value": value})
    return tr, out


def describe(frames):
    for fr in frames:
        v = fr["value"]
        cmd, code = (v >> 16) & 0xFF, (v >> 4) & 0xFFF
        per = np.diff(fr["sck"])
        print(f"  frame @{fr['t0'] * 1e3:8.3f} ms  0x{v:06X}  cmd 0x{cmd:02X} code {code:4d}"
              f"  -> {code / 4095 * 2.5 / 56 * 1000:5.2f} mA   CS low"
              f" {(fr['t1'] - fr['t0']) * 1e6:.2f} us, SCK {1 / per.mean() / 1e6:.2f} MHz")


# --- sweep -----------------------------------------------------------------------------
def sweep():
    """Use the sensor as the photometer: mean level against LED current."""
    from naneye.sources import DeviceSource

    rows = []
    with DeviceSource(clock_hz=49500000, depth=10) as src:
        print("\n".join(src.log))
        src.command("LEDMAX 20")
        src.command(f"EXP {SWEEP_ROWS_IN_RESET}")
        print(src.log[-1])
        gen = src.frames()
        images = {}
        for ma in SWEEP_MA:
            src.command(f"LEDI {ma}")
            src.command("LED 1" if ma else "LED 0")
            time.sleep(0.4)
            a = np.stack([next(gen)[1] for _ in range(6)][2:]).astype(float)
            rows.append({"ma": ma, "mean": a.mean(), "std": float(a.std()),
                         "p99": float(np.percentile(a, 99))})
            print(f"  {ma:5.1f} mA  mean {a.mean():7.2f} DN  std {a.std():6.2f}")
            if ma in (0, max(SWEEP_MA)):
                images[ma] = a.mean(axis=0)
        src.command("LED 0")
    os.makedirs(CAP, exist_ok=True)
    np.savez(os.path.join(CAP, "sweep_images.npz"),
             **{f"ma{k}": v for k, v in images.items()})
    json.dump(rows, open(SWEEP_JSON, "w"), indent=1)
    d = rows[-1]["mean"] - rows[0]["mean"]
    print(f"{d:+.2f} DN from 0 to {SWEEP_MA[-1]} mA "
          f"({d / SWEEP_MA[-1]:.2f} DN/mA)")


# --- plotting --------------------------------------------------------------------------
def plot():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(OUT, exist_ok=True)
    plt.rcParams.update({"font.size": 9, "axes.spines.top": False,
                         "axes.spines.right": False, "savefig.dpi": 110})
    C = {"cs": "#7c3aed", "sdi": "#ea580c", "sck": "#2563eb", "en": "#059669",
         "grid": "#e5e7eb", "hl": "#fde68a"}

    tr, frames = frames_of_capture()
    describe(frames)

    # ---- 1. the three writes, and one of them bit by bit ------------------------------
    fig, axes = plt.subplots(2, 1, figsize=(10, 5.4), constrained_layout=True)
    rows = [("LED_VCC_ON", tr[D_VCC_ON], C["en"]), ("CS_N", tr[D_CS], C["cs"]),
            ("SCK", tr[D_SCK], C["sck"]), ("SDI", tr[D_SDI], C["sdi"])]

    def draw(ax, t0, t1, unit, n=8000, head=0.0, foot=0.3):
        tt = np.linspace(t0, t1, n)
        for i, (name, trace, colour) in enumerate(rows):
            y = (len(rows) - 1 - i) * 1.5
            lv = trace.level_at(tt).astype(float)
            ax.fill_between((tt - t0) * unit, y, y + lv, step="post", color=colour,
                            alpha=0.18, lw=0)
            ax.step((tt - t0) * unit, y + lv, where="post", color=colour, lw=0.9)
            ax.text(-0.012, (y + 0.5 + foot) / (1.5 * len(rows) + head + foot), name,
                    transform=ax.transAxes, ha="right", va="center", fontsize=8,
                    color=colour)
        ax.set_yticks([])
        ax.set_ylim(-foot, 1.5 * len(rows) + head)
        ax.set_xlim(0, (t1 - t0) * unit)
        ax.grid(axis="x", color=C["grid"], lw=0.6)
        ax.spines["left"].set_visible(False)

    ax = axes[0]
    t0, t1 = frames[0]["t0"] - 0.25, frames[-1]["t1"] + 0.25
    draw(ax, t0, t1, 1e3, head=1.9)
    for fr, label in zip(frames, ["LED 1\nwrite+update 459\n(5.00 mA)",
                                  "LEDI 10\nwrite+update 917\n(10.00 mA)",
                                  "LED 0\npower down"]):
        x = (fr["t0"] - t0) * 1e3
        ax.axvline(x, color="#111827", lw=0.6, ls=":")
        ax.annotate(label, xy=(x, 6.0), xytext=(x + 0.02 * (t1 - t0) * 1e3, 6.15),
                    fontsize=8, color="#111827", va="bottom")
    ax.set_xlabel("ms")
    ax.set_title("Every DAC write the firmware makes: the rail is raised before the first "
                 "code and dropped after the power-down", fontsize=9.5, loc="left")

    ax = axes[1]
    fr = frames[0]
    pad = 0.4e-6
    draw(ax, fr["t0"] - pad, fr["t1"] + pad, 1e6, foot=1.25)
    t0 = fr["t0"] - pad
    names = ([f"C{i}" for i in range(8)] + [f"D{11 - i}" for i in range(8)]
             + [f"D{3 - i}" for i in range(4)] + ["x"] * 4)
    for t, b, nm in zip(fr["sck"], fr["bits"], names):
        x = (t - t0) * 1e6
        ax.text(x, -0.55, str(int(b)), ha="center", va="center", fontsize=7,
                color="#111827")
        ax.text(x, -1.0, nm, ha="center", va="center", fontsize=5.8, color="#6b7280")
    v = fr["value"]
    ax.set_xlabel("µs", labelpad=1)
    ax.set_title(f"One write, sampled on the rising edges: 0x{v:06X} = command 0x30 "
                 f"(write and update), code {(v >> 4) & 0xFFF}", fontsize=9.5, loc="left")
    fig.savefig(os.path.join(OUT, "led-dac-write.png"))
    plt.close(fig)
    print("wrote led-dac-write.png")

    # ---- 2. light against current -----------------------------------------------------
    if not os.path.exists(SWEEP_JSON):
        print("no sweep data; run `sweep` for led-response.png")
        return
    data = json.load(open(SWEEP_JSON))
    ma = np.array([r["ma"] for r in data], float)
    mean = np.array([r["mean"] for r in data], float)
    slope, offset = np.polyfit(ma, mean, 1)

    npz = os.path.join(CAP, "sweep_images.npz")
    imgs = np.load(npz) if os.path.exists(npz) else None
    ncol = 3 if imgs is not None else 1
    fig, axes = plt.subplots(1, ncol, figsize=(4.2 * ncol, 3.4), constrained_layout=True)
    axes = np.atleast_1d(axes)

    ax = axes[0]
    ax.plot(ma, mean - offset, "o", color=C["sck"], ms=5, label="measured")
    ax.plot(ma, slope * ma, "-", color=C["sdi"], lw=1,
            label=f"{slope:.2f} DN/mA (straight line)")
    ax.set_xlabel("LED current (mA)")
    ax.set_ylabel("mean level above the dark reading (DN)")
    ax.legend(frameon=False, fontsize=8)
    ax.set_title("The sensor as its own photometer", fontsize=9.5, loc="left")
    ax.grid(color=C["grid"], lw=0.6)

    if imgs is not None:
        off, on = imgs[f"ma{SWEEP_MA[0]}"], imgs[f"ma{SWEEP_MA[-1]}"]
        lo, hi = np.percentile(off, [1, 99])
        for ax, im, title in ((axes[1], on, f"{SWEEP_MA[-1]} mA"),
                              (axes[2], on - off, f"difference, {SWEEP_MA[-1]} mA − dark")):
            if "difference" in title:
                m = ax.imshow(im, cmap="magma")
            else:
                m = ax.imshow(im, cmap="gray", vmin=lo, vmax=hi)
            ax.set_title(title, fontsize=9.5, loc="left")
            ax.set_xticks([])
            ax.set_yticks([])
            fig.colorbar(m, ax=ax, fraction=0.046, label="DN")
    fig.savefig(os.path.join(OUT, "led-response.png"))
    plt.close(fig)
    print("wrote led-response.png")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", choices=["capture", "sweep", "plot", "all"])
    args = p.parse_args()
    if args.what in ("capture", "all"):
        capture(Logic())
    if args.what in ("sweep", "all"):
        sweep()
    if args.what in ("plot", "all"):
        plot()


if __name__ == "__main__":
    main()
