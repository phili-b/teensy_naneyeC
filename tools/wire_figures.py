"""Capture the SEIM link on the Saleae and draw the documentation's "on the wire" figures.

    uv run --with matplotlib python tools/wire_figures.py capture   # the three captures
    uv run --with matplotlib python tools/wire_figures.py plot      # figures from them

Three captures, all driven through the Teensy and the Logic 2 MCP server:

  startup  START at 12.375 MHz, all 7 channels at 250 MS/s (20 samples per bit), triggered
           on NanEye_EN: power-up, configuration, run-in, release, sampling calibration,
           row lock, the discarded first frame, and two streaming frames
  eye      streaming at 49.5 MHz, SCLK and SDAT at the Teensy at the highest rate the
           Logic Pro allows, for where SDAT's edges fall within a bit
  power    NanEye_EN digital and VCC_SENSOR analog, while the sensor is switched on and off

and the figures, into docs/images/wire/. Channel map as in docs/hardware.md: D0 SDAT at
Teensy pin 1, D1 NanEye_EN, D2 SDAT at pin 26, D3 SCLK at pin 27, D4 VCC_SENSOR, D5 SDAT at
the sensor, D6 SCLK at the sensor.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

from naneye import decode  # noqa: E402
from naneye.saleae import Logic, read_analog_bin, read_digital_bin  # noqa: E402

CAP = os.path.join(ROOT, "build", "wire")
OUT = os.path.join(ROOT, "docs", "images", "wire")
D_SDI, D_EN, D_SDO, D_SCLK, D_VCC, D_SDAT_B, D_SCLK_B = 0, 1, 2, 3, 4, 5, 6


# --- capture ---------------------------------------------------------------------------
def _device():
    from naneye.transport import Device

    dev = Device.open_first()
    time.sleep(0.3)
    dev.serial.reset_input_buffer()
    dev.ask("STOP", timeout=3)
    dev.ask("CAL 1")
    dev.ask("DEPTH 10")
    return dev


def capture_startup(logic):
    with _device() as dev:
        dev.ask("CLK 12375000")
        dev.ask("POWER 0")
        time.sleep(1.2)  # so START does not wait out the power-off time inside the capture
        cap = logic.start_triggered([0, 1, 2, 3, 4, 5, 6], rate=250e6, trigger=D_EN,
                                    after_s=0.45)
        time.sleep(1.0)
        print("\n".join(dev.ask("START", timeout=10)))
        logic.wait(cap)
        dev.ask("STOP", timeout=3)
        dev.ask("POWER 0")
    logic.export_digital(cap, os.path.join(CAP, "startup"), [0, 1, 2, 3, 4, 5, 6])
    logic.save(cap, os.path.join(CAP, "startup.sal"))
    print(f"startup capture {cap}")


def capture_eye(logic):
    with _device() as dev:
        dev.ask("CLK 49500000")
        print("\n".join(dev.ask("START", timeout=10)))
        time.sleep(0.5)
        cap = None
        for rate in (500e6, 250e6):
            try:
                cap = logic.start_timed([D_SDI, D_SCLK, D_SDAT_B, D_SCLK_B], rate=rate,
                                        seconds=0.06)
                print(f"eye capture at {rate / 1e6:.0f} MS/s")
                break
            except Exception as ex:  # noqa: BLE001 - try the next rate
                print(f"  {rate / 1e6:.0f} MS/s refused: {str(ex)[:80]}")
        logic.wait(cap)
        dev.ask("STOP", timeout=3)
        dev.ask("POWER 0")
    logic.export_digital(cap, os.path.join(CAP, "eye"), [D_SDI, D_SCLK, D_SDAT_B, D_SCLK_B])


def capture_power(logic):
    with _device() as dev:
        dev.ask("POWER 0")
        time.sleep(1.5)
        cap = logic.start_timed([D_EN], rate=6.25e6, seconds=2.0, analog=[D_VCC],
                                analog_rate=1.5625e6)
        time.sleep(0.3)
        dev.ask("POWER 1")
        time.sleep(0.5)
        dev.ask("POWER 0")
        logic.wait(cap)
    logic.export_digital(cap, os.path.join(CAP, "power_d"), [D_EN])
    logic.export_analog(cap, os.path.join(CAP, "power_a"), [D_VCC])


# --- helpers for plotting --------------------------------------------------------------
def load(name, channels):
    d = os.path.join(CAP, name)
    return {c: read_digital_bin(os.path.join(d, f"digital_{c}.bin")) for c in channels}


def bursts(sr, gap=150e-9):
    """Clock bursts from rising-edge times: (first edge index, number of clocks)."""
    breaks = np.flatnonzero(np.diff(sr) > gap)
    starts = np.concatenate([[0], breaks + 1])
    lens = np.diff(np.concatenate([starts, [len(sr)]]))
    return starts, lens


def teensy_bits(trace, sr):
    """Bits the Teensy drives: stable across the rising edge the sensor samples on."""
    return trace.level_at(sr).astype(np.uint8)


def sensor_bits(trace, sr):
    """Bits the sensor drives: launched after rising edge k, read just before edge k+1."""
    return trace.level_at(np.append(sr[1:], sr[-1] + 1e-6) - 2e-9).astype(np.uint8)


def word(bits):
    return int("".join(str(int(b)) for b in bits), 2)


# --- plotting --------------------------------------------------------------------------
def plot_all():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    os.makedirs(OUT, exist_ok=True)
    plt.rcParams.update({"font.size": 9, "axes.spines.top": False,
                         "axes.spines.right": False, "savefig.dpi": 110})
    C = {"sclk": "#2563eb", "teensy": "#ea580c", "sensor": "#059669", "en": "#6b7280",
         "vcc": "#dc2626", "hl": "#fde68a", "hl2": "#bfdbfe", "hl3": "#bbf7d0",
         "grid": "#e5e7eb"}

    tr = load("startup", [0, 1, 2, 3, 5, 6])
    t_en = tr[D_EN].edges(True)[0]
    sr = tr[D_SCLK].edges(True)
    starts, lens = bursts(sr)

    def draw_traces(ax, t0, t1, rows, n=6000, unit=1e6, label_x=None):
        """Logic-analyser style: one step trace per row, labelled on the left."""
        tt = np.linspace(t0, t1, n)
        for i, (name, trace, colour) in enumerate(rows):
            y = (len(rows) - 1 - i) * 1.5
            lv = trace.level_at(tt).astype(float)
            ax.fill_between((tt - t0) * unit, y, y + lv, step="post", color=colour,
                            alpha=0.18, lw=0)
            ax.step((tt - t0) * unit, y + lv, where="post", color=colour, lw=0.9)
            ax.text(-0.01, (y + 0.5) / (1.5 * len(rows)), name, transform=ax.transAxes,
                    ha="right", va="center", fontsize=8, color=colour)
        ax.set_yticks([])
        ax.set_ylim(-0.3, 1.5 * len(rows))
        ax.set_xlim(0, (t1 - t0) * unit)
        ax.grid(axis="x", color=C["grid"], lw=0.6)
        ax.spines["left"].set_visible(False)
        return len(rows)

    # ---- 1. start-up timeline ---------------------------------------------------------
    # classify bursts
    slow = [(s, n) for s, n in zip(starts, lens) if n <= 2]
    w24 = [(s, n) for s, n in zip(starts, lens) if n == 24]
    cal = [(s, n) for s, n in zip(starts, lens) if n == 1024]
    rows3936 = [(s, n) for s, n in zip(starts, lens) if n == 3936]
    last_pp = [(s, n) for s, n in zip(starts, lens) if n == 12]
    t = lambda idx: (sr[idx] - t_en) * 1e3  # noqa: E731 - ms after EN
    bb_end = slow[-1][0] if slow else 0
    idle_off = w24[0][0]                         # first LPSPI-speed write pair
    cal_start, cal_end = cal[0][0], cal[-1][0] + 1024
    # rows before the first streaming interface window belong to the start-up
    stream_if = w24[2][0] if len(w24) > 2 else len(sr) - 1
    startup_rows = [s for s, _ in rows3936 if s < stream_if]
    lock_end = startup_rows[-1] + 3936 if startup_rows else cal_end
    phases = [
        ("power on, 5 ms settle", 0.0, t(0)),
        ("activation clock + idle-on writes\n(bit-banged, ~1 MHz)", t(0), t(bb_end)),
        ("run-in: one frame of clocks,\nSDAT held low", t(bb_end), t(idle_off)),
        ("idle-off writes + interface window", t(idle_off), t(idle_off + 7776)),
        ("sampling-point calibration\n(4 x 1024 bits of training)", t(cal_start), t(cal_end)),
        ("presence check, row lock,\n8-row check", t(cal_end), t(lock_end)),
        ("rest of first frame clocked\nand discarded (overexposed)", t(lock_end), t(stream_if)),
    ]
    frames = []
    for k in range(2, len(w24), 2):
        s_if = w24[k][0]
        s_next = w24[k + 2][0] if k + 2 < len(w24) else len(sr) - 1
        frames.append((t(s_if), t(s_next)))
    fig, ax = plt.subplots(figsize=(11, 4.2), constrained_layout=True)
    colours = ["#9ca3af", "#f59e0b", "#a78bfa", "#fb923c", "#34d399", "#60a5fa", "#d1d5db"]
    for i, (name, a, b) in enumerate(phases):
        ax.barh(len(phases) + 1 - i, max(b - a, 0.25), left=a, color=colours[i],
                edgecolor="none", height=0.7)
        ax.text(b + 1.5, len(phases) + 1 - i, name, va="center", fontsize=8)
    for j, (a, b) in enumerate(frames):
        ax.barh(0.5, b - a, left=a, color="#2563eb" if j % 2 == 0 else "#60a5fa",
                height=0.7, edgecolor="white")
        ax.text((a + b) / 2, 0.5, f"frame {j + 1}", ha="center", va="center",
                color="white", fontsize=8)
    ax.text(frames[0][0] if frames else 0, 1.35, "streaming: interface window, sync, "
            "320 rows, EOF", fontsize=8, color="#1e3a8a")
    ax.set_yticks([])
    ax.set_xlabel("ms after NanEye_EN rises")
    ax.set_xlim(-2, (sr[-1] - t_en) * 1e3 + 2)
    ax.set_title("START at 12.375 MHz, as captured: what the Teensy does, phase by phase",
                 loc="left", fontsize=10, fontweight="bold")
    ax.grid(axis="x", color=C["grid"], lw=0.6)
    ax.spines["left"].set_visible(False)
    fig.savefig(os.path.join(OUT, "startup-timeline.png"))
    plt.close(fig)
    print("startup-timeline.png")

    # ---- 2. a register write, bit-banged ----------------------------------------------
    # The idle-on CONFIG_1 write: clocks 26..49 of the bit-banged group (after the
    # activation clock and the CONFIG_0 write).
    k0 = 1 + 24
    bits = teensy_bits(tr[D_SDO], sr[k0:k0 + 24])
    value = word(bits[7:23])
    t0, t1 = sr[k0] - 0.8e-6, sr[k0 + 23] + 1.4e-6
    fig, ax = plt.subplots(figsize=(11, 2.9), constrained_layout=True)
    draw_traces(ax, t0, t1, [("SCLK", tr[D_SCLK], C["sclk"]),
                             ("SDAT (Teensy)", tr[D_SDO], C["teensy"]),
                             ("SDAT (sensor)", tr[D_SDAT_B], C["sensor"])])
    groups = [(0, 4, "1001: write", C["hl"]), (4, 7, "addr 001", C["hl2"]),
              (7, 23, f"CONFIG_1 = 0x{value:04X}", C["hl3"]), (23, 24, "0", C["hl"])]
    for a, b, label, colour in groups:
        xa = (sr[k0 + a] - 0.5e-6 - t0) * 1e6
        xb = (sr[k0 + b - 1] + 0.5e-6 - t0) * 1e6
        ax.add_patch(Rectangle((xa, 4.6), xb - xa, 0.7, color=colour, lw=0))
        ax.text((xa + xb) / 2, 4.95, label, ha="center", va="center", fontsize=8)
    for i, b in enumerate(bits):
        ax.text((sr[k0 + i] - t0) * 1e6, 3.95, str(int(b)), ha="center", fontsize=7,
                color=C["teensy"])
    ax.set_ylim(-0.3, 5.5)
    ax.set_xlabel("µs")
    ax.set_title(f"A register write at start-up: 24 bits, bit-banged at ~1 MHz. The sensor "
                 f"reads SDAT on each rising SCLK edge (idle-on CONFIG_1 = 0x{value:04X})",
                 loc="left", fontsize=10, fontweight="bold")
    fig.savefig(os.path.join(OUT, "register-write.png"))
    plt.close(fig)
    print("register-write.png", hex(value))

    # ---- 3. release, calibration and the training pattern --------------------------------
    c0 = cal[0][0]
    t0 = sr[c0] - 3e-6
    t1 = sr[cal[-1][0] + 1023] + 3e-6
    fig = plt.figure(figsize=(11, 4.6), constrained_layout=True)
    gs = fig.add_gridspec(2, 1, height_ratios=[1, 1.1])
    ax = fig.add_subplot(gs[0])
    draw_traces(ax, t0, t1, [("SCLK", tr[D_SCLK], C["sclk"]),
                             ("SDAT (Teensy pin 1)", tr[D_SDI], C["sensor"])], n=20000)
    names = ["rising", "falling", "rising + delay", "falling + delay"]
    for (s, _), name in zip(cal, names):
        xa = (sr[s] - t0) * 1e6
        xb = (sr[s + 1023] - t0) * 1e6
        ax.annotate("", xy=(xa, 3.2), xytext=(xb, 3.2),
                    arrowprops=dict(arrowstyle="<->", color="#374151", lw=0.8))
        ax.text((xa + xb) / 2, 3.45, f"1024 bits, sampled on {name}", ha="center",
                fontsize=8)
    ax.set_ylim(-0.3, 3.8)
    ax.set_xlabel("µs")
    ax.set_title("Sampling-point calibration: four bursts of the sensor's training pattern, "
                 "one per sampling point (gaps: the SPI being reconfigured)", loc="left",
                 fontsize=10, fontweight="bold")
    ax2 = fig.add_subplot(gs[1])
    z0 = sr[c0 + 200]
    z1 = sr[c0 + 224]
    draw_traces(ax2, z0 - 20e-9, z1, [("SCLK", tr[D_SCLK], C["sclk"]),
                                      ("SDAT (sensor)", tr[D_SDAT_B], C["sensor"]),
                                      ("SDAT (Teensy pin 1)", tr[D_SDI], C["sensor"])],
                unit=1e9)
    bits = sensor_bits(tr[D_SDI], sr[c0 + 200:c0 + 224])
    for i, b in enumerate(bits):
        ax2.text((sr[c0 + 200 + i] - z0 + 20e-9 + 40e-9) * 1e9, 4.6, str(int(b)),
                 ha="center", fontsize=8, color=C["sensor"])
    ax2.set_ylim(-0.3, 5.0)
    ax2.set_xlabel("ns")
    ax2.set_title("Zoomed: the training pattern alternates every clock, 12-bit words "
                  "0x555 / 0xAAA. The sensor changes SDAT just after each rising edge.",
                  loc="left", fontsize=10, fontweight="bold")
    fig.savefig(os.path.join(OUT, "calibration.png"))
    plt.close(fig)
    print("calibration.png")

    # ---- 4. one row: 8 training words, then pixels -------------------------------------
    # The first readout row of the first streaming frame.
    stream_rows = [s for s, _ in rows3936 if s > stream_if]
    r0 = stream_rows[0]
    bits = sensor_bits(tr[D_SDI], sr[r0:r0 + 3936])
    words = [word(bits[i * 12:(i + 1) * 12]) for i in range(328)]
    n_show = 12  # 8 training + 4 pixels
    t0 = sr[r0] - 60e-9
    t1 = sr[r0 + 12 * n_show] + 40e-9
    fig, ax = plt.subplots(figsize=(11, 3.2), constrained_layout=True)
    draw_traces(ax, t0, t1, [("SCLK", tr[D_SCLK], C["sclk"]),
                             ("SDAT (Teensy pin 1)", tr[D_SDI], C["sensor"])], n=12000)
    for i in range(n_show):
        xa = (sr[r0 + 12 * i] + 20e-9 - t0) * 1e6
        xb = (sr[r0 + 12 * i + 11] + 60e-9 - t0) * 1e6
        w = words[i]
        if i < 8:
            label, colour = f"0x{w:03X}", C["hl2"]
        else:
            label, colour = f"{(w >> 1) & 0x3FF} DN", C["hl3"]
        ax.add_patch(Rectangle((xa, 3.15), xb - xa, 0.7, color=colour, lw=0))
        ax.text((xa + xb) / 2, 3.5, label, ha="center", va="center", fontsize=7.5)
    ax.text(0.0, 4.15, "8 training words (0x555)", fontsize=8, color="#1e40af")
    ax.text((sr[r0 + 96] - t0) * 1e6, 4.15, "pixel words: start bit 1, 10 data bits, "
            "stop bit 0", fontsize=8, color="#065f46")
    ax.set_ylim(-0.3, 4.6)
    ax.set_xlabel("µs")
    ax.set_title("Start of a row: 8 training words, then pixels. The first pixel's start "
                 "bit breaks the alternation (two 1s): that is how rows are found.",
                 loc="left", fontsize=10, fontweight="bold")
    fig.savefig(os.path.join(OUT, "row-start.png"))
    plt.close(fig)
    print("row-start.png", [hex(w) for w in words[:12]])

    # ---- 5. the interface window between frames -----------------------------------------
    k = w24[2][0]                     # first streaming interface window
    prev_row = max(s for s, _ in rows3936 if s < k)
    eof = prev_row + 3936             # EOF clocks follow the last row
    lp = min((s for s, _ in last_pp if s > k), default=None)
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.3), constrained_layout=True,
                             gridspec_kw={"width_ratios": [1.3, 1.3, 1.0]})
    spans = [
        (sr[eof - 30] - 50e-9, sr[k + 30], "end of the last row, EOF (8 zero words), then "
                                           "the Teensy writes CONFIG_0"),
        (sr[k + 18], sr[k + 60], "CONFIG_0 → CONFIG_1 → zeros"),
        (sr[lp - 20], sr[lp + 12 + 40], "last pixel period left to the sensor, "
                                        "then SYNC training"),
    ]
    for ax, (a, b, title) in zip(axes, spans):
        draw_traces(ax, a, b, [("SCLK", tr[D_SCLK], C["sclk"]),
                               ("SDAT Teensy (pin 26)", tr[D_SDO], C["teensy"]),
                               ("SDAT at sensor", tr[D_SDAT_B], C["sensor"])], n=6000)
        ax.set_title(title, loc="left", fontsize=8.5)
        ax.set_xlabel("µs")
    fig.suptitle("The interface window between frames: 648 pixel periods in which the "
                 "Teensy drives SDAT and writes both registers", x=0.01, ha="left",
                 fontsize=10, fontweight="bold")
    fig.savefig(os.path.join(OUT, "interface-window.png"))
    plt.close(fig)
    print("interface-window.png")

    # ---- 6. sampling eye at 49.5 MHz ------------------------------------------------------
    ey = load("eye", [D_SDI, D_SCLK, D_SDAT_B, D_SCLK_B])
    esr = ey[D_SCLK].edges(True)
    period = np.median(np.diff(esr)[np.diff(esr) < 100e-9])
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.4), constrained_layout=True)
    for ax, (ch_d, ch_c, where) in zip(axes, ((D_SDI, D_SCLK, "at the Teensy (pin 1, pin 27)"),
                                              (D_SDAT_B, D_SCLK_B, "at the sensor"))):
        csr = ey[ch_c].edges(True)
        e = ey[ch_d].times
        e = e[(e > csr[0]) & (e < csr[-1])]
        idx = np.searchsorted(csr, e) - 1
        ok = idx >= 0
        ph = (e[ok] - csr[idx[ok]]) * 1e9
        ph = ph[ph < period * 1e9 * 1.2]
        ax.hist(ph, bins=np.arange(0, period * 1e9 + 2, 1), color=C["sensor"], alpha=0.8,
                label="SDAT edges")
        top = ax.get_ylim()[1]
        T = period * 1e9
        for x, name, colour in ((T, "rising edge\n(79 % bad)", "#dc2626"),
                                (T / 2, "falling edge\n(0 bad)", "#059669"),
                                (T / 2 + 1e9 / 99e6, "falling\n+ delay", "#9ca3af"),
                                (1e9 / 99e6, "rising + delay\n(next bit)", "#9ca3af")):
            if ch_d == D_SDI:
                ax.axvline(x, color=colour, lw=1.4, ls="--")
                ax.text(x + 0.3, top * 0.95, name, fontsize=7.5, va="top", color=colour)
        ax.set_xlim(0, T + 1)
        ax.set_xlabel(f"ns after the SCLK rising edge (bit = {T:.1f} ns)")
        ax.set_title(f"SDAT transitions {where}", loc="left", fontsize=9)
        ax.set_yticks([])
    fig.suptitle("Why 49.5 MHz samples on the falling edge: SDAT changes near where the "
                 "rising edge would sample it", x=0.01, ha="left", fontsize=10,
                 fontweight="bold")
    fig.savefig(os.path.join(OUT, "sampling-eye.png"))
    plt.close(fig)
    print("sampling-eye.png", f"period {period * 1e9:.2f} ns")

    # ---- 7. sensor power ------------------------------------------------------------------
    pd = read_digital_bin(os.path.join(CAP, "power_d", f"digital_{D_EN}.bin"))
    pa = read_analog_bin(os.path.join(CAP, "power_a", f"analog_{D_VCC}.bin"))
    on, off = pd.edges(True)[0], pd.edges(False)[-1]
    tv = pa.times()
    v = pa.samples
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.2), constrained_layout=True,
                             gridspec_kw={"width_ratios": [1, 2]})
    m = (tv > on - 0.2e-3) & (tv < on + 1.5e-3)
    axes[0].plot((tv[m] - on) * 1e3, v[m], color=C["vcc"])
    axes[0].axvline(0, color=C["en"], ls="--", lw=1)
    axes[0].set_xlabel("ms after NanEye_EN rises")
    axes[0].set_ylabel("VCC_SENSOR (V)")
    axes[0].set_title("switching on: the LDO settles in a fraction of a ms", loc="left",
                      fontsize=9)
    m = (tv > off - 5e-3) & (tv < off + 1.2)
    axes[1].plot((tv[m] - off) * 1e3, v[m], color=C["vcc"])
    axes[1].axvline(0, color=C["en"], ls="--", lw=1)
    axes[1].axvline(1000, color="#059669", ls=":", lw=1.2)
    axes[1].text(1010, 1.6, "the firmware waits\n1 s before powering\nup again", fontsize=8,
                 color="#059669")
    for thr in (0.5, 0.1):
        hit = np.flatnonzero((tv > off) & (v < thr))
        if len(hit):
            x = (tv[hit[0]] - off) * 1e3
            axes[1].annotate(f"< {thr} V after {x:.0f} ms", xy=(x, thr),
                             xytext=(x + 120, thr + 0.6), fontsize=8,
                             arrowprops=dict(arrowstyle="->", lw=0.8))
    axes[1].set_xlabel("ms after NanEye_EN falls")
    axes[1].set_title("switching off: nothing discharges the rail, so it takes ~0.6 s",
                      loc="left", fontsize=9)
    for ax in axes:
        ax.grid(color=C["grid"], lw=0.6)
    fig.savefig(os.path.join(OUT, "sensor-power.png"))
    plt.close(fig)
    print("sensor-power.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("capture", "plot", "all"))
    ap.add_argument("--only", choices=("startup", "eye", "power"))
    args = ap.parse_args()
    if args.what in ("capture", "all"):
        logic = Logic()
        os.makedirs(CAP, exist_ok=True)
        for name, fn in (("startup", capture_startup), ("eye", capture_eye),
                         ("power", capture_power)):
            if args.only in (None, name):
                fn(logic)
    if args.what in ("plot", "all"):
        plot_all()


if __name__ == "__main__":
    main()
