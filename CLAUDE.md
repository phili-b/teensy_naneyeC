# Working on this repository

A NanEyeC camera sensor (ams-OSRAM, 1 mm², mono or colour) on a NanoBerry board, driven by a
Teensy 4.1, streaming to a Windows PC over USB. Firmware in C++ (PlatformIO), host tools in
Python (uv). `spec.md` is the living design record; `docs/` is the published site.

## Commands

```bash
uv run pytest                                    # 113 tests, none need hardware
uv run python -m naneye.gui                      # the camera GUI (holds the serial port)
uv run --group docs mkdocs serve                 # docs at 127.0.0.1:8000
cd firmware && uv run pio run                    # build
cd firmware && uv run pio run -t upload          # flash
```

`pio run -t upload` fails with "error writing to Teensy" perhaps half the time. Run it
again; it works on the second try. Nothing is wrong.

## The bench

- Teensy on **COM11**, opened exclusively: if the GUI is running, no script can talk to the
  device, and the error is a bare `PermissionError(13)`. Close one before starting the other.
- The **Saleae Logic Pro 16** is driven through Logic 2's MCP server on
  `http://127.0.0.1:10530` (`naneye.saleae.Logic`). Logic 2 must be running — start it with
  `Start-Process "C:\Program Files\Logic\Logic.exe"` and give it ~20 s.
- Probes are shared and the map depends on what is being measured. Link work: D0 SDAT at
  Teensy pin 1, D1 NanEye_EN, D2 SDAT at pin 26, D3 SCLK at pin 27, D4 VCC_SENSOR, D5/D6 the
  sensor end. LED work: D4 DAC CS, D5 DAC SDI, D6 DAC SCK, D7 LED_VCC_ON. Both sets cannot be
  captured at once — `tools/wire_figures.py` and `tools/led_figures.py` say so in their
  docstrings.
- Run bench scripts with **`python -u`**. Without it, stdout is block-buffered when the
  output is redirected and a script that is working fine looks like a hang.
- A colour sensor is fitted. The firmware knows (`CFA BGGR`, kept in EEPROM); the GUI reads
  it out of each frame header.

## Things that cost a day to learn

- **DMAMEM is cached.** Any buffer the eDMA writes needs `arm_dcache_delete` before it is
  read, and cache-line padding. A missing invalidate looks exactly like a sensor that only
  sends training patterns.
- **Believe the wire.** Three separate faults looked like signal integrity and were not: a
  cache bug, a 2-clock phase slip, and a row-lock search window that was too small. When the
  firmware and the logic analyser disagree, the logic analyser is right.
- **The sensor needs ≥1 s powered off** before a restart, measured from the rail's decay.
- **Start-up constants are measurements of one sample**, not constants. The colour module
  sends twice as much training before its first row as the mono one did.
- Where the datasheet and a measurement disagree, the measurement wins and the case goes in
  `docs/datasheet-crosscheck.md`.

## Conventions

- **Commit and push after each finished, tested task**, without asking. Never force-push.
- `doc/` is untracked on purpose: vendor datasheets, the board schematic and a 434 MB
  reference capture. The repository is public. Do not commit them, or anything derived that
  would reproduce them. `.claude/` and `tools.json` stay out of commits too.
- Hardware claims in the docs are measurements or they are labelled as not measured. Write
  down the wrong diagnosis as well as the right one — several pages do, and they are the
  useful parts.
- The docs are written for other people, in prose, with a light touch of humour. Keep it.
- Firmware end of commit messages and PR bodies as the session's attribution reminder says.

## Layout

```
firmware/src/     naneye_seim.cpp is the driver; main.cpp is the command shell
host/naneye/      protocol, transport, sources, decode, regs, color, isp, gui, viewer,
                  record, saleae, accounting, fake
tools/            reference-capture decoder, bring-up and figure tools
tests/            no hardware required
docs/             MkDocs Material site, published on push
```
