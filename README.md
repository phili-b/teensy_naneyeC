[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: Teensy](https://img.shields.io/badge/Platform-Teensy-blue.svg)](https://www.pjrc.com/store/teensy40.html)
[![Sensor: NanEyeC](https://img.shields.io/badge/Sensor-NanEyeC-orange.svg)](https://ams.com/)
[![Language: C++](https://img.shields.io/badge/Language-C++-00599C.svg?logo=c%2B%2B&logoColor=white)](https://isocpp.org/)
[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-Live-brightgreen.svg?logo=github&logoColor=white)](https://phili-b.github.io/teensy_naneyeC/)
[![Logic Analyzer: Saleae](https://img.shields.io/badge/Logic%20Analyzer-Saleae-00A9E0.svg?logo=saleae&logoColor=white)](https://www.saleae.com/)
# Teensy NanEyeC

Streams 320×320 mono images from an ams-OSRAM NanEyeC (on a NanoBerry board) to a Windows PC
through a Teensy 4.1 microcontroller, over the sensor's half-duplex single-ended interface (SEIM).

![The live viewer streaming from the NanEyeC through the Teensy: 10-bit, 0 failed rows](docs/images/viewer-live2.png)

*The live viewer, streaming from a real sensor: frame 5265, 10-bit at 12.375 MHz,
0 failed rows. Below the image are the frame statistics, the exposure and register
settings, and the histogram.*

Developed and brought up on the bench by Claude, working in Claude Code with a person on
the hardware: see [How this was built with Claude](docs/built-with-claude.md).

**New here?** Follow [Getting started](docs/getting-started.md): parts, wiring, flashing
and your first image, with a troubleshooting table. The full documentation is published at
<https://phili-b.github.io/teensy_naneyeC/>. [spec.md](spec.md) is the living design
record: decisions, measured ground truth, milestones and risks.

![The bench setup: Teensy 4.1 on a breadboard wired to the NanoBerry board](docs/images/bench-setup.jpg)

Full documentation is an MkDocs site under [docs/](docs/) — overview, hardware and bring-up,
firmware architecture, host usage and API, and a distilled SEIM protocol reference:

```bash
uv sync --group docs
uv run mkdocs serve      # http://127.0.0.1:8000
```

It is published to <https://phili-b.github.io/teensy_naneyeC/> by
`.github/workflows/docs.yml` on every push that touches the docs.

## Status

**Streaming real images** since 2026-09-18.

| | |
|---|---|
| Reference capture decoded | done: 7 frames, every start/stop bit valid |
| Host decode, transport, recorder, viewer | done; 84 tests passing, no hardware required |
| Start-up and row lock | reliable: reference start sequence plus a bit-level row lock |
| 49.5 MHz (default) | **35.5 fps**, 10 minutes with no lost frame, 0 failed rows, 0 concealed pixels ([how](docs/hardware.md#clock-rates)) |
| 24.75 / 12.375 MHz | 17.9 / 8.4 fps, 0 failed rows |
| Error handling | sampling point calibrated at every start; broken pixel words detected and concealed |
| Exposure control | verified: brightness linear in exposure, 1.3 to 102 ms |
| Watchdog | 2 s hardware watchdog, reset cause reported by `ID` |
| Illumination (LED DAC) | firmware and GUI controls done; LEDs not wired on the bench yet |

Three faults on the Teensy side hid first light: an uninvalidated D-cache over the DMA
buffers, a datasheet start sequence that was unreliable on this board, and a too-short
power-off. [docs/hardware.md](docs/hardware.md#first-light-what-it-took-2026-09-18) has the
story, and the tools that found each one.

## Layout

```
spec.md                 living design record: decisions, measurements, milestones
docs/                   MkDocs documentation site
doc/                    UNTRACKED: datasheets, schematic, reference capture
firmware/               PlatformIO project for the Teensy 4.1
  src/naneye_regs.h     register model, frame geometry, exposure and clock maths
  src/seim_unpack.h     12-bit pixel-period extraction (pure logic)
  src/naneye_seim.cpp   LPSPI3 + DMA capture driver and phase sequencer
  src/led_dac.cpp       LTC2630 illumination control
  src/usb_proto.cpp     framing and CRC
  src/watchdog.cpp      RTWDOG hardware watchdog
  src/golden_vector.h   GENERATED: one real row + expected pixels, for SELFTEST
host/naneye/            decoder, transport, sources, viewer, recorder, Saleae client
tools/                  golden-capture decoder, test-vector generator, Saleae
                        bring-up tools (show_bringup, check_alignment, capture/analyze_link)
tests/                  84 tests, no hardware required
```

## The reference capture

`doc/` is **not tracked** — it holds vendor datasheets, the NanoBerry schematic and a
434 MB logic capture: third-party or raw input rather than project source, in a public
repository. See `.gitignore` for the file list and where each comes from.

The important one is `doc/digital.csv`, a Saleae export (2 channels, 500 MS/s, 0.383 s) of a
**working** NanoBerry ↔ Raspberry Pi link. It is the source of every measured figure in
spec.md section 3; `tools/decode_golden.py` turns it into `build/golden/`.

What the repo does carry is the part that matters for testing: `firmware/src/golden_vector.h`
holds one real row from that capture plus its expected pixel values, so both the on-device
`SELFTEST` and `tests/test_unpack.py` check against genuine sensor data. Tests that need the
full capture skip cleanly when it is absent.

## Getting started

The host side is managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync                                    # create the environment from uv.lock
uv run python tools/decode_golden.py       # decode the reference capture (~50 s first run)
uv run pytest                              # 84 tests
uv run --group firmware python -m platformio run -d firmware  # build the firmware
```

The host stack runs with no camera attached, replaying the reference frames through the real
wire protocol:

```bash
uv run python -m naneye.viewer --source replay
uv run python -m naneye.record --source replay --frames 20 --depth 10 --out build/demo
```

With hardware connected, swap `--source replay` for `--source auto`. Depth defaults to
10-bit and the clock to 49.5 MHz (~35 fps):

```bash
uv run python -m naneye.gui                                    # camera GUI (PyQt6)
uv run python -m naneye.record --source auto --frames 200 --out build/run1
```

The GUI shows the image, fps received and displayed, link errors and losses, sliders for
exposure, gain and the analog settings, and the sensor's registers decoded. Keys: `+`/`-`
exposure, `r` recommended settings, `s` save, space pause, `q` quit. It holds the COM port
while it is open. `python -m naneye.viewer` is the older, lighter OpenCV viewer.

Saleae capture automation is an optional extra: `uv sync --extra saleae`.

## Wiring

Teensy 4.1 to the NanoBerry 40-pin Raspberry Pi header (J2). See spec.md section 4.1;
keep SCLK and SDAT short, and leave J1/P1 unconnected.

| Teensy | J2 | Signal |
|---|---|---|
| 27 | 23 | SCLK |
| 26 + 1 (tied at the header) | 19 | SDAT, bidirectional |
| 2 | 33 | NanEye_EN — sensor is off until this is driven high |
| 3 | 31 | LED_VCC_ON |
| 4 / 5 / 6 | 36 / 38 / 40 | LED DAC CS / SDI / SCK |
| VUSB | 2 or 4 | 5 V |
| GND | 6, 14, 20, 25 | ground (and 9 = GNDL if using the LEDs) |

## Device commands

The USB port accepts plain text lines, so it is usable straight from a terminal; replies and
images come back framed (spec.md section 7).

```
ID                      firmware version, settings, last reset cause (normal / WATCHDOG)
POWER 0|1               sensor LDO enable (on waits for >= 1 s off: the rail is slow)
CLK 49500000            SCLK: 49500000, 24750000 or 12375000
START / STOP            begin or end streaming (START power-cycles the sensor first)
DEPTH 8|10|12           8-bit, packed 10-bit (default), or raw 12-bit pixel periods
EXP <rows_in_reset> [rows_delay]
GAIN <ramp_gain> <cds_gain>
REG <0|1> <0xHHHH>      raw register write
LED 0|1                 illumination on/off
LEDI <mA>               LED current, clamped (default ceiling 20 mA of 44.6 mA)
PROBE [rows]            report what the sensor is transmitting (bring-up)
SELFTEST                verify the unpack against the embedded reference row
STATS
SAMPLE 0|1              sample on the normal or the delayed edge
LISTEN [rows]           classify what the sensor sends, row by row, SDAT released
```

More bring-up diagnostics (`START REF`, `START AN`, `ALIGN`, `CLKMEAS`, `WDTEST`) are
described in [docs/firmware.md](docs/firmware.md#diagnostics).

## Bring-up order

Follow the milestones in spec.md section 9, written up as a procedure with pass/fail checks
in [docs/hardware.md](docs/hardware.md#bring-up). In short: `SELFTEST` and `PROBE` before
believing any image, then a Saleae capture decoded independently with
`host/naneye/decode.py` and compared against what the Teensy reported for the same frame.
