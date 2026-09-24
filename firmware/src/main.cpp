// NanEyeC -> Teensy 4.1 -> USB camera firmware. See spec.md.
//
// The capture loop owns the sensor link: it must never block on USB, because a stalled host
// would cost us sensor synchronisation. So frames are double buffered, transmitted
// opportunistically in the idle time between row transfers, and whole frames are dropped
// (and counted) if the host cannot keep up. A frame is never truncated.

#include <Arduino.h>
#include <EEPROM.h>
#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "board.h"
#include "golden_vector.h"
#include "led_dac.h"
#include "naneye_regs.h"
#include "naneye_seim.h"
#include "seim_unpack.h"
#include "usb_proto.h"
#include "watchdog.h"

using namespace naneye;

static constexpr const char* FW_VERSION = "0.1.0";

// Two frame buffers of 128,000 bytes: enough for packed 10-bit (320*320*10/8). The raw12
// diagnostic format needs 209,920 bytes, so it borrows the whole region and runs single
// buffered, which is fine because it is only used for bring-up.
static constexpr size_t FB_SIZE = 128000;
static DMAMEM uint8_t s_fb[2 * FB_SIZE];

static uint8_t s_format = proto::FMT_GRAY10;  // full sensor resolution by default

// Which colour filter array the sensor fitted to the board has. Nothing on the link tells
// us -- a mono and a colour NanEyeC stream identical-looking pixels -- so it is told once
// and remembered across resets, and then travels in every frame's header for the host.
static constexpr int EEPROM_CFA_ADDR = 0;
static constexpr uint8_t EEPROM_CFA_MAGIC = 0xC0;  // high nibble marks a value we wrote
static uint8_t s_cfa = proto::CFA_MONO;

static const char* cfa_name(uint8_t cfa) {
    switch (cfa) {
        case proto::CFA_BGGR: return "BGGR";
        case proto::CFA_GBRG: return "GBRG";
        case proto::CFA_GRBG: return "GRBG";
        case proto::CFA_RGGB: return "RGGB";
        default: return "MONO";
    }
}

static bool cfa_from_name(const char* name, uint8_t& out) {
    for (uint8_t c = proto::CFA_MONO; c <= proto::CFA_RGGB; c++) {
        if (!strcmp(name, cfa_name(c))) {
            out = c;
            return true;
        }
    }
    return false;
}

static void cfa_load() {
    const uint8_t stored = EEPROM.read(EEPROM_CFA_ADDR);
    if ((stored & 0xF0) == EEPROM_CFA_MAGIC && (stored & 0x0F) <= proto::CFA_RGGB) {
        s_cfa = stored & 0x0F;
    }
}

static void cfa_store() {
    EEPROM.update(EEPROM_CFA_ADDR, (uint8_t)(EEPROM_CFA_MAGIC | s_cfa));
}
static bool s_run = false;
static uint32_t s_frame_counter = 0;
static uint32_t s_frames_dropped = 0;
static uint32_t s_frames_sent = 0;

// --- Opportunistic transmitter ----------------------------------------------------------
// Two segments: the header, then the payload. Pumped from the row gaps during capture.
struct Tx {
    proto::Header header;
    const uint8_t* payload = nullptr;
    size_t sent = 0;
    size_t total = 0;
    bool active = false;
} static s_tx;

static void tx_begin(const proto::Header& h, const uint8_t* payload) {
    s_tx.header = h;
    s_tx.payload = payload;
    s_tx.sent = 0;
    s_tx.total = sizeof(proto::Header) + h.payload_len;
    s_tx.active = true;
}

// Never blocks: writes only what the USB endpoint has room for right now.
static void tx_pump() {
    if (!s_tx.active) return;
    if (!Serial) {
        s_tx.active = false;
        return;
    }
    int room = Serial.availableForWrite();
    while (room > 0 && s_tx.sent < s_tx.total) {
        const uint8_t* src;
        size_t avail;
        if (s_tx.sent < sizeof(proto::Header)) {
            src = (const uint8_t*)&s_tx.header + s_tx.sent;
            avail = sizeof(proto::Header) - s_tx.sent;
        } else {
            const size_t off = s_tx.sent - sizeof(proto::Header);
            src = s_tx.payload + off;
            avail = s_tx.total - s_tx.sent;
        }
        const size_t n = ((size_t)room < avail) ? (size_t)room : avail;
        const size_t w = Serial.write(src, n);
        s_tx.sent += w;
        room -= (int)w;
        if (w < n) break;
    }
    if (s_tx.sent >= s_tx.total) s_tx.active = false;
}

// A reply must never be written into the middle of an image packet that is still going
// out: the host would read the reply as payload, the image would fail its CRC, and the
// reply would be discarded with it. Commands are handled between frames, so finish the
// image first. Normally that takes a few ms, since the host is reading in order to get the
// reply. If the host has stopped reading, give up after timeout_ms; the host resyncs past
// the truncated packet on its magic word.
static void finish_tx(uint32_t timeout_ms) {
    const uint32_t t0 = millis();
    while (s_tx.active && (millis() - t0) < timeout_ms) {
        tx_pump();
        yield();
    }
    s_tx.active = false;
}

static bool s_force_start = false;

static size_t frame_payload_bytes(uint8_t format) {
    return (size_t)row_payload_bytes(format) * HEIGHT;
}

// --- Commands ---------------------------------------------------------------------------
static void reply(const char* fmt, ...) {
    char buf[200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    proto::send_text(proto::TYPE_RESPONSE, "%s", buf);
}

static uint32_t parse_u32(const char* s, uint32_t dflt) {
    if (!s || !*s) return dflt;
    return (uint32_t)strtoul(s, nullptr, 0);
}

// Verify the unpack path against a row captured from the working reference link, so the
// decode can be trusted before any image is believed. See tools/make_golden_vector.py.
static void selftest() {
    uint8_t gray8[WIDTH];
    const uint32_t bad = unpack_row_gray8(golden::ROW_WORDS_DATA, gray8);
    uint32_t mismatches = 0;
    for (uint32_t i = 0; i < WIDTH; i++) {
        if (gray8[i] != (uint8_t)(golden::EXPECTED_PIXELS[i] >> 2)) mismatches++;
    }
    const uint32_t training = count_training(golden::ROW_WORDS_DATA, WORD_TRAINING);
    reply("SELFTEST unpack: bad_words=%lu mismatches=%lu training=%lu/8 -> %s",
          (unsigned long)bad, (unsigned long)mismatches, (unsigned long)training,
          (bad == 0 && mismatches == 0 && training == 8) ? "PASS" : "FAIL");

    // Concealment: break one word of the golden row and check it is replaced by the mean of
    // its neighbours, and that nothing else changes.
    {
        uint32_t row[ROW_WORDS_PADDED];
        memcpy(row, golden::ROW_WORDS_DATA, sizeof(row));
        const uint32_t victim = 100;
        set_pp(row, TRAINING_PP + victim, 0x7FE);  // start bit 0: framing broken
        uint16_t px[WIDTH];
        uint32_t concealed = 0;
        const uint32_t bad2 = extract_row(row, px, &concealed);
        const uint16_t want = (uint16_t)((golden::EXPECTED_PIXELS[victim - 1] +
                                          golden::EXPECTED_PIXELS[victim + 1] + 1u) / 2u);
        uint32_t others = 0;
        for (uint32_t i = 0; i < WIDTH; i++)
            if (i != victim && px[i] != golden::EXPECTED_PIXELS[i]) others++;
        reply("SELFTEST conceal: bad=%lu concealed=%lu pixel=%u (expect %u) others changed=%lu"
              " -> %s",
              (unsigned long)bad2, (unsigned long)concealed, px[victim], want,
              (unsigned long)others,
              (bad2 == 1 && concealed == 1 && px[victim] == want && others == 0) ? "PASS"
                                                                                  : "FAIL");
    }

    // Exposure maths against the values decoded from the reference capture.
    const uint32_t e1 = exposure_pp(0, 0);
    const uint32_t e2 = exposure_pp(127, 0);
    reply("SELFTEST exposure: rir=0 -> %lu PP (expect 105616), rir=127 -> %lu PP (expect 22304) -> %s",
          (unsigned long)e1, (unsigned long)e2,
          (e1 == 105616u && e2 == 22304u) ? "PASS" : "FAIL");
}

// What START measured about the sampling point, and how the first frame checked out.
static void report_sampling() {
    static const char* const NAMES[4] = {"rise", "fall", "rise+d", "fall+d"};
    const seim::SampleCal& c = seim::sample_calibration();
    if (c.automatic) {
        reply("START sampling: training breaks per 1023 bits  rise %lu  fall %lu  rise+d %lu"
              "  fall+d %lu  -> %s",
              (unsigned long)c.errors[0], (unsigned long)c.errors[1],
              (unsigned long)c.errors[2], (unsigned long)c.errors[3], NAMES[c.choice]);
    } else {
        reply("START sampling: manual (SAMPLE %d PHASE %d)", seim::delayed_sample() ? 1 : 0,
              seim::rx_phase() ? 1 : 0);
    }
    if (c.verify_rows)
        reply("START check: %lu rows of the discarded first frame, %lu bad words",
              (unsigned long)c.verify_rows, (unsigned long)c.verify_bad_words);
}

// LISTEN's work, shared with START REF so the two can run back to back with no gap in the
// clock: the sensor turned out not to tolerate a long clock pause during INITIAL PRE-SYNC.
static void listen_and_report(uint32_t rows) {
    if (rows > 2000) rows = 2000;
    static char map[2001];
    seim::ListenReport rep;
    seim::listen(rows, rep, map, sizeof(map));

    reply("LISTEN %lu rows: 0x555=%lu 0xAAA=%lu 0x000=%lu pixel=%lu other=%lu  "
          "first active row %ld",
          (unsigned long)rep.rows, (unsigned long)rep.words_555,
          (unsigned long)rep.words_AAA, (unsigned long)rep.words_zero,
          (unsigned long)rep.words_pixel, (unsigned long)rep.words_other,
          (long)rep.first_active_row);
    if (rep.first_active_row >= 0) {
        const uint16_t* w = rep.first_active_words;
        reply("LISTEN first active row starts: %03X %03X %03X %03X %03X %03X %03X %03X "
              "%03X %03X %03X %03X",
              w[0], w[1], w[2], w[3], w[4], w[5], w[6], w[7], w[8], w[9], w[10], w[11]);
    }

    // Row map, run-length encoded so it fits in replies: e.g. ".x2 Sx4 Px320".
    char line[180];
    size_t len = 0;
    for (uint32_t i = 0; i < rep.rows;) {
        uint32_t j = i;
        while (j < rep.rows && map[j] == map[i]) j++;
        char seg[24];
        const int n = (j - i > 1)
                          ? snprintf(seg, sizeof(seg), "%cx%lu ", map[i], (unsigned long)(j - i))
                          : snprintf(seg, sizeof(seg), "%c ", map[i]);
        if (len + (size_t)n >= sizeof(line) - 1) {
            line[len] = 0;
            reply("LISTEN map: %s", line);
            len = 0;
        }
        memcpy(line + len, seg, (size_t)n);
        len += (size_t)n;
        i = j;
    }
    line[len] = 0;
    if (len) reply("LISTEN map: %s", line);
}

static void handle_command(char* line) {
    // Split into a verb and up to seven arguments.
    char* tok[8] = {};
    int n = 0;
    for (char* p = strtok(line, " \t"); p && n < 8; p = strtok(nullptr, " \t")) tok[n++] = p;
    if (n == 0) return;
    for (char* p = tok[0]; *p; p++) *p = (char)toupper(*p);

    if (!strcmp(tok[0], "ID")) {
        reply("naneye-teensy %s  sclk=%lu Hz (nominal %lu, LPSPI root %lu Hz)  "
              "cfg0=0x%04X cfg1=0x%04X  fmt=%u  cfa=%s  last reset: %s (SRSR 0x%03lX)",
              FW_VERSION, (unsigned long)seim::sclk_hz(),
              (unsigned long)seim::nominal_sclk_hz(), (unsigned long)seim::lpspi_root_hz(),
              seim::config0(), seim::config1(), s_format, cfa_name(s_cfa),
              watchdog::last_reset_was_watchdog() ? "WATCHDOG" : "normal",
              (unsigned long)watchdog::reset_status());
    } else if (!strcmp(tok[0], "CLKMEAS")) {
        if (s_run || seim::powered()) {
            reply("CLKMEAS needs the sensor powered off (the clocks would advance it): "
                  "STOP and POWER 0 first");
        } else {
            const uint32_t measured = seim::measure_sclk_hz();
            const uint32_t expect = seim::nominal_sclk_hz();
            const int32_t err_ppm =
                expect ? (int32_t)(((int64_t)measured - expect) * 1000000 / expect) : 0;
            reply("CLKMEAS measured %lu Hz  nominal %lu Hz  derived %lu Hz  error %+ld ppm "
                  "(frame gaps make it read slightly low) -> %s",
                  (unsigned long)measured, (unsigned long)expect,
                  (unsigned long)seim::sclk_hz(), (long)err_ppm,
                  (measured && err_ppm > -20000 && err_ppm < 5000) ? "PASS" : "FAIL");
        }
    } else if (!strcmp(tok[0], "POWER")) {
        // A bare POWER only reports. It used to default to 1, so asking whether the sensor
        // was off turned it on -- which is a poor answer to a question.
        if (n > 1) seim::power(parse_u32(tok[1], 1) != 0);
        reply("POWER %d%s", seim::powered() ? 1 : 0, n > 1 ? "" : "  (POWER 0|1 to change)");
    } else if (!strcmp(tok[0], "CFA")) {
        uint8_t want = s_cfa;
        if (n > 1) {
            for (char* p = tok[1]; *p; p++) *p = (char)toupper(*p);
            if (!cfa_from_name(tok[1], want)) {
                reply("CFA '%s' unknown. Try MONO BGGR GBRG GRBG RGGB", tok[1]);
                return;
            }
            s_cfa = want;
            cfa_store();
        }
        reply("CFA %s%s  (the 2x2 the first pixel of the first row starts; remembered "
              "across resets)", cfa_name(s_cfa), n > 1 ? " stored" : "");
    } else if (!strcmp(tok[0], "CLK")) {
        const ClockSetting& c = seim::set_clock(parse_u32(tok[1], 12375000u));
        reply("CLK %lu Hz  sckdiv=%u mclk_mode=%u high_speed=%u%s",
              (unsigned long)c.sclk_hz, c.sckdiv, c.mclk_mode, c.high_speed,
              s_run ? "  (restart with START: the sensor only sees the new mclk_mode at the"
                      " next interface window, so one frame will be mismatched)"
                    : "");
    } else if (!strcmp(tok[0], "SAMPLE")) {
        if (n > 1) {
            seim::set_delayed_sample(parse_u32(tok[1], 0) != 0);
            seim::set_auto_sample(false);
        }
        reply("SAMPLE %d%s", seim::delayed_sample() ? 1 : 0,
              seim::auto_sample() ? "" : "  (automatic calibration off: CAL 1 to restore)");
    } else if (!strcmp(tok[0], "INJECT")) {
        if (n > 1) seim::set_inject(parse_u32(tok[1], 0));
        reply("INJECT %lu corrupt pixel words per frame (test hook; 0 = off)",
              (unsigned long)seim::inject());
    } else if (!strcmp(tok[0], "CONCEAL")) {
        if (n > 1) seim::set_conceal(parse_u32(tok[1], 1) != 0);
        reply("CONCEAL %d  (%s)", seim::conceal() ? 1 : 0,
              seim::conceal() ? "corrupt pixels replaced by their neighbours' mean"
                              : "corrupt pixels left as received");
    } else if (!strcmp(tok[0], "CAL")) {
        if (n > 1) seim::set_auto_sample(parse_u32(tok[1], 1) != 0);
        reply("CAL %d  (%s)", seim::auto_sample() ? 1 : 0,
              seim::auto_sample() ? "START measures and picks the sampling point"
                                  : "START uses SAMPLE and PHASE as set");
    } else if (!strcmp(tok[0], "PHASE")) {
        if (n > 1) {
            seim::set_rx_phase(parse_u32(tok[1], 0) != 0);
            seim::set_auto_sample(false);
        }
        reply("PHASE %d (receive on the %s SCLK edge)", seim::rx_phase() ? 1 : 0,
              seim::rx_phase() ? "falling" : "rising");
    } else if (!strcmp(tok[0], "HYS")) {
        if (n > 1) seim::set_input_hysteresis(parse_u32(tok[1], 0) != 0);
        reply("HYS %d", seim::input_hysteresis() ? 1 : 0);
    } else if (!strcmp(tok[0], "START")) {
        if (n > 1 && !strcasecmp(tok[1], "REF")) {
            // START REF [rows]: with rows, listen straight away, with no gap in the clock.
            s_run = false;
            bool verbatim = false, fast = false, early = false, first = false;
            uint32_t rows = 0;
            for (int k = 2; k < n; k++) {
                if (!strcasecmp(tok[k], "VERBATIM")) verbatim = true;
                else if (!strcasecmp(tok[k], "FAST")) fast = true;
                else if (!strcasecmp(tok[k], "EARLY")) early = true;
                else if (!strcasecmp(tok[k], "FIRST")) first = true;
                else rows = parse_u32(tok[k], 0);
            }
            seim::start_reference(verbatim, fast, early, first);
            if (rows) listen_and_report(rows);
            reply("START REF done: cfg0=0x%04X cfg1=0x%04X, SDAT released%s",
                  seim::config0(), seim::config1(),
                  rows ? " (listened with no clock gap)" : ". Now LISTEN.");
            return;
        }
        bool an = false;
        s_force_start = false;
        for (int k = 1; k < n; k++) {
            if (!strcasecmp(tok[k], "FORCE")) s_force_start = true;
            if (!strcasecmp(tok[k], "AN")) an = true;  // AN000611 single-write sequence
        }
        if (seim::start(!s_force_start, an)) {
            s_run = true;
            reply("START ok  streaming  (pre-sync training %lu/%u, attempt %lu, "
                  "%lu false lock candidates)%s",
                  (unsigned long)seim::presync_training(), (unsigned)ROW_PP,
                  (unsigned long)seim::start_attempts(),
                  (unsigned long)seim::lock_false_candidates(),
                  s_force_start ? "  FORCED: frames are not from a verified sensor" : "");
            report_sampling();
        } else {
            s_run = false;
            const uint32_t training = seim::presync_training();
            if (training < ROW_PP / 2) {
                reply("START failed: pre-sync training pattern %lu/%u words. No sensor "
                      "answering: check power, wiring and that SDAT reaches the sensor. "
                      "START FORCE streams anyway, to test the USB path.",
                      (unsigned long)training, (unsigned)ROW_PP);
            } else {
                // The sensor answered, but no clean row start was found in the first frame:
                // the training pattern survives the link while pixel data does not.
                reply("START failed: sensor answers (pre-sync training %lu/%u) but could not "
                      "lock onto its rows: pixel data is not arriving intact. Signal "
                      "integrity at this clock? Try a lower CLK; the sampling report below "
                      "shows how each sampling point fared.",
                      (unsigned long)training, (unsigned)ROW_PP);
            }
            report_sampling();
        }
    } else if (!strcmp(tok[0], "STOP")) {
        s_run = false;
        seim::stop();
        reply("STOP");
    } else if (!strcmp(tok[0], "DEPTH")) {
        const uint32_t d = parse_u32(tok[1], 8);
        if (d != 8 && d != 10 && d != 12) {
            reply("DEPTH must be 8, 10 or 12; unchanged (currently %s)",
                  s_format == proto::FMT_GRAY10 ? "10" : s_format == proto::FMT_RAW12 ? "12"
                                                                                      : "8");
        } else {
            s_format = (d == 10) ? proto::FMT_GRAY10
                                 : (d == 12) ? proto::FMT_RAW12 : proto::FMT_GRAY8;
            reply("DEPTH %u  payload=%u bytes/frame", d,
                  (unsigned)frame_payload_bytes(s_format));
        }
    } else if (!strcmp(tok[0], "EXP")) {
        Config0 c0 = Config0::unpack(seim::config0());
        Config1 c1 = Config1::unpack(seim::config1());
        // The field is 8 bits but the datasheet caps rows in reset at the number of sensor
        // rows, so rows_in_reset must not exceed 159; beyond that the exposure formula goes
        // negative. Clamp rather than write an out-of-spec value, and say so.
        uint32_t want_rir = parse_u32(tok[1], c0.rows_in_reset);
        uint32_t want_rd = (n > 2) ? parse_u32(tok[2], c1.rows_delay) : c1.rows_delay;
        const bool clamped = want_rir > ROWS_IN_RESET_MAX || want_rd > ROWS_DELAY_MAX;
        if (want_rir > ROWS_IN_RESET_MAX) want_rir = ROWS_IN_RESET_MAX;
        if (want_rd > ROWS_DELAY_MAX) want_rd = ROWS_DELAY_MAX;
        c0.rows_in_reset = (uint8_t)want_rir;
        c1.rows_delay = (uint8_t)want_rd;
        seim::set_config(c0.pack(), c1.pack());
        if (clamped)
            reply("EXP clamped to the datasheet limits: rows_in_reset<=%u rows_delay<=%u",
                  ROWS_IN_RESET_MAX, ROWS_DELAY_MAX);
        reply("EXP rows_in_reset=%u rows_delay=%u -> t_exp=%lu PP (%lu us at %lu Hz)",
              c0.rows_in_reset, c1.rows_delay,
              (unsigned long)exposure_pp(c0.rows_in_reset, c1.rows_delay),
              (unsigned long)((uint64_t)exposure_pp(c0.rows_in_reset, c1.rows_delay) *
                              PP_BITS * 1000000ull / seim::sclk_hz()),
              (unsigned long)seim::sclk_hz());
    } else if (!strcmp(tok[0], "GAIN")) {
        Config0 c0 = Config0::unpack(seim::config0());
        Config1 c1 = Config1::unpack(seim::config1());
        c0.ramp_gain = (uint8_t)parse_u32(tok[1], c0.ramp_gain) & 3;
        if (n > 2) c1.cds_gain = (uint8_t)parse_u32(tok[2], c1.cds_gain) & 1;
        seim::set_config(c0.pack(), c1.pack());
        reply("GAIN ramp_gain=%u cds_gain=%u", c0.ramp_gain, c1.cds_gain);
    } else if (!strcmp(tok[0], "REG")) {
        const uint32_t addr = parse_u32(tok[1], 0);
        const uint32_t val = parse_u32(tok[2], 0);
        if (addr > 1) {
            reply("REG address must be 0 (CONFIG_0) or 1 (CONFIG_1); only those exist");
        } else if (val > 0xFFFF) {
            reply("REG value must fit 16 bits");
        } else {
            if (addr == 0) seim::set_config((uint16_t)val, seim::config1());
            else seim::set_config(seim::config0(), (uint16_t)val);
            reply("REG %lu = 0x%04X (takes effect next frame)", (unsigned long)addr,
                  (unsigned)val);
        }
    } else if (!strcmp(tok[0], "LED")) {
        led::set_enabled(parse_u32(tok[1], 0) != 0);
        reply("LED %d  current=%.2f mA (limit %.2f mA)", led::enabled() ? 1 : 0,
              led::current_ma(), led::max_current_ma());
    } else if (!strcmp(tok[0], "LEDI")) {
        const float ma = tok[1] ? (float)atof(tok[1]) : 0.0f;
        const float got = led::set_current_ma(ma);
        reply("LEDI %.2f mA requested -> %.2f mA applied (code %u, limit %.2f mA)", ma, got,
              led::code_for_current_ma(got), led::max_current_ma());
    } else if (!strcmp(tok[0], "LEDMAX")) {
        const float lim = led::set_max_current_ma(tok[1] ? (float)atof(tok[1]) : 0.0f);
        reply("LEDMAX %.2f mA (hardware maximum %.2f mA)", lim, led::MAX_CURRENT_MA);
    } else if (!strcmp(tok[0], "PROBE")) {
        seim::SyncReport rep;
        seim::probe_sync(rep, parse_u32(tok[1], 2));
        reply("PROBE words=%lu 0x555=%lu 0xAAA=%lu 0x000=%lu pixel_like=%lu",
              (unsigned long)rep.words, (unsigned long)rep.training_555,
              (unsigned long)rep.training_AAA, (unsigned long)rep.zeros,
              (unsigned long)rep.pixel_like);
        reply("PROBE end-of-interface PP: 0x%03X  (datasheet says the sensor sends 0x015; "
              "0x000 means it stays silent)",
              seim::last_interface_pp());
        reply("PROBE first: %03X %03X %03X %03X %03X %03X %03X %03X %03X %03X",
              rep.first_words[0], rep.first_words[1], rep.first_words[2], rep.first_words[3],
              rep.first_words[4], rep.first_words[5], rep.first_words[6], rep.first_words[7],
              rep.first_words[8], rep.first_words[9]);
    } else if (!strcmp(tok[0], "LISTEN")) {
        if (s_run) {
            reply("LISTEN needs streaming stopped: STOP first");
        } else if (!seim::powered()) {
            reply("LISTEN needs the sensor powered: POWER 1 or START first");
        } else {
            listen_and_report(parse_u32(tok[1], 400));
        }
    } else if (!strcmp(tok[0], "WDTEST")) {
        // Hang on purpose: the watchdog must reset the board within 2 s, after which the
        // port re-enumerates and ID reports "last reset: WATCHDOG".
        reply("WDTEST hanging now; expect a watchdog reset in %lu ms",
              (unsigned long)watchdog::WATCHDOG_TIMEOUT_MS);
        Serial.flush();
        seim::power(false);
        for (;;) {
        }
    } else if (!strcmp(tok[0], "ALIGN")) {
        if (n > 1) seim::set_align_clocks(parse_u32(tok[1], 10));
        reply("ALIGN %lu clocks between the idle-off write and pre-sync",
              (unsigned long)seim::align_clocks());
    } else if (!strcmp(tok[0], "STATS")) {
        reply("STATS frames=%lu sent=%lu dropped=%lu streaming=%d powered=%d "
              "end_of_interface=0x%03X",
              (unsigned long)s_frame_counter, (unsigned long)s_frames_sent,
              (unsigned long)s_frames_dropped, seim::streaming() ? 1 : 0,
              seim::powered() ? 1 : 0, seim::last_interface_pp());
    } else if (!strcmp(tok[0], "SELFTEST")) {
        selftest();
    } else {
        reply("unknown command '%s'. Try ID POWER CFA CLK CLKMEAS SAMPLE START STOP DEPTH EXP "
              "GAIN REG LED LEDI LEDMAX PROBE STATS SELFTEST",
              tok[0]);
    }
}

// Commands arrive as plain ASCII lines, so the port is usable from a terminal. Device
// output is always framed (spec.md section 7).
static void poll_commands() {
    static char line[128];
    static size_t len = 0;
    while (Serial.available()) {
        const int c = Serial.read();
        if (c < 0) break;
        if (c == '\r') continue;
        if (c == '\n') {
            line[len] = 0;
            if (len) {
                finish_tx(200);  // never interleave a reply into an image in flight
                handle_command(line);
            }
            len = 0;
        } else if (len < sizeof(line) - 1) {
            line[len++] = (char)c;
        } else {
            len = 0;  // overlong line, discard
        }
    }
}

void setup() {
    watchdog::begin();
    Serial.begin(115200);  // rate is ignored for USB CDC
    proto::crc32_init();
    cfa_load();
    led::begin();
    seim::begin();
}

void loop() {
    watchdog::feed();
    poll_commands();
    tx_pump();

    if (!s_run) {
        delay(1);
        return;
    }

    // raw12 needs 209,920 bytes and so borrows the whole region, leaving it single
    // buffered. Wait for the previous frame to leave before capturing over it.
    if (s_format == proto::FMT_RAW12 && s_tx.active) {
        tx_pump();
        return;
    }
    uint8_t* buf = (s_format == proto::FMT_RAW12) ? s_fb
                                                  : (s_fb + (s_frame_counter & 1) * FB_SIZE);
    seim::FrameInfo info;
    const bool ok = seim::capture_frame(buf, s_format, info, tx_pump);
    s_frame_counter++;

    if (!ok) {
        s_frames_dropped++;
        proto::send_text(proto::TYPE_LOG, "capture failed, re-syncing (rows_failed=%lu)",
                         (unsigned long)info.rows_failed);
        s_run = seim::start(!s_force_start);  // power-cycles the sensor first
        return;
    }

    // A frame still in flight means the host could not keep up: drop this one rather than
    // truncate, and account for it.
    if (s_tx.active) {
        s_frames_dropped++;
        return;
    }

    const size_t payload = frame_payload_bytes(s_format);
    proto::Header h;
    proto::header_init(h, proto::TYPE_IMAGE, (uint32_t)payload);
    h.frame_counter = s_frame_counter;
    h.timestamp_us = info.timestamp_us;
    h.width = (uint16_t)WIDTH;
    h.height = (uint16_t)HEIGHT;
    h.format = s_format;
    h.flags = proto::flags_with_cfa(0, s_cfa);
    // SYNC_LOST means the row phase itself is in doubt (training words wrong); isolated
    // corrupt pixels are CONCEALED instead, and both still count in rows_failed.
    if (info.rows_sync_lost) h.flags |= proto::FLAG_SYNC_LOST;
    if (info.pixels_concealed) h.flags |= proto::FLAG_CONCEALED;
    h.pixels_concealed = info.pixels_concealed;
    h.rows_failed = (uint16_t)(info.rows_failed > 0xFFFF ? 0xFFFF : info.rows_failed);
    h.sclk_hz = seim::sclk_hz();
    h.cfg0 = seim::config0();
    h.cfg1 = seim::config1();
    h.exposure_pp = exposure_pp(Config0::unpack(h.cfg0).rows_in_reset,
                                Config1::unpack(h.cfg1).rows_delay);
    h.frames_dropped = s_frames_dropped;
    proto::header_finish(h, buf, payload);
    tx_begin(h, buf);
    s_frames_sent++;
    tx_pump();
}
