# NanEyeC → Teensy 4.1 → Windows USB Camera — Specification

**Status:** draft for approval
**Date:** 2026-09-17
**Scope owner:** developed autonomously by Claude; hardware actions and approvals by P. Baetens

---

## 1. Purpose

Stream live 320×320 images from a NanEyeC miniature image sensor (mounted on a NanoBerry
board) to a Windows PC, using a Teensy 4.1 as the camera interface. The sensor's
single-ended interface (SEIM) is half-duplex on two wires; the Teensy must clock it,
configure it, capture pixel data, and forward frames over USB.

The primary application is **measurement and image capture**: recording frames for later
analysis. This drives requirements for 10-bit fidelity, frame timestamps, explicit
dropped-frame accounting, deterministic exposure/gain control, and lossless framing
(§7, §9).

---

## 2. Agreed decisions

| # | Decision | Value |
|---|----------|-------|
| D1 | PC interface | USB CDC serial + Python viewer/recorder (no UVC) |
| D2 | Sensor variant | **Mono / B&W** (confirmed by measurement, §3.4) |
| D3 | Physical connection | Jumper wires, Teensy ↔ NanoBerry 40-pin RPi header (J2) |
| D4 | Autonomy | Claude installs the toolchain, builds/flashes from CLI, and drives the Saleae via the Logic 2 automation API |
| D5 | Bit depth | Runtime-selectable 8-bit / 10-bit |
| D6 | Clock rate | **Start low: 12.375 MHz, then 24.75 MHz.** Higher rates are a stretch goal |
| D7 | Illumination | On/off **plus DAC current control** (bit-banged, §4.4). Kept minimal: set current in mA, clamped |
| D8 | Board population | Checked part by part on 2026-09-18. **Fitted:** R19 (EN pull-down), R20 (SCLK 24R), R23 (SDAT 24R). **Not fitted:** R13 and R33, the 10k header pull-downs on SDAT and SCLK, as the schematic's NoBom marking says; firmware substitutes the pads' internal pull-downs (§4.3). The first answer here was "everything is mounted" |
| D9 | 5 V rail | Teensy VUSB |
| D10 | Teensy pins | Claude's choice; nothing reserved |

---

## 3. Reference capture — verified ground truth

`doc/digital.csv` is a 433 MB Saleae export (2 channels, 500 MS/s, 0.383 s) of a **working**
NanoBerry ↔ Raspberry Pi link. It has been fully decoded (§9, M0 is therefore already
partly satisfied). Everything in this section is *measured*, not assumed, and takes
precedence over datasheet nominal values where they differ.

### 3.1 Link parameters

| Property | Measured value |
|----------|----------------|
| SCLK frequency | 31.25 MHz (32.0 ns period, exact) |
| Sensor data launch | **~8 ns after each SCLK rising edge** |
| Setup margin at the next rising edge | 24 ns (median), 20 ns (min) |
| Correct sample edge | **Rising** (SPI mode 0, CPOL=0 CPHA=0) |
| Frame readout duration | 40.30 ms (320 × 328 PP × 12 bits @ 31.25 MHz) |
| Frame-to-frame period | 43.1 ms → 23.2 fps (nominal 24 fps; the deficit is host clock gaps) |
| Row pitch | 3936 bits = 328 PP × 12 bits, exact, no exceptions |
| Start/stop bit validity | 100 % over 7 frames (2,240 rows, 716,800 pixels) |

The ~8 ns launch delay is a **round trip**: Teensy clock edge out → sensor → data back.
It is the hard limit on SCLK. At 49.5 MHz (20.2 ns period) the setup window is ~12 ns; at
62.6 MHz (16.0 ns) only ~8 ns. This independently justifies D6.

**On this bench the round trip is longer.** Teensy pad delays and jumper wires add to it,
and at 49.5 MHz rising-edge sampling landed on the transition (79 % of words corrupt), while
falling-edge sampling was clean (2026-09-18, `tools/link_quality.py`). The firmware
therefore measures the sampling point at every start (§6.4) rather than fixing the edge.

### 3.2 Register sequence used by the working host

All writes are 24 bits: `1001` + 3-bit address + 16-bit data (MSB first) + `0`, sent at full
SCLK speed with the MCU driving SDAT. Decoded by `tools/decode_golden.py`, which scans each
INTERFACE MODE window (the 648 PP before every SYNC run):

```
t=0          1 activation clock (SDAT low)
t=0.001037   CONFIG_0 = 0x009F   CONFIG_1 = 0x009F   (SEIM selected, idle still ON)
t=0.001..0.006  ~22 slow clocks (~217 us apart) — bit-banged by the Pi, SDAT low
t=0.0484     CONFIG_0 = 0x009F   CONFIG_1 = 0x0065   (idle OFF → streaming starts)
then         continuous 31.25 MHz clocking, and BOTH registers rewritten
             in every frame's INTERFACE MODE window
```

Decoded field values:

| Register | Value | Fields |
|----------|-------|--------|
| CONFIG_0 | `0x009F` | rows_in_reset=0 (→2 rows in reset, **max exposure**), vrst_pix=**2.6 V** (recommended), ramp_gain=**0.99×** (unity), offset_ramp=**2.2 V** (recommended), output_curr=**9.6 mA** (max drive) |
| CONFIG_0 | `0x7F9F` | as above but rows_in_reset=127 (→256 rows in reset) — the host's auto-exposure, written for the last two frames |
| CONFIG_1 (power-up) | `0x009F` | output_mode=**SEIM**, mclk_mode=2 (/2), vref=2.0 V, cvc_curr=3, **idle_mode=1**, high_speed=1 |
| CONFIG_1 (streaming) | `0x0065` | output_mode=**SEIM**, mclk_mode=1 (default), vref=**2.1 V** (recommended), cvc_curr=**1** (recommended), **idle_mode=0**, high_speed=1 → 31.1 MHz |

Notable points:

- The data line stays low for the first 1.28 M clocks (idle_mode=1), then streams as soon
  as the second write clears idle.
- Final settings `mclk_mode=1, high_speed=1` → nominal 31.1 MHz internal MCLK against a
  31.25 MHz external SCLK (+0.5 %), confirming the required external/internal clock match.
- Apart from `rows_in_reset`, the host uses the datasheet's **recommended** values
  throughout, at unity analog gain and maximum output drive strength.
- The host rewrites **both registers every frame**. This is how it does auto-exposure, and
  it means a steady-state implementation should keep driving the interface window rather
  than configuring once.
- `rows_in_reset` 0→127 (t_exp 105,616 → 22,304 PP, a 4.74× reduction) was written in the
  window before frame 6 but only takes effect on **frame 7** — one frame of latency, as
  expected from rolling-shutter integration overlapping readout. Measured signal above
  black scales consistently with the §5.4 formula (black level ≈ 200 DN; to be measured
  properly in M6).

**We will replicate this exact sequence for bring-up**, since it is proven on this board,
then re-tune (§6.7).

### 3.3 Defect observed in the reference host — an explicit non-goal

The Pi restarts its DMA every 65,532 bytes, producing 50–200 µs clock gaps **mid-row**.
Each gap corrupts exactly three consecutive pixels:

```
row 129:  266 274 270 278 271 275 | 1020  84   0 | 272 274 277
row 128:  275 271 276 271 274 274 |  272 273 272 | 275 277 272   (same columns, clean)
```

This is the effect AN000611 warns about. It costs the reference ≥2 pixel triplets per
frame. **Our design must not do this** (§6.5), and this is a measurable acceptance
criterion (§9, M4).

### 3.4 Sensor variant confirmation

Bayer sub-lattice means on a smooth image region: 279.98 / 280.72 / 280.64 / 281.18 DN —
spread < 0.5 %. No colour filter array. Mono confirmed (D2); no debayering anywhere in the
pipeline.

### 3.5 First-frame behaviour

Frame 0 after idle-off is fully saturated (mean 1020 DN of 1023). Frames 1–6 are normal
(mean ≈ 274, temporal σ = 2.74 DN). The datasheet's warning to discard the first frame
after power-on / idle-deactivation is confirmed and must be implemented.

---

## 4. Hardware

### 4.1 Signal path

Onboard sensor `S1` (NanEyeC 2×2 SGA) pads: `A1` VDDA, `A2` VSS, `B1` SCLK/DATA-,
`B2` SDAT/DATA+. From the schematic:

```
Teensy pin 27 ──────────────── J2.23 ──[R20 24R]── S1.B1  (SCLK, host→sensor)
Teensy pin 26 ┐                                    C11 15pF
Teensy pin  1 ┴─── tie at J2 ── J2.19 ──[R23 24R]── S1.B2  (SDAT, bidirectional)
                                                    C13 15pF
Teensy pin  2 ──────────────── J2.33 ── R19 10k pd ── TPS71701 EN → VCC_SENSOR 3.3 V
Teensy VUSB   ──────────────── J2.2 / J2.4  (5vs)
Teensy GND    ──────────────── J2.6, 14, 20, 25  (GND)
```

Optional illumination (§4.4):

```
Teensy pin  3 ──── J2.31  LED_VCC_ON_1   (enables LT3473 boost)
Teensy pin  4 ──── J2.36  LED_DAC_CS_N   ┐ LTC2630 12-bit DAC, bit-banged
Teensy pin  5 ──── J2.38  LED_DAC_SDI    │ (sets LT3092 LED current)
Teensy pin  6 ──── J2.40  LED_DAC_SCK    ┘
Teensy GND    ──── J2.9   GNDL           (separate LED ground — only if LEDs used)
```

Wiring rules:
- SCLK and SDAT leads **as short as possible**, each with its own adjacent ground return.
- **Do not connect anything to J1 (FPC) or P1 (6-pin)**: they sit on the same D+/D- nets as
  the onboard sensor. A second camera there would contend on the bus.
- Teensy 4.1 I/O is 3.3 V and not 5 V tolerant; the sensor is 3.3 V. Direct connection is
  correct. Sensor V_IH,min = VDDA − 0.3 V = 3.0 V, satisfied by Teensy's 3.3 V push-pull.

### 4.2 Power

`5vs` from Teensy VUSB → TPS71701 LDO → `VCC_SENSOR` 3.3 V, gated by `Naneye_EN`
(10 k pulldown, so the sensor is **off until the Teensy drives pin 2 high** — this is the
designed power-on reset control and the recovery mechanism of last resort).

Sensor power is negligible (9.7 mW in SEIM). LED draw is bounded by the DAC ceiling (§4.4).

**The rail discharges slowly.** Measured after EN goes low: still 0.5 V after 143 ms,
below 0.1 V only after ~630 ms — nothing on the NanoBerry actively discharges it. A power
cycle shorter than that may not give the sensor a clean power-on reset, and the start-up
sequence (§6.4) depends on one: starts after a 0.4 s off-time failed. `seim::power(true)`
therefore enforces **at least 1 s off** since the last power-down, however soon it is
called, and every `START` power-cycles the sensor first.

### 4.3 Half-duplex direction control

SDAT is driven by the Teensy during INTERFACE MODE and by the sensor at all other times.
The datasheet is explicit that the host **must** tristate before the sensor starts
transmitting, and recommends driving the bus through the interface window to avoid EMI
pickup on a floating line.

Two refinements, both from reading the datasheet against the board rather than from
hardware:

- **The host drives 647 of the 648 PP, not all of them.** DS000503 §6.4.3 says the sensor
  itself transmits an end-of-interface word (0x015 in SEIM) in the last PP, which is also
  why register writes are forbidden there. AN000611's recipe drives all 648. The reference
  capture cannot settle it, because the host's GPIO would out-drive the sensor's
  current-limited output either way. Releasing costs nothing if the sensor is silent and
  avoids a fight every frame if not; the firmware receives that PP and reports it, so M2
  settles the question.
- **R13 and R33 are not fitted** (D8), and the SPI pads are configured with no pull, so
  both nets would float whenever undriven. The firmware enables the pads' internal 100k
  pull-downs on SCLK and both SDAT pins. SCLK matters most: a floating clock while the
  sensor is powered could inject a spurious edge and slip word alignment.

**Primary scheme (deterministic):** two Teensy pins tied together at the header —
pin 26 (`LPSPI3_SDO`) and pin 1 (`LPSPI3_SDI`). Direction is one IOMUXC write:

| Phase | Pin 26 mux | Pin 1 |
|-------|-----------|-------|
| INTERFACE MODE | `LPSPI3_SDO` (drives) | reads (harmless) |
| SYNC / DELAY / READOUT | GPIO input, hi-Z | reads sensor data |

**Alternative to evaluate later:** true 3-wire half-duplex on pin 26 alone via
`LPSPIx_CFGR1[PINCFG]=10b` (SOUT used for both input and output), saving a wire and ~5 pF
of pad capacitance. Deferred because the two-pin scheme has no peripheral-behaviour
unknowns. Note the Raspberry Pi reference does exactly this — it drives SDAT from
SPI0 MOSI (header pin 19) with MISO (pin 21) unconnected — so it is known to work
electrically.

### 4.4 Illumination

The NanoBerry has VIS (`D3`/`D4`, DURIS S2) and NIR (`D1`/`D2`, SFH 4053) LED strings in
parallel between `+VCC_LED` and `LED_CATHODE`, fed by an LT3473 boost and sunk by an
LT3092 programmable current source. The set point comes from an **LTC2630 12-bit DAC**:

```
LTC2630 VOUT ──[R9 1k]──┬── LT3092 SET          LED_CATHODE ──[R7 12R]── LT3092 IN
                        └── C10 1nF                                       LT3092 OUT
                                                                              │
                                                                         [R11 56R]
                                                                              │
                                                                            GNDL
```

The LT3092 servos its OUT pin to the SET voltage, and R9 only carries the internal 10 µA
bias, so `V_SET ≈ V_DAC` and:

```
I_LED ≈ V_DAC / R11 = V_DAC / 56 Ω
```

With the `-LZ12` part (2.5 V full scale, 12-bit): **0 → 44.6 mA, 10.9 µA per LSB**.
The `Z` suffix means power-on reset to **zero scale**, which resolves R11 — `LED_VCC_ON`
alone yields ≈ 0.18 mA (the 10 µA bias term), i.e. no useful light. The DAC must be
programmed, which is why D7 now includes it.

**Implementation (deliberately minimal):** the LTC2630 is a write-only 24-bit SPI device
with no readback and no timing constraints that matter at these speeds, so it is
bit-banged on 3 GPIOs — no second SPI peripheral, no contention with the camera bus.

```
CS low; shift 24 bits MSB-first (SDI set, then SCK high, then SCK low); CS high to load
  byte 0 : 0x30      command "write and update, power up"
  byte 1 : code >> 4       12-bit code left-justified in 16 bits
  byte 2 : (code & 0xF) << 4
```

Firmware exposes this as a current in mA (converted via the formula above) and clamps to a
configurable ceiling, defaulting to 20 mA so a mistyped command cannot dump 45 mA into the
LEDs or the USB rail. Power-down (command `0x40`) is used for `LED 0`, alongside dropping
`LED_VCC_ON`.

**To verify on first contact with hardware:** R16 and R17 are 0 Ω jumpers selecting the NIR
and VIS strings respectively. If *both* are fitted (likely, per D8), the two strings sit in
parallel across one current sink and the split between them is set by their forward
voltages, not by us — so "45 mA" is a total, unevenly shared. Normally only one would be
fitted. Confirm visually and note which; it changes nothing in the firmware but matters for
interpreting radiometry.

---

## 5. Sensor interface — protocol summary

### 5.1 SEIM basics

- Host drives SCLK. Sensor shifts out one bit per **rising** edge on SDAT.
- Everything is in 12-bit **pixel periods** (PP): `start(1) + 10-bit data + stop(0)`.
- Pixel word: start = 1, stop = 0, payload = 10-bit pixel, MSB first.
- Training word `0x555` = `010101010101` (SYNC, DELAY, start-of-row).
- Training word `0xAAA` = `101010101010` (INITIAL PRE-SYNC, and the first row of the
  first frame after power-on only).
- End of frame `0x000`, sent 8×.
- No chip select. The sensor cannot share the bus.

### 5.2 Frame structure

| Phase | Duration (PP) | Direction |
|-------|--------------|-----------|
| INTERFACE MODE | 648 | host drives SDAT |
| SYNC MODE | 2 × 328 = 656 | sensor sends `0x555` |
| DELAY MODE | (16·rows_delay + 2) × 328, default 656 | sensor sends `0x555` |
| READOUT | 320 × (8 × `0x555` + 320 pixels) = 104,960 | sensor |
| EOF | 8 × `0x000` | sensor |
| **Total (default delay)** | **106,928 PP** | |

Row boundaries are found by the `...0101` → `11` transition (last training bit 1 followed
by the pixel start bit 1), which is how the reference capture was decoded: it yielded a row
pitch of exactly 3936 bits with zero exceptions.

### 5.3 Clock plan

The external SCLK must closely match the sensor's internal MCLK or dynamic range is lost
(SCLK too fast → ADC clips low, reducing full-scale; too slow → raised black level).

LPSPI root clock: `CCM_CBCMR[LPSPI_CLK_SEL]=11b` (PLL2_PFD2, 396 MHz), `LPSPI_PODF=3`
(÷4) → **99 MHz**. Then `SCK = 99 / (SCKDIV + 2)`:

| SCKDIV | SCLK | Sensor mode (`high_speed=0`) | Nominal MCLK | Error | PP rate | Frame time | fps | 8-bit MB/s |
|--------|------|------------------------------|--------------|-------|---------|-----------|-----|-----------|
| 6 | 12.375 MHz | mclk_mode=2 (÷2) | 12.3 MHz | +0.6 % | 1.031 MHz | 103.7 ms | 9.6 | 0.99 |
| 2 | 24.75 MHz | mclk_mode=1 (default) | 24.7 MHz | +0.2 % | 2.063 MHz | 51.8 ms | 19.3 | 1.98 |
| 0 | 49.5 MHz | mclk_mode=0 (2×) | 49.1 MHz | +0.8 % | 4.125 MHz | 25.9 ms | 38.6 | 3.95 |

All three land within 1 % of nominal — better than the reference host's +0.5 %, and better
than the 6–7 % error a 132 MHz root clock would give. The resulting 9.6 / 19.3 / 38.6 fps
match the datasheet's 9 / 19 / 38 fps exactly.

`high_speed=1` modes (15.7 / 31.1 / 62.6 MHz) have no clean divisor from available PLLs
(best: 480/4 → 60.0 MHz, −4 % against 62.6 MHz) and exceed the round-trip timing budget of
§3.1. They are out of scope, with one exception: 31.25 MHz is reachable if we ever want to
reproduce the reference capture bit-for-bit (root 528/4 = 132 MHz, SCKDIV=2 → 33.0 MHz is
+6 %, so an exact match would need a dedicated PLL setup — deferred).

### 5.4 Exposure and gain

```
t_exp = t_rows_btw_frame + t_rows_matrix − t_rows_in_reset − t_rows_in_readout       [PP]
t_rows_btw_frame = 648 + 656 + (16·rows_delay[4:0] + 2)·328
t_rows_matrix    = 320·328 + 8 = 104,968
t_rows_in_reset  = (2·rows_in_reset[7:0] + 2)·328
t_rows_in_readout= 656
```

- `rows_in_reset` — fine exposure control, no frame-rate cost. Range ≈ 0.5 ms (max reset)
  to ~full frame (reference used 1 → near-max exposure).
- `rows_delay` — coarse exposure extension, reduces frame rate; up to 261 ms at 12.3 MHz.
- Gain: `ramp_gain` (0.79 / 0.99 / 1.32 / 1.97) × `cds_gain` (1.3 or 2.0).

Firmware computes `t_exp` from the formula and reports it in every frame header so
recordings are self-describing. A register write takes effect **one frame later** (§3.2), so
the header must report the settings that were actually in force for the frame it describes,
not the most recently written ones.

### 5.5 Register access

24 bits: `1001` + `00a` + 16 data bits (MSB first) + `0`. Address 0 = CONFIG_0,
1 = CONFIG_1. Captured on SCLK rising edges; SDAT should change on falling edges. Rules:
never send config in the first clock pulse after power-up (send ≥1 activation clock first),
and never in the last PP of INTERFACE MODE.

---

## 6. Firmware architecture

Target: Teensy 4.1 (i.MX RT1062, 600 MHz). Framework: Arduino/Teensyduino via PlatformIO
(`teensy41`), with direct register access for LPSPI/DMA/IOMUXC.

### 6.1 Peripheral choice

**LPSPI3 (Teensy "SPI1": SCK 27, SDO 26, SDI 1)**, master, mode 0, with eDMA.

Rationale: AN000611 explicitly recommends SPI+DMA, and LPSPI on the i.MX RT1062 supports
**arbitrary frame sizes (`TCR[FRAMESZ]`, 8–4096 bits)** — including 12, which is exactly one
pixel period. LPSPI3 is chosen over the default LPSPI4 to keep the 60 MHz-class clock off
pin 13 (onboard LED capacitance). Pin 1 and 26 are adjacent-ish on the header edge and
pin 27 is next to 26, keeping the wire bundle short.

### 6.2 Frame size options

| Option | `FRAMESZ` | RX word → memory | Unpack | Risk |
|--------|-----------|-----------------|--------|------|
| **A (primary)** | 12 bits | 1 PP → one `uint16` | `(w>>1) & 0x3FF`, plus start/stop validation | LPSPI may insert idle SCK cycles between frames |
| **B (fallback)** | 3936 bits = 328 PP = **one row** | 123 × `uint32` | 8 pixels per 3 words (96 bits = 8 PP exactly, fixed repeating pattern) | Gaps possible only at row boundaries, where they are harmless |

Option A makes unpacking trivial and costs 2 bytes per PP (214 KB/frame of DMA writes,
8.3 MB/s at 38.6 fps — comfortable). Decision criterion: a Saleae capture must show
**no SCK gap > 1 PP** within a row; if option A cannot meet that with `TCR[CONT]`
continuation, switch to option B. This is settled in M4.

### 6.3 Memory plan

Row-chunked ring buffer, **not** whole-frame buffering:

- RX ring: 16 rows × 328 PP × 2 B = 10.5 KB in `DMAMEM` (OCRAM).
- **`DMAMEM` is cached.** OCRAM sits behind the Cortex-M7's write-back D-cache, and the
  eDMA writes RAM behind the cache's back. Every row buffer is invalidated
  (`arm_dcache_delete`) before its transfer is armed and again when it completes, and each
  buffer is padded to whole 32-byte cache lines. Without this the sensor streamed perfect
  frames on the wire — seen on the logic analyser — while the firmware read zeros or stale
  words: the bug that hid first light for most of a day (2026-09-18).
- Decoded frame buffer: 320 × 320 × 1 B (8-bit) or packed 10-bit (128 KB), double-buffered.
- Total well under the 512 KB OCRAM + 512 KB RAM1 budget. **PSRAM is not required.**

Per-row time budget at 24.75 MHz is 159 µs; unpacking 320 pixels is a few hundred
cycles — roughly two orders of magnitude of headroom.

### 6.4 Capture state machine

```
POWER_CYCLE   → EN low (if on), ≥ 1 s off (§4.2), EN high, 5 ms
INIT_CONFIG   → 1 activation clock; CONFIG_0; CONFIG_1 (idle=1, SEIM), bit-banged at ~1 MHz
                (replicates §3.2 step 1)
RUN_IN        → one frame's worth of clocks (1,279,366) with SDAT driven low — as the
                reference host does
START         → CONFIG_0; CONFIG_1 with idle=0 and rows_delay=0 at SCLK rate, zeros to the
                end of a 648 PP window, release SDAT (§3.2 step 2)
CALIBRATE     → 1024 bits of the alternating training at each of 4 sampling points (rising or
                falling edge, with or without CFGR1[SAMPLE]); keep the one with fewest breaks
PRESYNC       → first row received: must be mostly training ("is there a sensor?")
ROW_LOCK      → scan the bits for row 1's 8×0x555 → "11" break; clock the exact number of
                bits to the next row boundary; that row must show 8 training words, and
                the next 8 rows of real pixels are checked and their bad words reported
FRAME_0       → rest of the first frame + EOF clocked and DISCARDED (saturated — §3.5)
STREAM        → per frame: 648 PP interface window (drive SDAT: 2 register writes, then
                zeros, sensor owns the last PP), tristate, sync + delay, 320 rows, 8 PP EOF
FAULT         → capture failure: power-cycle and START again
```

Why this and not the datasheet's sequence. AN000611's single write (activation clock,
CONFIG_1 idle-off, 10 alignment clocks) started only sometimes on this board, and when it
did, its fixed phase count put every row transfer 2 clocks late, so every row failed. The
reference host's sequence starts every time. And the phase after it is *found*, not
counted: how many SCLK clocks the first frame's training lasts varies from start to start
(12,074–12,084 bits measured, not whole pixel periods), because parts of the sensor run on
its own oscillator. From the first locked row on, everything is SCLK-counted and
deterministic — every row of every later frame lands exactly (`tools/check_alignment.py`).
Row 0 of the first frame is trained with 0xAAA, which runs straight on into its first
start bit, so the lock is taken on row 1.

### 6.5 Continuous clocking requirement

The sensor's ADC counter runs off its own oscillator, so any host clock gap desynchronises
readout from conversion and corrupts pixels (§3.3, measured: 3 pixels per gap). Therefore:

- The TX DMA must keep the LPSPI TX FIFO fed so SCK never stalls mid-row.
- DMA chunk boundaries must align to row ends (AN000611's recommendation).
- Firmware counts and reports any detected discontinuity rather than hiding it.

### 6.6 Sync loss detection

Every row is validated: 8 training words must read `0x555`, all 320 pixel words must have
start = 1 and stop = 0. Rows failing validation increment a counter in the frame header; a
threshold triggers re-sync. Full validation is cheap and, per §3.1, is expected to pass
100 % of the time — so any failure is real information.

**Concealment.** SEIM carries no redundancy beyond each word's start and stop bits, so
errors in the ten data bits cannot be corrected or even detected; errors that break the
framing can be detected, and then the value is known to be wrong. Such a pixel is replaced
by the mean of its nearest intact neighbours on the row and counted in the header
(`pixels_concealed`, flag `CONCEALED`); `CONCEAL 0` leaves it as received instead. Rows
with more than 32 broken words are not concealed: that is a lost row, not a damaged one.
`SYNC_LOST` now means a row's training words failed (the phase is in doubt), not that a
pixel was damaged. `INJECT n` corrupts n words per frame to test all of this on a clean
link: with 500 per frame, the concealed image differs from a clean one by 2.1 DN on
average against 1.8 DN of frame-to-frame noise, and 3 pixels stay more than 100 DN off,
against 416 without concealment.

### 6.6b Watchdog

RTWDOG (WDOG3), 2 s timeout, clocked from the 32 kHz LPO so it survives any PLL mistake.
Fed from `loop()`, per row in `LISTEN`, and while waiting out a power cycle; the longest
legitimate blocking operation is ~0.7 s. The reset cause is latched at boot and reported by
`ID` (`last reset: WATCHDOG`). `WDTEST` hangs on purpose: measured, the port drops after
1.97 s and is back at 2.25 s. A flash also usually reads as a watchdog reset, because the
old image parks in the bootloader hand-off with the watchdog still running — harmless.

### 6.7 Tuning after bring-up

The reference's static settings (§3.2) are a sound starting point: recommended `vrst_pix`,
`offset_ramp` and `vref`, unity `ramp_gain`, max output drive. Two changes to consider:

- `rows_in_reset=0` is maximum exposure and will clip in normal room light. The host worked
  around this with per-frame auto-exposure; we expose `rows_in_reset` as a command (§7) and
  leave any AE loop to the PC side, where the measurement application can control it.
- `output_curr=9.6 mA` (max drive) is the right default for fast edges on jumper wires, but
  it is also the noisiest choice for the analog front end. Once the link is reliable, sweep
  it down and check whether temporal noise improves without hurting setup margin.

Defaults will be chosen from measured black level and saturation (M6).

---

## 7. USB protocol

Single USB CDC serial port (one COM port, no driver questions on Windows 11).

**Device to host is always framed**, images and text alike, so log output can never be
mistaken for image data. **Host to device is plain ASCII lines**, which keeps the port
usable from a plain terminal. The asymmetry is deliberate: framing matters for the
high-rate direction that has to be parsed by machine, and costs nothing in the direction a
human types into. The `command` packet type is reserved in case that changes.

```
offset size field
 0      4   magic  "NANE"
 4      1   version = 1
 5      1   type    0=image, 1=command, 2=response, 3=log
 6      2   header_len
 8      4   payload_len
12      4   frame_counter          (monotonic, gaps = drops)
16      4   timestamp_us           (ARM cycle counter based)
20      2   width  = 320
22      2   height = 320
24      1   format  0=8-bit, 1=10-bit packed (5 B per 4 px), 2=raw 12-bit PP
25      1   flags   bit0 sync_lost, bit1 clock_gap, bit2 first_frame_discarded
26      2   rows_failed_validation
28      4   sclk_hz
32      4   exposure_pp
36      2   cfg0
38      2   cfg1
40      4   frames_dropped_total
44      4   reserved
48      4   crc32 (header + payload)
52          payload
```

ASCII commands (type 1), one per line, for terminal and script use alike:

```
ID                     → firmware version, chip UID
POWER 0|1              → Naneye_EN
CLK 12375000|24750000  → SCLK (also sets matching mclk_mode)
START / STOP
DEPTH 8|10|12
EXP <rows_in_reset> [rows_delay]
GAIN <ramp_gain> <cds_gain>
REG <0|1> <0xHHHH>     → raw register write
LED 0|1                → LED_VCC_ON + DAC power-up/down
LEDI <mA>              → LED current, 0..20 mA (ceiling raised with LEDMAX)
STATS                  → counters
SELFTEST               → run the unpack over an embedded golden buffer (§8.3)
```

Throughput: worst case in scope is 4.94 MB/s (10-bit at 49.5 MHz). Teensy 4.1 USB HS CDC
sustains well above that. Back-pressure is handled by **dropping whole frames and counting
them**, never by emitting a partial frame.

---

## 8. Host software (Windows)

`host/naneye/`

- `transport.py` — framing, CRC, reconnect; yields `(header, payload)`.
- `decode.py` — 12-bit word → pixel unpack, row/frame framing, validation. **Also decodes
  Saleae exports**, so the same code validates both the live link and logic captures.
- `viewer.py` — live view (OpenCV), histogram, saturation/black-level readout, FPS and
  drop counters.
- `record.py` — lossless capture to disk with sidecar metadata (exposure, gain, SCLK,
  timestamps), 10-bit preserved.
- `analyze.py` — dark frame, temporal noise, FPN, black level and full-scale vs SCLK/MCLK
  mismatch.
- `saleae.py` — Logic 2 automation: capture N ms on 2 channels at 500 MS/s, export, decode,
  compare against what the Teensy reported for the same frames.

Dependencies: `numpy`, `pillow`, `opencv-python`, `pyserial`, `logic2-automation`.
Python 3.13 and Saleae Logic 2 are already installed; `numpy` and `pillow` are in place and
were used to produce the §3 results.

---

## 9. Milestones and acceptance criteria

| M | Goal | Acceptance criterion |
|---|------|---------------------|
| **M0** | Tooling + golden decode | PlatformIO installed; blink builds and flashes from CLI; Saleae automation API reachable and scripted capture works. Golden decode of `digital.csv` reproduces §3 numbers. *(decode half already done)* |
| **M1** | Clock and configure | Saleae capture of the Teensy's own output contains the activation clock plus `CONFIG_0=0x009F` / `CONFIG_1=0x009F` then `0x0065`, bit-identical to §3.2, at 12.375 MHz, and decodes cleanly with `tools/decode_golden.py` |
| **M2** | First light | Teensy reports ≥ 8 consecutive `0x555` words and achieves word alignment; `SYNC_LOCK` reached |
| **M3** | First frame | One frame over USB → PNG on the PC. All 102,400 pixels have start=1/stop=0. A simultaneous Saleae capture, decoded independently by `decode.py`, is **pixel-identical** to what the Teensy sent |
| **M4** | Continuous streaming | 60 s at 12.375 MHz then 24.75 MHz: zero dropped frames, zero failed rows, and a Saleae capture showing **no SCK gap > 1 PP within a row** (i.e. none of the §3.3 pixel corruption) |
| **M5** | Control | Exposure, gain, bit depth and SCLK settable at runtime. Measured `t_exp` matches the §5.4 formula within 1 % (verified by a light-level sweep); black level responds as predicted |
| **M5b** | Illumination | `LED`/`LEDI` work; measured LED current matches `V_DAC / 56 Ω`; image brightness scales with commanded current. **Done 2026-09-23**: the DAC writes were read off the wire (`0x30` + code = `I / 44.6 mA × 4095`, rail raised before the first code and dropped after the power-down) and image brightness is linear at 1.98 DN/mA from 0 to 20 mA. The current itself is still inferred from the code, not metered |
| **M6** | Measurement readiness | Lossless recording with metadata; dark-frame/temporal-noise/FPN report; black level and full-scale characterised vs SCLK-to-MCLK mismatch. Temporal noise should land near the reference's 2.74 DN |
| **M7** | Stretch | 49.5 MHz / 38.6 fps sustained; single-pin half-duplex (§4.3). **49.5 MHz reached 2026-09-18**: 35.3 fps sustained for 60 s, 0 failed rows (the 38.6 fps ceiling assumes no gaps between row transfers). Single-pin not attempted |

---

## 10. Repository layout

```
spec.md
doc/            datasheets, schematic, digital.csv (golden capture)
firmware/       platformio.ini, src/ (main, seim driver, usb transport)
host/naneye/    transport, decode, viewer, record, analyze, saleae
tools/          golden-capture decode, bring-up scripts
tests/          golden decode regression, protocol round-trip
```

---

## 11. Risks and open items

| # | Risk | Mitigation |
|---|------|-----------|
| R1 | Jumper-wire signal integrity ≥ 25 MHz (24 R series + ~30 pF of pads, caps and connector stubs) | **Resolved 2026-09-18:** all three rates run clean on jumper wires. 49.5 MHz first looked like a slew-rate limit; it was the sampling point (§3.1), now calibrated at every start. 60 s at 49.5 MHz: 0 failed rows, 0 concealed pixels |
| R2 | ~8 ns measured round-trip delay caps SCLK (longer on this bench) | Stay ≤ 49.5 MHz; treat 62.6 MHz as out of scope. The sampling-point calibration absorbs the delay up to 49.5 MHz |
| R3 | LPSPI may insert idle SCK cycles between 12-bit frames → §3.3 pixel corruption | `TCR[CONT]`; fallback to one-row `FRAMESZ` (option B, §6.2); verified by Saleae in M4 |
| R4 | Sensor has no chip select and cannot share the bus | LPSPI3 dedicated to the camera; LED DAC bit-banged on GPIO |
| R5 | Changing the LPSPI root clock affects all LPSPI instances | Only LPSPI3 is used; no other SPI peripheral in the design |
| R6 | Bus contention if the Teensy fails to tristate SDAT in time | Datasheet says this degrades integrity but is not destructive; direction switch is a single register write; the 24 R series resistors limit current |
| R7 | USB back-pressure causing partial frames | Drop whole frames only, and count them in the header |
| R8 | First frame after idle-off is saturated | Confirmed in §3.5; always discarded, flagged in header bit 2 |
| R9 | Teensy VUSB current budget with LEDs on | Bounded by design: LED current is DAC-limited to 44.6 mA absolute and 20 mA by default (§4.4), so the USB budget is safe; sensor draw is negligible |
| R10 | 433 MB golden CSV is slow to parse (~47 s) | Cache the extracted bitstream as `.npy`; use Saleae **binary** export for new captures |
| R11 | **Resolved:** the LTC2630 powers up at zero scale, so `LED_VCC_ON` alone gives ≈ 0.18 mA — no useful light | DAC control brought into scope (D7, §4.4), bit-banged on 3 GPIOs |
| R14 | If R16 and R17 are both fitted, the VIS and NIR strings share one current sink and the split between them is set by their forward voltages | Firmware unaffected; confirm the jumpers on first hardware contact and record which strings are active (§4.4) |
| R12 | **Open:** exact `high_speed` clock matching is unavailable from the PLL dividers | Out of scope (§5.3); non-HS modes match within 1 % |
| R13 | Teensy pins 26/1 tied together adds pad capacitance to SDAT | Acceptable at ≤ 24.75 MHz; single-pin half-duplex (§4.3) is the fix if it bites at 49.5 MHz |
| R15 | **Resolved:** row DMA into cached OCRAM read stale data | D-cache invalidation around every row transfer (§6.3) |
| R16 | **Resolved:** first-frame phase not deterministic in SCLK clocks; datasheet start unreliable | Reference start sequence plus a bit-level row lock (§6.4); ≥ 1 s power-off (§4.2) |

---

## 12. Out of scope

- UVC / standard webcam enumeration (D1; may be revisited after M6).
- LVDS mode — needs a comparator front-end and > 750 MHz sampling; impossible on a Teensy.
- Colour processing / debayering — sensor is mono (D2, §3.4).
- Multi-camera synchronisation.
- On-Teensy image processing beyond unpacking and optional 10→8-bit reduction.

---

## 13. References

- `doc/NanEyeC_DS000503_3-00.pdf` — datasheet v3-00. §3 pins, §5 electrical, §6.3.2.2 SEIM
  sequence, §6.4.2 SEIM encoding, §6.4.3 register access, §6.5 exposure, §7 registers.
- `doc/NanEyeC_AN000611_2-00.pdf` — AN000611 "NanEyeC MCU Interface", SPI+DMA reference
  implementation, DMA-gap warning, sampling-edge guidance.
- `doc/nanoberry_board.pdf` — NanoBerry schematic rev 1.0. Sheet 2 sensor + RPi header,
  sheet 3 LED current source, sheet 4 sensor power.
- `doc/digital.csv` — golden Saleae capture of a working link; decoded in §3.
