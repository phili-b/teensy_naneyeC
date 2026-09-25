// NanEyeC SEIM capture driver for Teensy 4.1 (LPSPI3 + eDMA).
//
// The whole frame is a deterministic sequence of clock counts (spec.md section 5.2), so the
// driver is a phase sequencer: emit exactly the right number of clocks per phase and switch
// the SDAT direction at the phase boundaries.
//
//   INTERFACE   648 PP = 7776 bits = 324 frames of 24 bits, MCU driving SDAT
//                 frame 0   CONFIG_0 write
//                 frame 1   CONFIG_1 write
//                 frames 2+ zeros (the datasheet asks for the bus to stay driven)
//   SYNC+DELAY  (656 + delay) PP, clocked and discarded, SDAT hi-Z
//   READOUT     320 rows x 3936 bits, captured by DMA
//   EOF         8 PP, discarded
//
// !! Not yet run on hardware. Everything here is derived from the datasheet, AN000611 and
// the decoded reference capture; the register-level setup is the part most likely to need
// adjustment during M1/M2 bring-up.
#pragma once

#include <stdint.h>

#include "naneye_regs.h"
#include "seim_unpack.h"

namespace seim {

// Invoked while a row transfer is in flight, so the caller can use the ~159 us of idle CPU
// (at 24.75 MHz) to push the previous frame to USB. Must not block.
typedef void (*IdleFn)();

struct FrameInfo {
    uint32_t rows_failed;     // rows with a bad training pattern or bad start/stop bits
    uint32_t pixels_failed;   // individual pixel words that failed validation
    uint32_t pixels_concealed;  // of those, replaced by their neighbours' mean
    uint32_t rows_sync_lost;    // rows whose 8 training words did not match: phase lost
    uint32_t timestamp_us;    // start of readout
    uint32_t duration_us;     // readout duration
};

void begin();

// Sensor power via the on-board LDO enable. power(false) is also the recovery of last
// resort: it forces a full power-on reset of the sensor.
void power(bool on);
bool powered();

// Select one of the supported SCLK rates (spec.md section 5.3); the matching sensor
// mclk_mode/high_speed bits are applied to the config on the next frame.
const naneye::ClockSetting& set_clock(uint32_t want_hz);

// SCLK as the hardware is actually configured, derived from the live CBCMR register and
// the selected divider. This is what frame headers report.
uint32_t sclk_hz();
// The table value the selected setting is meant to produce.
uint32_t nominal_sclk_hz();
// The LPSPI root clock decoded from CBCMR (should be 99 MHz).
uint32_t lpspi_root_hz();
// Delay the input sampling point by one LPSPI functional-clock cycle (CFGR1[SAMPLE]).
// Intended as a timing knob for the higher clock rates; see spec.md R2.
void set_delayed_sample(bool on);
bool delayed_sample();
// Sample received data on the falling SCLK edge instead of the rising one (TCR[CPHA], receive
// transfers only). With set_delayed_sample() this gives four sampling points per bit.
void set_rx_phase(bool falling);
bool rx_phase();
// Automatic choice of the sampling point at every START (on by default). Setting SAMPLE or
// PHASE by hand turns it off; CAL 1 turns it back on.
struct SampleCal {
    bool automatic;
    uint32_t errors[4];     // alternation breaks in 1024 training bits, per sampling point:
                            // rising, falling, rising+delay, falling+delay
    uint8_t choice;         // index of the point chosen
    uint32_t verify_rows;   // rows of the (discarded) first frame checked after the lock
    uint32_t verify_bad_words;
};
const SampleCal& sample_calibration();
void set_auto_sample(bool on);
bool auto_sample();
// Schmitt-trigger input on the receive pin.
void set_input_hysteresis(bool on);
bool input_hysteresis();

void set_config(uint16_t cfg0, uint16_t cfg1);
uint16_t config0();
uint16_t config1();

// Run the power-on sequence up to the point where the sensor is streaming: activation
// clock, register writes with idle on and then off, the pre-sync / sync / delay phases, the
// sampling calibration and the row lock, then the rest of the first (discarded) frame.
//
// The first 328 PP of INITIAL PRE-SYNC are received rather than discarded and checked for
// the training pattern. With require_sensor (the default) start() fails if fewer than half
// of them are training words, so a missing or unpowered sensor is reported instead of
// streaming zeros. require_sensor=false streams regardless, for exercising the USB path
// with no camera attached.
bool start(bool require_sensor = true);

// How many power-on attempts the last start needed, and how many false row-lock
// candidates it stepped over. Both are 0 until the first start.
uint32_t start_attempts();
uint32_t lock_false_candidates();

// Training-pattern words seen in the INITIAL PRE-SYNC row by the most recent start(),
// out of naneye::ROW_PP. 0xAAA and 0x555 both count: the alternating pattern reads as one
// or the other depending on whether the word phase is off by an odd number of bits.
uint32_t presync_training();

// Bring-up: the reference host's power-up sequence, reproduced verbatim from the decoded
// capture (spec.md section 3.2) rather than from the documentation. Activation clock;
// CONFIG_0=0x009F, CONFIG_1=0x009F (idle on); 1,279,318 clocks with SDAT held low; then
// CONFIG_0=0x009F, CONFIG_1=0x0065 (idle off) padded with driven zeros to a full 648-PP
// interface window. Leaves SDAT released and the sensor powered; follow with LISTEN.
// verbatim: the reference's exact CONFIG_1 values. fast_first: the first write pair at
// SCLK rate as the reference host did, not bit-banged. early_release: release SDAT right
// after the idle-off write instead of driving zeros to the end of the interface window.
// first_only: stop after the first (idle-on) write pair and release SDAT, to see what the
// sensor does in idle.
void start_reference(bool verbatim = false, bool fast_first = false,
                     bool early_release = false, bool first_only = false);
void stop();
bool streaming();

// One complete frame cycle. Pixels are written to dst in the requested format; returns
// false only if the sensor link lost sync badly enough to abandon the frame.
bool capture_frame(uint8_t* dst, uint8_t format, FrameInfo& info, IdleFn idle);

// Diagnostics for bring-up (M2): clock the bus and report what the sensor is sending.
struct SyncReport {
    uint32_t words;            // pixel periods examined
    uint32_t training_555;     // words equal to 0x555
    uint32_t training_AAA;     // words equal to 0xAAA
    uint32_t zeros;            // words equal to 0x000
    uint32_t pixel_like;       // words with start=1 and stop=0
    uint16_t first_words[16];  // the first few words, for eyeballing
};
void probe_sync(SyncReport& report, uint32_t rows);

// The word received in the final pixel period of the most recent INTERFACE MODE, which the
// driver leaves undriven for the sensor. The datasheet says the sensor sends 0x015 there;
// AN000611 implies it does not. 0x000 means silent (SDAT is pulled down), 0xFFFF means no
// frame has run yet. Bring-up (M2) should settle which.
uint16_t last_interface_pp();

// Bring-up diagnostic: clock `rows` row-sized bursts (3936 clocks each) with SDAT released
// the whole time, and classify what the sensor sends in each. It never drives SDAT, so it
// is safe whatever phase the sensor is in -- the tool for finding out what the sensor is
// doing when START does not see what it expects. Continues from wherever the clock count
// currently is; needs the sensor powered.
struct ListenReport {
    uint32_t rows;
    int32_t first_active_row;  // first row with any non-zero word, -1 if none
    uint32_t words_555, words_AAA, words_zero, words_pixel, words_other;
    uint16_t first_active_words[12];
};
// map receives one character per row: '.' all zero, 'S' mostly 0x555, 'A' mostly 0xAAA,
// 'P' mostly pixel words, '?' anything else. NUL-terminated if map_len > rows.
void listen(uint32_t rows, ListenReport& rep, char* map, uint32_t map_len);

}  // namespace seim
