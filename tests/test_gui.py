"""The Qt GUI, headless: frame accounting, and a smoke test on replayed frames."""

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from naneye import gui, protocol  # noqa: E402
from naneye.sources import open_source  # noqa: E402


def header(counter, dropped=0):
    return protocol.Header(frame_counter=counter, frames_dropped=dropped)


def feed(reader, frames):
    img = np.zeros((4, 4), np.uint16)
    for counter, dropped in frames:
        reader._frame(header(counter, dropped), img)


class _NoSource:
    name = "none"


def test_gaps_the_device_counted_are_not_blamed_on_the_pc():
    r = gui.FrameReader(_NoSource())
    # frames 1..3, then the device drops 4 and 5 (and says so), then 6
    feed(r, [(1, 0), (2, 0), (3, 0), (6, 2)])
    assert r.dropped_on_device == 2 and r.lost_on_pc == 0


def test_gaps_the_device_did_not_count_are_lost_on_the_pc():
    r = gui.FrameReader(_NoSource())
    feed(r, [(1, 0), (2, 0), (4, 0), (5, 0)])      # frame 3 sent, never arrived
    assert r.lost_on_pc == 1 and r.dropped_on_device == 0


def test_reset_forgets_history():
    r = gui.FrameReader(_NoSource())
    feed(r, [(1, 0), (5, 0)])
    r.reset_counts()
    feed(r, [(100, 7), (101, 7)])                    # e.g. after a restart
    assert r.lost_on_pc == 0 and r.dropped_on_device == 0


def test_take_latest_hands_each_frame_over_once():
    r = gui.FrameReader(_NoSource())
    feed(r, [(1, 0), (2, 0)])
    got = r.take_latest()
    assert got[0].frame_counter == 2
    assert r.take_latest() is None


def test_rate():
    now = time.monotonic()
    assert gui.rate([now - 1.0 + i * 0.1 for i in range(11)]) == pytest.approx(10.0)
    assert gui.rate([]) == 0.0


def test_window_receives_and_displays_replayed_frames():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    source = open_source("replay", depth=10, fmt=protocol.FMT_GRAY10, fps=50,
                         allow_synthetic=True)
    win = gui.MainWindow(source, 49500000)
    win.show()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(win.painted) < 10:
        app.processEvents()
        time.sleep(0.01)
    win._update_stats()
    assert win.reader.received >= 10
    assert len(win.painted) >= 10
    assert win.table.item(0, 0).text() == "0.rows_in_reset"
    assert not win.btn_start.isEnabled()             # no device: acquisition disabled
    win.close()


class _DeadReader:
    def next_packet(self):
        raise OSError("device disconnected")


class _QuietReader:
    def next_packet(self):
        time.sleep(0.01)
        return None


class _FakeSerial:
    port = "COM99"


class _FakeDevice:
    def __init__(self, reader):
        self.reader = reader
        self.serial = _FakeSerial()
        self.closed = False

    def close(self):
        self.closed = True


class _LiveSource:
    name = "fake device"

    def __init__(self):
        self.device = _FakeDevice(_DeadReader())


def test_reader_reconnects_when_the_port_fails(monkeypatch):
    import naneye.transport as transport

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    opened = []

    class _NewDevice(_FakeDevice):
        def __init__(self, port):
            super().__init__(_QuietReader())
            opened.append(port)

    monkeypatch.setattr(transport, "Device", _NewDevice)
    src = _LiveSource()
    old = src.device
    r = gui.FrameReader(src)
    r.RETRY_S = 0.05
    signalled = []
    r.reconnected.connect(lambda: signalled.append(True))
    r.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not signalled:
        app.processEvents()
        time.sleep(0.01)
    r.stop()
    r.wait(2000)
    assert old.closed
    assert opened == ["COM99"]                       # reopened by its old name first
    assert src.device is not old and r.connected and r.reconnects == 1
    assert signalled
    assert any("reconnected" in line for line in r.log)


class _RecordingDevice(_FakeDevice):
    """A device that only remembers what it was told."""

    def __init__(self):
        super().__init__(_QuietReader())
        self.sent = []

    def command(self, text):
        self.sent.append(text)


class _RecordingSource:
    name = "recording device"
    log = []

    def __init__(self):
        self.device = _RecordingDevice()

    def close(self):
        pass


def test_stop_and_close_power_the_sensor_off():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    src = _RecordingSource()
    win = gui.MainWindow(src, 49500000)
    win.show()
    app.processEvents()                       # runs the queued start
    assert "START" in src.device.sent         # opening the GUI enables the sensor
    src.device.sent.clear()

    win._stop_device()
    assert src.device.sent == ["STOP", "POWER 0"]
    src.device.sent.clear()

    win._start()                              # Start powers it back up
    assert src.device.sent[-1] == "START"
    src.device.sent.clear()

    win.close()
    assert src.device.sent[:3] == ["STOP", "LED 0", "POWER 0"]


def test_the_colour_switch_turns_the_mosaic_into_rgb():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from naneye import color

    src = _RecordingSource()
    win = gui.MainWindow(src, 49500000)
    win.wb_box.setCurrentIndex(0)               # "as measured": no grey world in the way
    win.ccm_box.setCurrentIndex(0)              # and no colour matrix either
    raw = np.full((8, 8), 500, np.uint16)
    raw[color.masks("BGGR", raw.shape)["R"]] = 900

    shown, _ = win._to_display(raw)
    assert shown.shape == (8, 8, 3)             # RGB by default: demosaiced
    assert shown[4, 4, 0] > shown[4, 4, 2]      # red is the bright channel

    win.sw_mono.setChecked(True)
    shown, _ = win._to_display(raw)
    assert shown.ndim == 2                      # mono: the raw mosaic, as received

    win.sw_rgb.setChecked(True)
    assert win._to_display(raw)[0].shape == (8, 8, 3)
    win.close()


def test_the_gui_follows_the_mosaic_the_device_reports():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from naneye import color

    win = gui.MainWindow(_RecordingSource(), 49500000)
    assert win.rgb_mode and win.cfa == "BGGR"                # the default, before any frame

    win._follow_header_cfa(header(1))                        # a mono device turns it off
    assert not win.rgb_mode

    colour = header(2)
    colour.flags |= color.CODE_BY_CFA["GRBG"] << protocol.FLAG_CFA_SHIFT
    win._follow_header_cfa(colour)
    assert win.rgb_mode and win.cfa == "GRBG"

    win.sw_mono.setChecked(True)                             # the switch wins from now on
    win._follow_header_cfa(header(3))
    win._follow_header_cfa(colour)
    assert not win.rgb_mode
    win.close()


def test_the_camera_comes_up_on_the_bench_defaults():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from naneye import isp as isp_mod

    src = _RecordingSource()
    win = gui.MainWindow(src, gui.DEFAULTS["clock_hz"])
    assert not win.auto_contrast and not win.chk_auto.isChecked()
    assert win.rgb_mode and win.sw_rgb.isChecked()
    assert win.cfa_box.currentData() == "BGGR" and win.isp.pattern == "BGGR"
    assert win.black.value() == 170 and win.isp.black_level == 170
    assert win.wb_box.currentData() == "auto"
    assert win.ccm_box.currentData() == "saturation" and win.isp.matrix == "saturation"
    assert win.gamma_box.currentData() is None and win.isp.gamma is None   # sRGB
    assert win.clock.currentData() == 12375000

    # The longest exposure is sent once, when the first frame says where the registers are.
    app.processEvents()
    src.device.sent.clear()
    short = protocol.Header(frame_counter=1, cfg0=60 << 8)   # rows_in_reset = 60
    win._init_controls(short)
    win._flush_register_writes()
    assert win.sliders[0].field_value() == 0          # rows_in_reset = 0: the longest
    assert any(c.startswith("REG 0") for c in src.device.sent), src.device.sent
    src.device.sent.clear()
    win._init_controls(short)                         # a restart must not redo it
    win._flush_register_writes()
    assert win.defaults_sent and win.sliders[0].field_value() == 60
    assert not src.device.sent
    win.close()
