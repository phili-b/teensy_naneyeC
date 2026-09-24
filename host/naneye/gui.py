"""NanEyeC camera GUI (PyQt6).

    uv run python -m naneye.gui                        the first Teensy found, 49.5 MHz
    uv run python -m naneye.gui --source replay        reference frames, no hardware needed
    uv run python -m naneye.gui --snapshot gui.png     write a screenshot after a few frames

Frames are read on a background thread as fast as the device sends them; the window paints
the latest one at up to 60 Hz. The two rates are shown separately: "received" is what came
over USB intact, "displayed" is what was painted. If painting falls behind, frames are
skipped for display only -- none are lost from reception.

Keys: Space pause, S save frame, + / - exposure, R datasheet-recommended analog settings,
      L LED on/off, [ / ] LED current -/+ 1 mA, Q quit.
"""

from __future__ import annotations

import argparse
import collections
import os
import threading
import time

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from . import color, isp as isp_mod, protocol, regs
from .accounting import FrameAccounting
from .sources import _decode, open_source

CLOCKS = ((49500000, "49.5 MHz  (~35 fps)"), (24750000, "24.75 MHz  (~18 fps)"),
          (12375000, "12.375 MHz  (~8 fps)"))
SETTLE_S = 0.15          # a slider must be still this long before its value is sent
NO_FRAMES_S = 2.0        # this long without a frame: show NO FRAMES
RESTART_AFTER_S = 5.0    # ... and this long: restart the camera (unless Stop was pressed)
# Illumination: LTC2630 DAC (12 bit, 2.5 V) into a 56 ohm sense resistor (docs/hardware.md).
LED_MAX_MA = 2.5 / 56.0 * 1000.0   # 44.6 mA, the hardware ceiling
LED_DEFAULT_LIMIT_MA = 20.0        # the firmware's default ceiling


def led_code(ma: float) -> int:
    return int(round(ma / LED_MAX_MA * 4095))
ACCENT = "#4fa3ff"
GOOD, BAD, WARN = "#3ecf8e", "#ff5c5c", "#ffb44f"


# --- Frame reception -------------------------------------------------------------------
class FrameReader(QtCore.QThread):
    """Owns the source: reads every packet, keeps the latest frame, counts what arrives.

    If the serial port fails -- the Teensy was unplugged, or reset by its watchdog and
    re-enumerated -- the reader closes it, tries to open it again every second, and emits
    `reconnected` when it succeeds, so the window can restart the camera.

    For a live device the serial port is read here and only here. Commands are written
    from the GUI thread with Device.command(), which only writes; their replies arrive in
    the packet stream like everything else and are collected into `log`.
    """

    reconnected = QtCore.pyqtSignal()
    RETRY_S = 1.0

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.connected = True
        self.reconnects = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest = None          # (header, img)
        self._new = False
        self.arrivals = collections.deque(maxlen=400)
        self.received = 0
        self.acct = FrameAccounting()  # device drops and PC losses, kept apart
        self.log = collections.deque(maxlen=200)
        self.error = None

    def stop(self):
        self._stop.set()

    def take_latest(self):
        """The newest frame if it has not been taken yet, else None."""
        with self._lock:
            if not self._new:
                return None
            self._new = False
            return self._latest

    def peek_latest(self):
        with self._lock:
            return self._latest

    @property
    def dropped_on_device(self) -> int:
        return self.acct.dropped_on_device

    @property
    def lost_on_pc(self) -> int:
        return self.acct.lost_on_pc

    def reset_counts(self):
        """Forget the counter history, e.g. after a restart."""
        self.acct.reset()

    def _frame(self, header, img):
        self.acct.add(header)
        self.received += 1
        self.arrivals.append(time.monotonic())
        with self._lock:
            self._latest = (header, img)
            self._new = True

    def run(self):
        try:
            if getattr(self.source, "device", None) is not None:
                # Read packets directly: next_packet() returns on every quiet read, so the
                # stop flag is checked even when the device is not streaming.
                while not self._stop.is_set():
                    try:
                        p = self.source.device.reader.next_packet()
                    except Exception as ex:  # noqa: BLE001 - port gone: unplug or reset
                        self._reconnect(ex)
                        continue
                    if p is None:
                        continue
                    if p.is_image:
                        self._frame(*_decode(p))
                    elif p.header.type in (protocol.TYPE_RESPONSE, protocol.TYPE_LOG):
                        self.log.append(p.text)
            else:
                for header, img in self.source.frames():
                    if self._stop.is_set():
                        break
                    self._frame(header, img)
        except Exception as ex:  # noqa: BLE001 - reported in the window, not swallowed
            if not self._stop.is_set():
                self.error = f"{type(ex).__name__}: {ex}"

    def _reconnect(self, why: Exception):
        from .transport import Device

        self.connected = False
        self.log.append(f"! connection lost ({type(why).__name__}); reconnecting")
        port = self.source.device.serial.port
        try:
            self.source.device.close()
        except Exception:  # noqa: BLE001
            pass
        while not self._stop.is_set():
            time.sleep(self.RETRY_S)
            try:
                dev = Device(port)
            except Exception:  # noqa: BLE001 - not back yet
                try:
                    dev = Device.open_first()  # it may have come back under another name
                except Exception:  # noqa: BLE001
                    continue
            self.source.device = dev
            self.connected = True
            self.reconnects += 1
            self.log.append(f"! reconnected on {dev.serial.port}")
            self.reconnected.emit()
            return


def rate(stamps, window=2.0) -> float:
    """Events per second over the last `window` seconds of monotonic timestamps."""
    if len(stamps) < 2:
        return 0.0
    now = time.monotonic()
    recent = [t for t in stamps if now - t <= window]
    if len(recent) < 2:
        return 0.0
    return (len(recent) - 1) / (recent[-1] - recent[0])


# --- Widgets ---------------------------------------------------------------------------
class ImageView(QtWidgets.QWidget):
    """The frame, scaled to fit with square pixels and no smoothing."""

    def __init__(self):
        super().__init__()
        self._pixmap = None
        self.setMinimumSize(320, 320)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Expanding)

    def set_image(self, img8: np.ndarray):
        """Grey (h, w) or colour (h, w, 3), both uint8 and C-contiguous."""
        if img8.ndim == 3:
            h, w, _ = img8.shape
            qimg = QtGui.QImage(img8.data, w, h, w * 3, QtGui.QImage.Format.Format_RGB888)
        else:
            h, w = img8.shape
            qimg = QtGui.QImage(img8.data, w, h, w, QtGui.QImage.Format.Format_Grayscale8)
        self._pixmap = QtGui.QPixmap.fromImage(qimg.copy())
        self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#111316"))
        if self._pixmap is None:
            p.setPen(QtGui.QColor("#6b7280"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "waiting for frames…")
            return
        side = min(self.width(), self.height())
        scale = max(1, side // self._pixmap.width()) if side >= self._pixmap.width() else 1
        size = self._pixmap.width() * scale if side >= self._pixmap.width() else side
        x = (self.width() - size) // 2
        y = (self.height() - size) // 2
        p.drawPixmap(QtCore.QRect(x, y, size, size), self._pixmap)


class Histogram(QtWidgets.QWidget):
    """Log-scaled histogram of the raw pixel values, with the display window marked."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(90)
        self._hist = None
        self._full = 1024
        self._window = None

    def set_data(self, img: np.ndarray, window):
        self._full = 1024 if img.dtype == np.uint16 else 256
        h, _ = np.histogram(img, bins=256, range=(0, self._full))
        h = np.log1p(h.astype(np.float32))
        self._hist = h / h.max() if h.max() > 0 else h
        self._window = window
        self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#15181c"))
        if self._hist is None:
            return
        w, h = self.width(), self.height()
        if self._window is not None:
            lo, hi = self._window
            x0, x1 = lo / self._full * w, hi / self._full * w
            p.fillRect(QtCore.QRectF(x0, 0, max(1.0, x1 - x0), h), QtGui.QColor("#1f2a38"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(ACCENT))
        n = len(self._hist)
        bw = w / n
        for i, v in enumerate(self._hist):
            bh = v * (h - 14)
            p.drawRect(QtCore.QRectF(i * bw, h - 12 - bh, bw + 0.6, bh))
        p.setPen(QtGui.QColor("#6b7280"))
        f = p.font()
        f.setPointSize(7)
        p.setFont(f)
        p.drawText(2, h - 1, "0")
        p.drawText(w - 30, h - 1, str(self._full - 1))


class Stat(QtWidgets.QWidget):
    """A caption over a large value."""

    def __init__(self, caption: str, big: bool = False):
        super().__init__()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.value = QtWidgets.QLabel("–")
        self.value.setObjectName("statBig" if big else "stat")
        cap = QtWidgets.QLabel(caption)
        cap.setObjectName("caption")
        lay.addWidget(self.value)
        lay.addWidget(cap)

    def set(self, text: str, colour: str | None = None):
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {colour};" if colour else "")


class FieldSlider(QtWidgets.QWidget):
    """One register field: name, slider, and the value in real units."""

    changed = QtCore.pyqtSignal()

    def __init__(self, label: str, field: str, inverted: bool = False):
        super().__init__()
        self.field = field
        self.inverted = inverted
        f = regs.BY_NAME[field]
        self.top = regs.ROWS_IN_RESET_MAX if field == "rows_in_reset" else f.max
        lay = QtWidgets.QGridLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        self.name = QtWidgets.QLabel(label)
        self.readout = QtWidgets.QLabel("")
        self.readout.setObjectName("readout")
        self.readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, self.top)
        self.slider.setPageStep(8 if self.top > 31 else 1)
        self.slider.valueChanged.connect(lambda _v: self.changed.emit())
        lay.addWidget(self.name, 0, 0)
        lay.addWidget(self.readout, 0, 1)
        lay.addWidget(self.slider, 1, 0, 1, 2)

    def field_value(self) -> int:
        v = self.slider.value()
        return self.top - v if self.inverted else v

    def set_field_value(self, value: int):
        self.slider.blockSignals(True)
        self.slider.setValue(self.top - value if self.inverted else value)
        self.slider.blockSignals(False)

    def set_readout(self, text: str, off_recommendation: bool):
        self.readout.setText(text)
        self.readout.setStyleSheet(f"color: {WARN};" if off_recommendation else "")


# --- Main window -----------------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, source, clock_hz: int, depth: int = 10):
        super().__init__()
        self.source = source
        self.depth = depth
        self.reader = FrameReader(source)
        self.painted = collections.deque(maxlen=400)
        self.paused = False
        self.auto_contrast = True
        # Colour: the switch decides what is shown, the pattern decides how it is read. The
        # device's own answer (from the frame header) is followed until the switch is touched.
        self.rgb_mode = False
        self.rgb_chosen = False      # the user worked the switch: stop following the header
        self.cfa = color.PATTERNS[0]
        self.wb_gains = None
        self.header_cfa = protocol.Header().cfa
        self.isp = isp_mod.Isp(pattern=self.cfa)
        self.header = None
        self.img = None
        self.controls_ready = False
        self.device_cfg = None
        self.sent_cfg = None
        self.pending_cfg = None
        self.pending_since = 0.0
        self.saved = 0
        self.user_stopped = False    # Stop pressed: no automatic restarts
        self.restore_cfg = None      # settings to put back after a restart
        self.last_restart = time.monotonic()
        self.setWindowTitle(f"NanEyeC — {source.name}")
        self._build(clock_hz)
        self.reader.reconnected.connect(self._on_reconnected)
        self.reader.start()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)
        self.slow = QtCore.QTimer(self)
        self.slow.timeout.connect(self._update_stats)
        self.slow.start(250)
        # The device is started only now, with the reader already running: started earlier,
        # it streams while the window is being built and drops frames nobody is reading.
        if self.device is not None:
            QtCore.QTimer.singleShot(0, self._restore_led)
            QtCore.QTimer.singleShot(0, self._start)

    @property
    def device(self):
        """The live device, if any: after a reconnect this is a new Device object."""
        return getattr(self.source, "device", None)

    # --- layout ------------------------------------------------------------------------
    def _build(self, clock_hz):
        central = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        left = QtWidgets.QVBoxLayout()
        self.view = ImageView()
        self.hist = Histogram()
        left.addWidget(self.view, 1)
        left.addWidget(self.hist)
        root.addLayout(left, 1)

        panel = QtWidgets.QWidget()
        side = QtWidgets.QVBoxLayout(panel)
        side.setContentsMargins(0, 0, 4, 0)
        side.setSpacing(10)
        side.addWidget(self._link_box())
        side.addWidget(self._acquisition_box(clock_hz))
        side.addWidget(self._colour_box())
        side.addWidget(self._exposure_box())
        side.addWidget(self._analog_box())
        side.addWidget(self._led_box())
        side.addWidget(self._register_box())
        side.addWidget(self._log_box(), 1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(400)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        root.addWidget(scroll)
        self.setCentralWidget(central)

        self.status = QtWidgets.QLabel("")
        self.statusBar().addWidget(self.status, 1)
        for key, fn in ((Qt.Key.Key_Space, self._toggle_pause), (Qt.Key.Key_S, self._save),
                        (Qt.Key.Key_Plus, lambda: self._nudge_exposure(-8)),
                        (Qt.Key.Key_Equal, lambda: self._nudge_exposure(-8)),
                        (Qt.Key.Key_Minus, lambda: self._nudge_exposure(8)),
                        (Qt.Key.Key_R, self._recommended), (Qt.Key.Key_Q, self.close),
                        (Qt.Key.Key_L, lambda: self.btn_led.toggle()),
                        (Qt.Key.Key_C, lambda: self.sw_rgb.setChecked(not self.rgb_mode)),
                        (Qt.Key.Key_BracketLeft, lambda: self._nudge_led(-1.0)),
                        (Qt.Key.Key_BracketRight, lambda: self._nudge_led(1.0))):
            QtGui.QShortcut(QtGui.QKeySequence(key), self, activated=fn)
        self.resize(1180, 860)

    def _group(self, title):
        box = QtWidgets.QGroupBox(title)
        return box

    def _link_box(self):
        box = self._group("Link")
        g = QtWidgets.QGridLayout(box)
        self.st_rx = Stat("fps received", big=True)
        self.st_disp = Stat("fps displayed", big=True)
        self.st_sync = Stat("link")
        self.st_failed = Stat("failed rows")
        self.st_conc = Stat("concealed px")
        self.st_gaps = Stat("lost on PC")
        self.st_devdrop = Stat("dropped by device")
        self.st_frame = Stat("frame")
        self.st_sclk = Stat("SCLK")
        g.addWidget(self.st_rx, 0, 0)
        g.addWidget(self.st_disp, 0, 1)
        g.addWidget(self.st_sync, 0, 2)
        g.addWidget(self.st_failed, 1, 0)
        g.addWidget(self.st_conc, 1, 1)
        g.addWidget(self.st_gaps, 1, 2)
        g.addWidget(self.st_devdrop, 2, 0)
        g.addWidget(self.st_frame, 2, 1)
        g.addWidget(self.st_sclk, 2, 2)
        return box

    def _acquisition_box(self, clock_hz):
        box = self._group("Acquisition")
        g = QtWidgets.QGridLayout(box)
        self.clock = QtWidgets.QComboBox()
        for hz, text in CLOCKS:
            self.clock.addItem(text, hz)
        self.clock.setCurrentIndex(max(0, [hz for hz, _ in CLOCKS].index(clock_hz))
                                   if clock_hz in [hz for hz, _ in CLOCKS] else 0)
        self.btn_start = QtWidgets.QPushButton("Start")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop_device)
        self.chk_auto = QtWidgets.QCheckBox("Auto contrast")
        self.chk_auto.setChecked(True)
        self.chk_auto.toggled.connect(lambda on: setattr(self, "auto_contrast", on))
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(lambda on: setattr(self, "paused", on))
        self.btn_save = QtWidgets.QPushButton("Save frame")
        self.btn_save.clicked.connect(self._save)
        g.addWidget(QtWidgets.QLabel("Clock"), 0, 0)
        g.addWidget(self.clock, 0, 1, 1, 2)
        g.addWidget(self.btn_start, 1, 0)
        g.addWidget(self.btn_stop, 1, 1)
        g.addWidget(self.btn_pause, 1, 2)
        g.addWidget(self.chk_auto, 2, 0, 1, 2)
        g.addWidget(self.btn_save, 2, 2)
        live = self.device is not None
        for w in (self.clock, self.btn_start, self.btn_stop):
            w.setEnabled(live)
        return box

    def _colour_box(self):
        """Mono or RGB, which mosaic, and what to do about white balance."""
        box = self._group("Colour")
        g = QtWidgets.QGridLayout(box)
        self.sw_mono = QtWidgets.QPushButton("Mono")
        self.sw_rgb = QtWidgets.QPushButton("RGB")
        group = QtWidgets.QButtonGroup(box)
        group.setExclusive(True)
        for i, b in enumerate((self.sw_mono, self.sw_rgb)):
            b.setCheckable(True)
            group.addButton(b, i)
        self.sw_mono.setChecked(True)
        self.sw_rgb.toggled.connect(self._rgb_toggled)

        self.cfa_box = QtWidgets.QComboBox()
        for p in color.PATTERNS:
            self.cfa_box.addItem(p, p)
        self.cfa_box.currentIndexChanged.connect(self._cfa_changed)

        self.wb_box = QtWidgets.QComboBox()
        self.wb_box.addItem("as measured", None)
        self.wb_box.addItem("grey world (once)", "once")
        self.wb_box.addItem("grey world (every frame)", "auto")
        self.wb_box.currentIndexChanged.connect(self._wb_changed)

        self.black = QtWidgets.QSpinBox()
        self.black.setRange(0, 512)
        self.black.setSingleStep(4)
        self.black.setSuffix(" DN")
        self.black.valueChanged.connect(
            lambda v: setattr(self.isp, "black_level", float(v)))
        self.btn_black = QtWidgets.QPushButton("from frame")
        self.btn_black.setToolTip("The darkest 1 % of the current frame. A real black "
                                  "level needs a covered lens.")
        self.btn_black.clicked.connect(self._black_from_frame)

        self.gamma_box = QtWidgets.QComboBox()
        for name, value in isp_mod.GAMMAS.items():
            self.gamma_box.addItem(name, value)
        self.gamma_box.setCurrentIndex(list(isp_mod.GAMMAS).index("2.2"))
        self.gamma_box.currentIndexChanged.connect(
            lambda: setattr(self.isp, "gamma", self.gamma_box.currentData()))

        self.ccm_box = QtWidgets.QComboBox()
        for name in isp_mod.MATRICES:
            self.ccm_box.addItem(name, name)
        self.ccm_box.currentIndexChanged.connect(
            lambda: setattr(self.isp, "matrix", self.ccm_box.currentData()))

        self.lbl_cfa = QtWidgets.QLabel("")
        self.lbl_cfa.setObjectName("caption")
        self.lbl_cfa.setWordWrap(True)
        g.addWidget(self.sw_mono, 0, 0)
        g.addWidget(self.sw_rgb, 0, 1)
        g.addWidget(QtWidgets.QLabel("mosaic"), 1, 0)
        g.addWidget(self.cfa_box, 1, 1, 1, 2)
        g.addWidget(QtWidgets.QLabel("black level"), 2, 0)
        g.addWidget(self.black, 2, 1)
        g.addWidget(self.btn_black, 2, 2)
        g.addWidget(QtWidgets.QLabel("white balance"), 3, 0)
        g.addWidget(self.wb_box, 3, 1, 1, 2)
        g.addWidget(QtWidgets.QLabel("colour matrix"), 4, 0)
        g.addWidget(self.ccm_box, 4, 1, 1, 2)
        g.addWidget(QtWidgets.QLabel("gamma"), 5, 0)
        g.addWidget(self.gamma_box, 5, 1, 1, 2)
        g.addWidget(self.lbl_cfa, 6, 0, 1, 3)
        self._update_cfa_caption()
        return box

    def _black_from_frame(self):
        if self.img is not None:
            self.black.setValue(int(self.isp.measure_black_level(self.img)))

    def _rgb_toggled(self, on: bool):
        self.rgb_mode = on
        self.rgb_chosen = True
        self.wb_gains = None
        self._update_cfa_caption()

    def _cfa_changed(self):
        self.cfa = self.isp.pattern = self.cfa_box.currentData()
        self.wb_gains = None
        self._update_cfa_caption()

    def _wb_changed(self):
        self.wb_gains = None
        self.isp.gains = (1.0, 1.0, 1.0)
        self._update_cfa_caption()

    def _follow_header_cfa(self, header):
        """Adopt what the device says its sensor is, until the switch is touched."""
        cfa = header.cfa
        if cfa == self.header_cfa:
            return
        self.header_cfa = cfa
        if cfa != color.MONO:
            self.cfa_box.blockSignals(True)
            self.cfa_box.setCurrentIndex(color.PATTERNS.index(cfa))
            self.cfa_box.blockSignals(False)
            self.cfa = self.isp.pattern = cfa
        if not self.rgb_chosen:
            want_rgb = cfa != color.MONO
            (self.sw_rgb if want_rgb else self.sw_mono).setChecked(True)
            self.rgb_chosen = False      # following the device is not a choice
            self.rgb_mode = want_rgb
        self._update_cfa_caption()

    def _update_cfa_caption(self):
        said = ("the device says MONO" if self.header_cfa == color.MONO
                else f"the device says {self.header_cfa}")
        if not self.rgb_mode:
            what = "raw mosaic, black level and gamma only"
        else:
            r, _, b = self.isp.gains
            wb = f", WB R×{r:.2f} B×{b:.2f}" if (r, b) != (1.0, 1.0) else ""
            what = f"bilinear demosaic{wb}"
        self.lbl_cfa.setText(f"{what} — {said} (the CFA travels in the frame header). "
                             f"ISP {self.isp.last_ms:.1f} ms/frame")

    def _to_display(self, img):
        """Raw frame -> uint8 for the view, through the ISP.

        Auto contrast is a stretch of the linear image before gamma, so it works the same
        in both modes and does not fight the tone curve.
        """
        full = 1023.0 if img.dtype == np.uint16 else 255.0
        self.isp.white = full
        self.isp.demosaic = self.rgb_mode and img.ndim == 2
        wb = self.wb_box.currentData()
        if self.isp.demosaic and (wb == "auto" or (wb == "once" and self.wb_gains is None)):
            self.wb_gains = self.isp.gains = self.isp.auto_white_balance(img)
        elif not self.isp.demosaic:
            self.isp.gains = (1.0, 1.0, 1.0)
        t0 = time.perf_counter()
        if self.auto_contrast:
            lin = self.isp.linear(img)                  # the ISP's own buffer
            lo, hi = (float(v) for v in np.percentile(lin[::2, ::2], [0.5, 99.5]))
            hi = max(hi, lo + 1.0)
            np.subtract(lin, lo, out=lin)
            np.multiply(lin, full / (hi - lo), out=lin)
            out = self.isp.apply_curve(lin)
        else:
            lo, hi = 0.0, full
            out = self.isp.process(img)
        self.isp.last_ms = (time.perf_counter() - t0) * 1000.0
        return np.ascontiguousarray(out), (lo, hi)

    def _exposure_box(self):
        box = self._group("Exposure and gain")
        v = QtWidgets.QVBoxLayout(box)
        self.sliders = [
            FieldSlider("Exposure", "rows_in_reset", inverted=True),
            FieldSlider("Frame delay", "rows_delay"),
            FieldSlider("Ramp gain", "ramp_gain"),
            FieldSlider("CDS gain", "cds_gain"),
        ]
        for s in self.sliders:
            v.addWidget(s)
        return box

    def _analog_box(self):
        box = self._group("Analog settings")
        v = QtWidgets.QVBoxLayout(box)
        analog = [
            FieldSlider("Pixel reset (vrst_pix)", "vrst_pix"),
            FieldSlider("Ramp offset (offset_ramp)", "offset_ramp"),
            FieldSlider("ADC reference (vref)", "vref"),
            FieldSlider("Column current (cvc_curr)", "cvc_curr"),
            FieldSlider("Output drive (output_curr)", "output_curr"),
            FieldSlider("Pixel bias boost", "bias_curr_increase"),
        ]
        for s in analog:
            v.addWidget(s)
        self.sliders += analog
        btn = QtWidgets.QPushButton("Datasheet recommended")
        btn.setToolTip("Recommended analog values, gains at unity (R)")
        btn.clicked.connect(self._recommended)
        v.addWidget(btn)
        for s in self.sliders:
            s.changed.connect(self._slider_moved)
            s.setEnabled(False)
        box.setCheckable(True)
        box.setChecked(True)
        box.toggled.connect(lambda on: [w.setVisible(on) for w in analog + [btn]])
        return box

    def _led_box(self):
        """The NanoBerry's LEDs: on/off, current, and the current ceiling.

        The DAC powers up at zero scale, so switching on sends the current first. Current
        is set in 0.1 mA steps (the DAC's own step is 0.011 mA)."""
        box = self._group("Illumination")
        g = QtWidgets.QGridLayout(box)
        self.btn_led = QtWidgets.QPushButton("LED off")
        self.btn_led.setCheckable(True)
        self.btn_led.toggled.connect(self._led_toggled)
        self.led_limit = QtWidgets.QDoubleSpinBox()
        self.led_limit.setRange(1.0, LED_MAX_MA)
        self.led_limit.setDecimals(1)
        self.led_limit.setSingleStep(1.0)
        self.led_limit.setSuffix(" mA max")
        self.led_limit.setValue(LED_DEFAULT_LIMIT_MA)
        self.led_limit.setToolTip("Current ceiling (LEDMAX). Hardware maximum 44.6 mA")
        self.led_limit.valueChanged.connect(self._led_limit_changed)
        self.led_slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.led_slider.setRange(0, int(LED_DEFAULT_LIMIT_MA * 10))
        self.led_slider.setValue(50)  # 5 mA
        self.led_slider.valueChanged.connect(self._led_current_moved)
        self.led_readout = QtWidgets.QLabel("")
        self.led_readout.setObjectName("readout")
        self.led_readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        note = QtWidgets.QLabel("VIS and NIR strings share one current sink; which are active "
                                "depends on jumpers R16/R17.")
        note.setObjectName("caption")
        note.setWordWrap(True)
        g.addWidget(self.btn_led, 0, 0)
        g.addWidget(self.led_limit, 0, 1)
        g.addWidget(QtWidgets.QLabel("Current"), 1, 0)
        g.addWidget(self.led_readout, 1, 1)
        g.addWidget(self.led_slider, 2, 0, 1, 2)
        g.addWidget(note, 3, 0, 1, 2)
        self.led_pending_since = None
        self.led_sent_ma = None
        self._update_led_readout()
        for w in (self.btn_led, self.led_limit, self.led_slider):
            w.setEnabled(self.device is not None)
        return box

    def _led_ma(self) -> float:
        return self.led_slider.value() / 10.0

    def _update_led_readout(self):
        ma = self._led_ma()
        self.led_readout.setText(f"{ma:.1f} mA  ·  DAC code {led_code(ma)}")

    def _led_toggled(self, on: bool):
        self.btn_led.setText("LED on" if on else "LED off")
        if on:
            self._send(f"LEDI {self._led_ma():.2f}")
            self.led_sent_ma = self._led_ma()
        self._send(f"LED {1 if on else 0}")

    def _led_current_moved(self):
        self._update_led_readout()
        self.led_pending_since = time.monotonic()

    def _led_limit_changed(self, limit: float):
        self._send(f"LEDMAX {limit:.1f}")
        self.led_slider.setMaximum(int(round(limit * 10)))  # also clamps the current
        self._update_led_readout()

    def _nudge_led(self, step_ma: float):
        self.led_slider.setValue(self.led_slider.value() + int(step_ma * 10))

    def _flush_led(self):
        """Send a settled current change, like the register sliders."""
        if self.led_pending_since is None:
            return
        if time.monotonic() - self.led_pending_since < SETTLE_S:
            return
        self.led_pending_since = None
        if self._led_ma() != self.led_sent_ma:
            self._send(f"LEDI {self._led_ma():.2f}")
            self.led_sent_ma = self._led_ma()

    def _restore_led(self):
        """Make the device's LED state match the panel: at startup (the device keeps its
        LED settings in RAM between sessions) and after a reconnect."""
        self._send(f"LEDMAX {self.led_limit.value():.1f}")
        if self.btn_led.isChecked():
            self._send(f"LEDI {self._led_ma():.2f}")
            self._send("LED 1")
        else:
            self._send("LED 0")

    def _register_box(self):
        box = self._group("Registers")
        v = QtWidgets.QVBoxLayout(box)
        self.reg_head = QtWidgets.QLabel("")
        self.reg_head.setObjectName("mono")
        self.table = QtWidgets.QTableWidget(len(regs.FIELDS), 3)
        self.table.setHorizontalHeaderLabels(["field", "value", "meaning"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        vh = self.table.verticalHeader()
        vh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        vh.setMinimumSectionSize(20)
        vh.setDefaultSectionSize(20)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setFixedHeight(20 * len(regs.FIELDS) + 30)
        legend = QtWidgets.QLabel(f"<span style='color:{WARN}'>amber</span>: not the "
                                  "datasheet's recommended value · grey: set by the firmware")
        legend.setObjectName("caption")
        legend.setWordWrap(True)
        v.addWidget(self.reg_head)
        v.addWidget(self.table)
        v.addWidget(legend)
        return box

    def _log_box(self):
        box = self._group("Device")
        v = QtWidgets.QVBoxLayout(box)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("mono")
        self.log.setMinimumHeight(90)
        for line in getattr(self.source, "log", []):
            self.log.appendPlainText(line)
        v.addWidget(self.log)
        return box

    # --- device commands ---------------------------------------------------------------
    def _send(self, text: str):
        if self.device is None:
            return
        self.log.appendPlainText(f"> {text}")
        try:
            self.device.command(text)
        except Exception as ex:  # noqa: BLE001
            self.log.appendPlainText(f"! {ex}")

    def _stop_device(self):
        # Stop means stop: the sensor's rail goes down too, so a paused GUI leaves nothing
        # clocking and nothing drawing current. Start powers it back up.
        self.user_stopped = True
        self._send("STOP")
        self._send("POWER 0")

    def _on_reconnected(self):
        self.log.appendPlainText("! restarting the camera after reconnecting")
        self._restore_led()
        self._start(automatic=True)

    def _start(self, automatic: bool = False):
        if not automatic:
            self.user_stopped = False
        # A restart (or a device that reset) comes back with its default registers: put the
        # sliders' settings back once the first frame shows what the device has.
        if self.controls_ready and self.sent_cfg is not None:
            self.restore_cfg = self.sent_cfg
        self.last_restart = time.monotonic()
        self._send("STOP")
        self._send(f"CLK {self.clock.currentData()}")
        self._send(f"DEPTH {self.depth}")
        self._send("START")
        self.controls_ready = False  # re-read the registers from the new stream
        self.reader.reset_counts()

    # --- register controls -------------------------------------------------------------
    def _init_controls(self, header):
        values = regs.unpack(header.cfg0, header.cfg1)
        for s in self.sliders:
            s.set_field_value(values[s.field])
            s.setEnabled(self.device is not None)
        self.device_cfg = self.sent_cfg = self.pending_cfg = (header.cfg0, header.cfg1)
        self.controls_ready = True
        if self.restore_cfg is not None:
            wanted = regs.unpack(*self.restore_cfg)
            for s in self.sliders:
                s.set_field_value(wanted[s.field])
            self.restore_cfg = None
            self.pending_cfg = self._wanted_cfg()
            self.pending_since = 0.0  # send at once

    def _wanted_cfg(self):
        values = {s.field: s.field_value() for s in self.sliders}
        return regs.pack(values, *self.device_cfg)

    def _slider_moved(self):
        self.pending_cfg = self._wanted_cfg()
        self.pending_since = time.monotonic()
        self._update_readouts()

    def _nudge_exposure(self, step):
        s = self.sliders[0]
        s.set_field_value(max(0, min(regs.ROWS_IN_RESET_MAX, s.field_value() + step)))
        self._slider_moved()

    def _recommended(self):
        if not self.controls_ready:
            return
        c0, c1 = regs.recommended(*self._wanted_cfg())
        values = regs.unpack(c0, c1)
        for s in self.sliders:
            s.set_field_value(values[s.field])
        self._slider_moved()

    def _flush_register_writes(self):
        if not self.controls_ready or self.pending_cfg is None:
            return
        if self.pending_cfg == self.sent_cfg:
            return
        if time.monotonic() - self.pending_since < SETTLE_S:
            return
        want = self.pending_cfg
        if want[0] != self.sent_cfg[0]:
            self._send(f"REG 0 0x{want[0]:04X}")
        if want[1] != self.sent_cfg[1]:
            self._send(f"REG 1 0x{want[1]:04X}")
        self.sent_cfg = want

    def _update_readouts(self):
        if not self.controls_ready or self.header is None:
            return
        c0, c1 = self._wanted_cfg()
        v = regs.unpack(c0, c1)
        sclk = self.header.sclk_hz or None
        for s in self.sliders:
            f = regs.BY_NAME[s.field]
            val = v[s.field]
            if s.field == "rows_in_reset" and sclk:
                text = f"{regs.exposure_ms(val, v['rows_delay'], sclk):.2f} ms"
            elif s.field == "rows_delay" and sclk:
                text = f"+{16 * val + 2} rows · ≤ {1e3 / regs.frame_period_ms(val, sclk):.1f} fps"
            else:
                text = f.label(val) if f.labels else str(val)
            s.set_readout(text, f.recommended is not None and val != f.recommended)

    # --- per-frame work ----------------------------------------------------------------
    def _tick(self):
        if self.reader.error:
            self.status.setText(f"<span style='color:{BAD}'>{self.reader.error}</span>")
        while self.reader.log:
            self.log.appendPlainText(self.reader.log.popleft())
        self._flush_register_writes()
        self._flush_led()
        frame = self.reader.take_latest()
        if frame is None or self.paused:
            return
        header, img = frame
        self.header, self.img = header, img
        if not self.controls_ready:
            self._init_controls(header)
            self._update_readouts()
        else:
            self.device_cfg = (header.cfg0, header.cfg1)

        self._follow_header_cfa(header)
        shown, (lo, hi) = self._to_display(img)
        self.view.set_image(shown)
        self.hist.set_data(img, (lo, hi))   # the raw mosaic values, whatever is displayed
        self._update_cfa_caption()
        self.painted.append(time.monotonic())

    def _watch_link(self):
        """'RECONNECTING' or 'NO FRAMES' when the stream has stopped, else None. Restarts
        the camera after RESTART_AFTER_S without frames, unless Stop was pressed."""
        if self.device is None and self.reader.connected:
            return None                      # replay or file: nothing to watch
        if not self.reader.connected:
            return "RECONNECTING"
        now = time.monotonic()
        last = self.reader.arrivals[-1] if self.reader.arrivals else 0.0
        quiet = now - max(last, self.last_restart)
        if self.user_stopped or quiet < NO_FRAMES_S:
            return None
        if quiet >= RESTART_AFTER_S:
            self.log.appendPlainText("! no frames: restarting the camera")
            self._start(automatic=True)
        return "NO FRAMES"

    def _update_stats(self):
        rx = rate(self.reader.arrivals)
        disp = rate(self.painted)
        trouble = self._watch_link()
        if trouble:
            self.st_sync.set(trouble, BAD)
        self.st_rx.set(f"{rx:.1f}")
        self.st_disp.set(f"{disp:.1f}", WARN if rx and disp < 0.9 * rx else None)
        h = self.header
        if h is None:
            return
        sync_ok = not h.sync_lost
        if not trouble:
            self.st_sync.set("locked" if sync_ok else "SYNC LOST",
                             GOOD if sync_ok else BAD)
        self.st_failed.set(str(h.rows_failed), BAD if h.rows_failed else None)
        self.st_conc.set(str(h.pixels_concealed), WARN if h.pixels_concealed else None)
        lost = self.reader.lost_on_pc
        dev = self.reader.dropped_on_device
        self.st_gaps.set(f"{lost}", WARN if lost else None)
        self.st_devdrop.set(f"{dev}", WARN if dev else None)
        self.st_frame.set(str(h.frame_counter))
        self.st_sclk.set(f"{h.sclk_hz / 1e6:.3f} MHz")
        self.st_gaps.setToolTip("frames that left the device but did not arrive intact")
        self.st_devdrop.setToolTip("frames the device skipped because the PC had not yet "
                                   "taken the previous one (counted by the firmware)")
        self._update_registers(h)
        img = self.img
        self.status.setText(
            f"{h.format_name}   min {int(img.min())}   max {int(img.max())}   "
            f"mean {img.mean():.1f}   exposure {h.exposure_us() / 1000:.2f} ms   "
            f"received {self.reader.received}   saved {self.saved}"
            + ("   PAUSED" if self.paused else ""))

    def _update_registers(self, h):
        self.reg_head.setText(f"CONFIG_0 0x{h.cfg0:04X}    CONFIG_1 0x{h.cfg1:04X}")
        grey = QtGui.QColor("#6b7280")
        amber = QtGui.QColor(WARN)
        for row, (reg, name, value, meaning, editable, differs) in enumerate(
                regs.rows(h.cfg0, h.cfg1, h.sclk_hz or None)):
            for col, text in enumerate((f"{reg}.{name}", str(value), meaning)):
                item = self.table.item(row, col)
                if item is None:
                    item = QtWidgets.QTableWidgetItem()
                    self.table.setItem(row, col, item)
                item.setText(text)
                item.setForeground(grey if not editable else amber if differs
                                   else QtGui.QColor("#e5e7eb"))

    # --- actions -----------------------------------------------------------------------
    def _toggle_pause(self):
        self.btn_pause.toggle()

    def _save(self):
        if self.img is None or self.header is None:
            return
        name = f"frame_{self.header.frame_counter:06d}.png"
        import cv2  # 16-bit PNG, raw values: 10-bit data keeps all its bits

        cv2.imwrite(name, self.img)
        self.saved += 1
        self.log.appendPlainText(f"saved {os.path.abspath(name)} (raw {self.img.dtype})")

    def closeEvent(self, event):
        self.timer.stop()
        self.slow.stop()
        # Leave the bench dark and unpowered. These are write-only, so they are safe to
        # send while the reader thread still owns the port.
        self._send("STOP")
        self._send("LED 0")
        self._send("POWER 0")
        self.reader.stop()
        self.reader.wait(2000)
        try:
            self.source.close()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


STYLE = f"""
QWidget {{ background: #1b1e23; color: #e5e7eb; font-size: 10pt; }}
QGroupBox {{ border: 1px solid #2c313a; border-radius: 8px; margin-top: 14px;
             padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                    color: {ACCENT}; font-weight: 600; }}
QGroupBox::indicator {{ width: 12px; height: 12px; }}
QLabel#statBig {{ font-size: 22pt; font-weight: 600; }}
QLabel#stat {{ font-size: 13pt; font-weight: 600; }}
QLabel#caption {{ color: #8b93a1; font-size: 8pt; }}
QLabel#readout {{ color: #cbd5e1; font-family: Consolas, monospace; }}
QLabel#mono, QPlainTextEdit#mono {{ font-family: Consolas, monospace; font-size: 9pt; }}
QPlainTextEdit {{ background: #15181c; border: 1px solid #2c313a; border-radius: 6px; }}
QTableWidget {{ background: #15181c; border: 1px solid #2c313a; border-radius: 6px;
                gridline-color: #22262d; font-family: Consolas, monospace; font-size: 9pt; }}
QHeaderView::section {{ background: #22262d; color: #8b93a1; border: none; padding: 2px 4px; }}
QPushButton {{ background: #262b33; border: 1px solid #353b45; border-radius: 6px;
               padding: 5px 10px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:checked {{ background: {ACCENT}; color: #0b1320; }}
QPushButton:disabled, QComboBox:disabled {{ color: #5b626d; }}
QComboBox, QDoubleSpinBox {{ background: #262b33; border: 1px solid #353b45;
                            border-radius: 6px; padding: 4px 8px; }}
QSlider::groove:horizontal {{ height: 4px; background: #2c313a; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: #e5e7eb; width: 14px; margin: -6px 0;
                              border-radius: 7px; }}
QSlider::handle:horizontal:disabled {{ background: #4b5260; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: #1b1e23; }}
QStatusBar {{ background: #15181c; color: #8b93a1; }}
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="auto",
                    help="'auto' (default), a COM port, 'replay', a .csv capture, or a "
                         "recorded stream file")
    ap.add_argument("--clock", type=int, default=49500000,
                    help="SCLK: 49500000 (default, ~35 fps), 24750000 or 12375000")
    ap.add_argument("--depth", type=int, default=10, choices=(8, 10))
    ap.add_argument("--fps", type=float, default=35.0, help="replay rate")
    ap.add_argument("--snapshot", metavar="PATH",
                    help="save a screenshot of the window after --snapshot-after s, then exit")
    ap.add_argument("--snapshot-after", type=float, default=4.0)
    args = ap.parse_args(argv)

    fmt = {8: protocol.FMT_GRAY8, 10: protocol.FMT_GRAY10}[args.depth]
    app = QtWidgets.QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    source = open_source(args.source, depth=args.depth, clock_hz=args.clock, fmt=fmt,
                         fps=args.fps, start=False)
    win = MainWindow(source, args.clock, args.depth)
    win.show()
    if args.snapshot:
        def shoot():
            win.grab().save(args.snapshot)
            r = win.reader
            print(f"wrote {args.snapshot}: received {r.received} "
                  f"({rate(r.arrivals):.1f} fps), displayed {rate(win.painted):.1f} fps, "
                  f"lost on PC {r.lost_on_pc}, dropped by device {r.dropped_on_device}")
            win.close()
        QtCore.QTimer.singleShot(int(args.snapshot_after * 1000), shoot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
