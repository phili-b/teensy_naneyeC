"""Live viewer.

    python -m naneye.viewer --source replay          reference frames, no hardware needed
    python -m naneye.viewer --source doc/digital.csv a Saleae capture, decoded and played
    python -m naneye.viewer --source auto            the first Teensy found
    python -m naneye.viewer --source COM7 --depth 10
    python -m naneye.viewer --source replay --snapshot shot.png   one frame, then exit

With a live device a second window, "NanEyeC controls", has sliders for exposure, frame
delay, gains and the analog settings. A slider change is sent once it has been still for
0.15 s, and takes effect on the next frame. The register panel beside the image shows both
registers decoded field by field.

Keys: q quit, s save a PNG, r toggle raw/auto contrast, h toggle the histogram,
      g toggle the register panel, d datasheet-recommended analog settings,
      SPACE pause, +/- exposure, l toggle the LED, [ / ] LED current.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from . import protocol, regs
from .sources import autoscale, open_source

LINE_H = 22
REG_PANEL_W = 360
REG_LINE_H = 20


def draw_histogram(img: np.ndarray, width: int, height: int = 90) -> np.ndarray:
    """A small log-scaled histogram strip."""
    bins = 128
    hist, _ = np.histogram(img, bins=bins, range=(0, 1024 if img.dtype == np.uint16 else 256))
    hist = np.log1p(hist.astype(np.float32))
    if hist.max() > 0:
        hist = hist / hist.max()
    panel = np.zeros((height, width), dtype=np.uint8)
    for x in range(width):
        v = hist[min(int(x / width * bins), bins - 1)]
        panel[height - int(v * (height - 1)):, x] = 200
    return panel


def status_lines(header: protocol.Header, img: np.ndarray, fps: float, gaps: int) -> list:
    saturated = float((img >= (1020 if img.dtype == np.uint16 else 255)).mean() * 100)
    return [
        f"frame {header.frame_counter}  {fps:4.1f} fps  {header.format_name}",
        f"min {int(img.min())}  max {int(img.max())}  mean {img.mean():6.1f}"
        f"  sat {saturated:.2f}%",
        f"exp {header.exposure_us() / 1000:6.2f} ms  sclk {header.sclk_hz / 1e6:.3f} MHz"
        f"  cfg 0x{header.cfg0:04X}/0x{header.cfg1:04X}",
        f"dropped {header.frames_dropped}  counter gaps {gaps}"
        f"  rows_failed {header.rows_failed}  concealed {header.pixels_concealed}"
        + ("  SYNC LOST" if header.sync_lost else ""),
    ]


def draw_registers(header: protocol.Header, height: int) -> np.ndarray:
    """Side panel: CONFIG_0 and CONFIG_1 decoded field by field, in fixed columns
    (OpenCV's font is proportional, so columns are drawn separately)."""
    import cv2

    panel = np.full((height, REG_PANEL_W, 3), 24, dtype=np.uint8)
    font, size = cv2.FONT_HERSHEY_SIMPLEX, 0.42
    y = 20

    def text(s, x, colour):
        cv2.putText(panel, s, (x, y), font, size, colour, 1, cv2.LINE_AA)

    last_reg = None
    for reg, name, value, meaning, editable, differs in regs.rows(
            header.cfg0, header.cfg1, header.sclk_hz or None):
        if reg != last_reg:
            if last_reg is not None:
                y += 6
            text(f"CONFIG_{reg}   0x{(header.cfg1 if reg else header.cfg0):04X}", 8,
                 (120, 200, 255))
            y += REG_LINE_H
            last_reg = reg
        colour = (220, 220, 220) if editable else (130, 130, 130)
        text(name, 16, colour)
        text(str(value), 170, colour)
        text(meaning + ("  (fw)" if not editable else ""), 205, colour)
        if differs:
            text("*", 160, (0, 200, 255))
        y += REG_LINE_H
    y += 10
    text("*  not the datasheet's recommended value", 8, (0, 200, 255))
    y += REG_LINE_H
    text("(fw) set by the firmware; grey = read-only", 8, (130, 130, 130))
    return panel


def compose(header: protocol.Header, img: np.ndarray, scale: int = 2,
            raw_mode: bool = False, show_hist: bool = True, fps: float = 0.0,
            gaps: int = 0, show_regs: bool = True) -> np.ndarray:
    """Build the full display image: frame, optional register panel, status bar and
    optional histogram.

    Shared by the live loop and --snapshot so the two cannot disagree.
    """
    import cv2

    if raw_mode:
        disp = img.astype(np.uint8) if img.dtype == np.uint8 else (img >> 2).astype(np.uint8)
    else:
        disp = autoscale(img)
    view = cv2.resize(disp, (img.shape[1] * scale, img.shape[0] * scale),
                      interpolation=cv2.INTER_NEAREST)
    view = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR)
    if show_regs:
        view = np.hstack([view, draw_registers(header, view.shape[0])])

    lines = status_lines(header, img, fps, gaps)
    bar = np.zeros((LINE_H * len(lines) + 8, view.shape[1], 3), dtype=np.uint8)
    for i, text in enumerate(lines):
        colour = (0, 0, 255) if "SYNC LOST" in text else (220, 220, 220)
        cv2.putText(bar, text, (8, 18 + i * LINE_H), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    colour, 1, cv2.LINE_AA)

    panels = [view, bar]
    if show_hist:
        panels.append(cv2.cvtColor(draw_histogram(img, view.shape[1]), cv2.COLOR_GRAY2BGR))
    return np.vstack(panels)


class Controls:
    """Sliders for the editable register fields, in their own window.

    The device stays the source of truth: sliders start from the first frame header's
    registers, firmware-owned fields are always taken from the latest header, and a change
    is sent only once the slider has been still for SETTLE_S, so dragging does not flood
    the link with writes.
    """

    WINDOW = "NanEyeC controls"
    SETTLE_S = 0.15
    # (trackbar label, field, inverted). Exposure is inverted so that right = longer.
    # Labels are short because HighGUI truncates long trackbar names.
    SLIDERS = (
        ("exposure", "rows_in_reset", True),
        ("delay", "rows_delay", False),
        ("ramp gain", "ramp_gain", False),
        ("CDS gain", "cds_gain", False),
        ("vrst", "vrst_pix", False),
        ("offset", "offset_ramp", False),
        ("vref", "vref", False),
        ("cvc", "cvc_curr", False),
        ("drive", "output_curr", False),
        ("bias+", "bias_curr_increase", False),
    )
    READOUT_W = 520
    READOUT_LINE_H = 22

    def __init__(self, source, header: protocol.Header):
        import cv2

        self.cv2 = cv2
        self.source = source
        self.device_cfg = (header.cfg0, header.cfg1)
        self.sent = self.device_cfg
        self.pending = None
        self.changed_at = 0.0
        # AUTOSIZE: the readout is drawn at its own size rather than stretched to fill.
        cv2.namedWindow(self.WINDOW, cv2.WINDOW_AUTOSIZE)
        values = regs.unpack(*self.device_cfg)
        for label, name, inverted in self.SLIDERS:
            f = regs.BY_NAME[name]
            top = regs.ROWS_IN_RESET_MAX if name == "rows_in_reset" else f.max
            pos = top - values[name] if inverted else values[name]
            cv2.createTrackbar(label, self.WINDOW, pos, top, lambda _v: None)
        self.render(header)

    def render(self, header: protocol.Header) -> None:
        """Below the sliders: what each one is set to, in real units, as sent."""
        cv2 = self.cv2
        c0, c1 = self.wanted()
        values = regs.unpack(c0, c1)
        n = len(self.SLIDERS)
        img = np.full((self.READOUT_LINE_H * (n + 2), self.READOUT_W, 3), 240, np.uint8)
        pending = (c0, c1) != (header.cfg0, header.cfg1)
        title = "sending..." if pending else "in force on the sensor"
        cv2.putText(img, title, (10, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (0, 120, 220) if pending else (0, 130, 0), 1, cv2.LINE_AA)
        for i, (label, name, _inv) in enumerate(self.SLIDERS):
            f = regs.BY_NAME[name]
            v = values[name]
            if name == "rows_in_reset" and header.sclk_hz:
                meaning = (f"{regs.exposure_ms(v, values['rows_delay'], header.sclk_hz):.1f} ms"
                           f"   (rows_in_reset {v})")
            elif name == "rows_delay" and header.sclk_hz:
                meaning = (f"{1e3 / regs.frame_period_ms(v, header.sclk_hz):.1f} fps max"
                           f"   (rows_delay {v})")
            else:
                meaning = f.label(v) if f.labels else str(v)
                if f.recommended is not None and v != f.recommended:
                    meaning += f"   (recommended {f.label(f.recommended)})"
            y = 17 + (i + 1) * self.READOUT_LINE_H
            cv2.putText(img, label, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 40, 40), 1,
                        cv2.LINE_AA)
            cv2.putText(img, meaning, (110, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (40, 40, 40),
                        1, cv2.LINE_AA)
        cv2.imshow(self.WINDOW, img)

    def _field_value(self, label, name, inverted):
        pos = self.cv2.getTrackbarPos(label, self.WINDOW)
        return regs.ROWS_IN_RESET_MAX - pos if inverted else pos

    def wanted(self) -> tuple[int, int]:
        values = {name: self._field_value(label, name, inv)
                  for label, name, inv in self.SLIDERS}
        return regs.pack(values, *self.device_cfg)

    def set_field(self, name: str, value: int) -> None:
        for label, n, inverted in self.SLIDERS:
            if n == name:
                pos = regs.ROWS_IN_RESET_MAX - value if inverted else value
                self.cv2.setTrackbarPos(label, self.WINDOW, int(pos))

    def set_all(self, cfg0: int, cfg1: int) -> None:
        values = regs.unpack(cfg0, cfg1)
        for _label, name, _inv in self.SLIDERS:
            self.set_field(name, values[name])

    def value(self, name: str) -> int:
        for label, n, inverted in self.SLIDERS:
            if n == name:
                return self._field_value(label, n, inverted)
        raise KeyError(name)

    def poll(self, header: protocol.Header, now: float) -> None:
        """Track the device's registers and send settled slider changes."""
        self.device_cfg = (header.cfg0, header.cfg1)
        self.render(header)
        want = self.wanted()
        if want != self.pending:
            self.pending = want
            self.changed_at = now
            return
        if want == self.sent or now - self.changed_at < self.SETTLE_S:
            return
        if want[0] != self.sent[0]:
            self.source.command(f"REG 0 0x{want[0]:04X}")
        if want[1] != self.sent[1]:
            self.source.command(f"REG 1 0x{want[1]:04X}")
        self.sent = want


def main(argv=None):
    import cv2

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="replay",
                    help="'replay', 'auto', a COM port, a .csv capture, or a "
                         "recorded stream file")
    ap.add_argument("--depth", type=int, default=10, choices=(10, 12))
    ap.add_argument("--clock", type=int, default=49500000,
                    help="SCLK: 49500000 (default, ~34 fps), 24750000 or 12375000")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--fps", type=float, default=19.3, help="replay rate")
    ap.add_argument("--snapshot", metavar="PATH",
                    help="write one composed frame to PATH and exit (no window)")
    ap.add_argument("--no-regs", action="store_true",
                    help="start without the register panel (toggle with g)")
    args = ap.parse_args(argv)

    fmt = {8: protocol.FMT_GRAY8, 10: protocol.FMT_GRAY10,
           12: protocol.FMT_RAW12}[args.depth]
    source = open_source(args.source, depth=args.depth, clock_hz=args.clock, fmt=fmt,
                         fps=args.fps)
    print(f"source: {source.name}")
    for line in getattr(source, "log", []):
        print(f"  {line}")

    if args.snapshot:
        with source:
            # Skip a couple of frames so the reported rate is representative.
            for n, (header, img) in enumerate(source.frames()):
                if n < 2:
                    continue
                shot = compose(header, img, scale=args.scale, fps=args.fps,
                               show_regs=not args.no_regs)
                cv2.imwrite(args.snapshot, shot)
                print(f"wrote {args.snapshot} ({shot.shape[1]}x{shot.shape[0]})")
                print(f"  {header.describe()}")
                return 0
        print("no frames received")
        return 1

    window = "NanEyeC"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    sized = False  # the window is fitted to the first composite, panels included

    raw_mode = False
    show_hist = True
    show_regs = not args.no_regs
    paused = False
    controls = None
    live = hasattr(source, "device")  # only a live device takes register writes
    led_on = False
    led_ma = 5.0
    saved = 0

    times = []
    last_counter = None
    gaps = 0

    with source:
        for header, img in source.frames():
            if last_counter is not None:
                gaps += max(0, header.frame_counter - last_counter - 1)
            last_counter = header.frame_counter

            now = time.time()
            if live and controls is None:
                controls = Controls(source, header)
            if controls is not None:
                controls.poll(header, now)
            times.append(now)
            times[:] = [t for t in times if now - t < 2.0]
            fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 else 0.0

            if not paused:
                composite = compose(header, img, scale=args.scale, raw_mode=raw_mode,
                                    show_hist=show_hist, fps=fps, gaps=gaps,
                                    show_regs=show_regs)
                if not sized:
                    cv2.resizeWindow(window, composite.shape[1], composite.shape[0])
                    sized = True
                cv2.imshow(window, composite)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q") or key == 27:
                break
            elif key == ord("s"):
                name = f"frame_{header.frame_counter:06d}.png"
                cv2.imwrite(name, autoscale(img))
                saved += 1
                print(f"saved {name}")
            elif key == ord("r"):
                raw_mode = not raw_mode
            elif key == ord("h"):
                show_hist = not show_hist
                sized = False
            elif key == ord("g"):
                show_regs = not show_regs
                sized = False
            elif key == ord("d") and controls is not None:
                controls.set_all(*regs.recommended(header.cfg0, header.cfg1))
            elif key == ord(" "):
                paused = not paused
            elif key in (ord("+"), ord("="), ord("-")) and controls is not None:
                # Longer exposure means fewer rows in reset.
                step = -8 if key != ord("-") else 8
                rir = controls.value("rows_in_reset") + step
                controls.set_field("rows_in_reset",
                                   max(0, min(regs.ROWS_IN_RESET_MAX, rir)))
            elif key == ord("l"):
                led_on = not led_on
                source.command(f"LEDI {led_ma}")
                source.command(f"LED {1 if led_on else 0}")
            elif key == ord("]"):
                led_ma = min(20.0, led_ma + 1.0)
                source.command(f"LEDI {led_ma}")
            elif key == ord("["):
                led_ma = max(0.0, led_ma - 1.0)
                source.command(f"LEDI {led_ma}")

    cv2.destroyAllWindows()
    if saved:
        print(f"{saved} image(s) saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
