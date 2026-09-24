#include "naneye_seim.h"

#include <Arduino.h>
#include <DMAChannel.h>
#include <SPI.h>
#include <string.h>

#include "board.h"
#include "watchdog.h"

namespace seim {

using namespace naneye;

// --- LPSPI register bits (i.MX RT1060 RM, chapter 47) ----------------------------------
// Defined locally rather than relying on header macro names, which vary between cores.
namespace reg {
constexpr uint32_t CR_MEN = 1u << 0;
constexpr uint32_t CR_RST = 1u << 1;
constexpr uint32_t CR_RTF = 1u << 8;  // reset transmit FIFO
constexpr uint32_t CR_RRF = 1u << 9;  // reset receive FIFO

constexpr uint32_t RSR_RXEMPTY = 1u << 1;  // receive FIFO empty

constexpr uint32_t SR_TDF = 1u << 0;
constexpr uint32_t SR_FCF = 1u << 9;   // frame complete
constexpr uint32_t SR_REF = 1u << 12;  // receive error (FIFO overflow)

constexpr uint32_t CFGR1_MASTER = 1u << 0;
constexpr uint32_t CFGR1_SAMPLE = 1u << 1;
constexpr uint32_t CFGR1_OUTCFG = 1u << 26;  // tristate SDO when the frame's PCS negates

constexpr uint32_t DER_RDDE = 1u << 1;

constexpr uint32_t TCR_TXMSK = 1u << 18;
constexpr uint32_t TCR_RXMSK = 1u << 19;

constexpr uint32_t SR_STICKY = 0x3F00u;

inline uint32_t framesz(uint32_t bits) { return (bits - 1u) & 0xFFFu; }
}  // namespace reg

// Largest frame the peripheral supports, rounded down to a whole number of pixel periods.
constexpr uint32_t MAX_FRAME_BITS = 4092;                    // 341 PP
constexpr uint32_t ROW_BITS = ROW_PP * PP_BITS;              // 3936
constexpr uint32_t INTERFACE_BITS = INTERFACE_PP * PP_BITS;  // 7776
constexpr uint32_t REG_WRITE_BITS = 24;                      // one register write = 2 PP
constexpr uint32_t INTERFACE_FRAMES = INTERFACE_BITS / REG_WRITE_BITS;  // 324, exact
constexpr uint32_t EOF_BITS = EOF_PP * PP_BITS;              // 96

// Spin limit for the hardware waits: generous enough never to trip in normal operation,
// small enough that a mis-configured peripheral reports a fault instead of hanging.
constexpr uint32_t SPIN_LIMIT = 40000000u;

#define LPSPI IMXRT_LPSPI3_S

// Two row buffers, each with one extra word of padding so pp_at() may read w[wi + 1].
//
// DMAMEM is OCRAM, which the Cortex-M7 D-cache covers (write-back). The DMA writes RAM
// behind the cache's back, so every completed row must be invalidated before the CPU reads
// it, or it reads whatever the cache still holds. Until that was done the sensor was
// streaming perfect frames on pin 1 -- seen on the logic analyser -- while LISTEN read all
// zeros or stale 0xAAA. Each row gets whole cache lines to itself so invalidating one can
// never touch the other.
constexpr uint32_t CACHE_LINE = 32;
constexpr uint32_t ROW_BUF_WORDS =
    (ROW_WORDS_PADDED * 4 + CACHE_LINE - 1) / CACHE_LINE * CACHE_LINE / 4;
static DMAMEM uint32_t s_row[2][ROW_BUF_WORDS] __attribute__((aligned(32)));
static uint32_t* s_row_pending = nullptr;
static DMAChannel s_rx;

static bool s_powered = false;
static bool s_streaming = false;
static bool s_delayed_sample = false;
static uint32_t s_align_clocks = 10;
static bool s_first_frame_after_por = true;
static uint16_t s_cfg0 = REF_CONFIG0;
static uint16_t s_cfg1 = REF_CONFIG1_IDLE;
static const ClockSetting* s_clock = &CLOCKS[0];
static uint32_t s_mux_sdat = 0;  // IOMUX value connecting pin 26 to LPSPI3_SDO
static uint32_t s_mux_sclk = 0;  // IOMUX value connecting pin 27 to LPSPI3_SCK
static uint32_t s_pad_sdat = 0;  // pad control (drive, slew) as SPI1.begin() left it
static uint32_t s_pad_sclk = 0;
// What arrived on SDAT in the final pixel period of the last INTERFACE MODE, which we leave
// to the sensor (see interface_window). 0xFFFF until the first frame has run.
static uint16_t s_last_interface_pp = 0xFFFF;
static uint32_t s_presync_training = 0;

// The NanoBerry's R13 and R33 (10k pull-downs on SDAT and SCLK at the header) are not
// fitted, and SPI1.begin() configures these pads with no pull at all, so both nets would
// float whenever nothing drives them. A floating SCLK is the dangerous one: the sensor
// advances its state machine on clock edges, so noise while it is powered -- for instance
// while LPSPI is being reconfigured -- could slip the 12-bit word alignment for the rest of
// the session. The pad's internal 100k pull-down stands in for the missing resistors.
constexpr uint32_t PAD_PULL_MASK = IOMUXC_PAD_PKE | IOMUXC_PAD_PUE | IOMUXC_PAD_PUS(3);
constexpr uint32_t PAD_PULLDOWN = IOMUXC_PAD_PKE | IOMUXC_PAD_PUE | IOMUXC_PAD_PUS(0);

static inline void add_pulldown(uint8_t pin) {
    volatile uint32_t* pad = portControlRegister(pin);
    *pad = (*pad & ~PAD_PULL_MASK) | PAD_PULLDOWN;
}

// --- SDAT direction (spec.md section 4.3) ----------------------------------------------
// Pin 26 drives during INTERFACE MODE and is a hi-Z input otherwise; pin 1 always reads.
// TCR[TXMSK] tristates the output too, so this is belt and braces. Releasing SDAT leaves it
// pulled down rather than floating. Driving restores the pad as well as the mux, because
// pinMode() rewrites the pad and would otherwise leave SDO at the wrong speed setting.
static inline void sdat_drive() {
    *portControlRegister(board::PIN_SDAT_OUT) = s_pad_sdat;
    *portConfigRegister(board::PIN_SDAT_OUT) = s_mux_sdat;
}
static inline void sdat_hiz() { pinMode(board::PIN_SDAT_OUT, INPUT_PULLDOWN); }

// --- Low-level frame helpers -----------------------------------------------------------
// CPOL=0, CPHA=0: the sensor launches data ~8 ns after the rising edge and we sample on the
// following rising edge, which measured 24 ns of setup at 31.25 MHz (spec.md section 3.1).
static inline uint32_t tcr_base() { return 0; }

// Receive-side sampling edge. CPHA=1 makes LPSPI sample on the falling edge, half a period
// earlier than the rising edge, so together with CFGR1[SAMPLE] the receiver has four
// sampling points to choose from. Only transfers that receive use it: register writes must
// keep CPHA=0, because the sensor captures SDAT on the rising edge.
static bool s_rx_cpha = false;
constexpr uint32_t TCR_CPHA = 1u << 30;
static inline uint32_t tcr_rx() { return tcr_base() | (s_rx_cpha ? TCR_CPHA : 0u); }

static inline bool wait_frame() {
    uint32_t guard = 0;
    while (!(LPSPI.SR & reg::SR_FCF)) {
        if (++guard > SPIN_LIMIT) return false;
    }
    LPSPI.SR = reg::SR_FCF;
    return true;
}

// Clock `pp` pixel periods with the output masked, discarding everything received.
static bool clock_pp_discard(uint32_t pp) {
    uint32_t bits = pp * PP_BITS;
    while (bits) {
        uint32_t chunk = bits > MAX_FRAME_BITS ? MAX_FRAME_BITS : bits;
        LPSPI.TCR = tcr_base() | reg::framesz(chunk) | reg::TCR_TXMSK | reg::TCR_RXMSK;
        if (!wait_frame()) return false;
        bits -= chunk;
    }
    return true;
}

// Drive `bits` clocks with SDAT held at zero, in as few LPSPI frames as possible.
// The datasheet asks for the bus to stay driven for the whole interface window; doing that
// as 322 separate 24-bit frames would add 322 inter-frame gaps, and wall-clock time spent
// in the interface window is time the pixels keep integrating.
static bool drive_zeros(uint32_t bits) {
    while (bits) {
        uint32_t chunk = bits > MAX_FRAME_BITS ? MAX_FRAME_BITS : bits;
        LPSPI.TCR = tcr_base() | reg::framesz(chunk) | reg::TCR_RXMSK;
        for (uint32_t sent = 0; sent < chunk; sent += 32) {
            uint32_t guard = 0;
            while (!(LPSPI.SR & reg::SR_TDF)) {
                if (++guard > SPIN_LIMIT) return false;
            }
            LPSPI.TDR = 0;
        }
        if (!wait_frame()) return false;
        bits -= chunk;
    }
    return true;
}

// Emit one 24-bit word (two pixel periods) with SDAT driven: a register write, or filler.
static inline bool send24(uint32_t word) {
    LPSPI.TCR = tcr_base() | reg::framesz(24) | reg::TCR_RXMSK;
    uint32_t guard = 0;
    while (!(LPSPI.SR & reg::SR_TDF)) {
        if (++guard > SPIN_LIMIT) return false;
    }
    LPSPI.TDR = word & 0xFFFFFFu;
    return wait_frame();
}

// Start a row transfer: 3936 clocks, output masked, received words DMA'd into buf.
// Receive `bits` clocks (a multiple of 32, at most one row) into buf by DMA.
static inline void start_rx(uint32_t* buf, uint32_t bits) {
    // Drop any cached copy first, so no line can be evicted over the DMA's data.
    arm_dcache_delete(buf, sizeof(s_row[0]));
    s_row_pending = buf;
    s_rx.destinationBuffer(buf, bits / 8);
    s_rx.enable();
    LPSPI.TCR = tcr_rx() | reg::framesz(bits) | reg::TCR_TXMSK;
}
static inline void start_row(uint32_t* buf) { start_rx(buf, ROW_BITS); }

static inline bool wait_row() {
    uint32_t guard = 0;
    while (!s_rx.complete()) {
        if (++guard > SPIN_LIMIT) return false;
    }
    s_rx.clearComplete();
    LPSPI.SR = reg::SR_FCF;
    // And again now it is complete: the CPU may have pulled lines in meanwhile.
    arm_dcache_delete(s_row_pending, sizeof(s_row[0]));
    return true;
}

static void configure_lpspi() {
    LPSPI.CR = 0;
    LPSPI.CR = reg::CR_RST;
    LPSPI.CR = 0;
    // OUTCFG: measured on hardware, with OUTCFG=0 ("retain last value") LPSPI drives SDO
    // HIGH about 48 ns after the last clock of every transmit frame -- when the internal
    // PCS negates. After the interface filler that left SDAT charged to 3.56 V at the
    // moment of release, decaying through the 100k pull-down (tau ~2.5 us) across the whole
    // last-PP window and into SYNC. With OUTCFG=1 SDO is tristated instead, so the pad
    // pull-down holds it where the last bit left it: low.
    LPSPI.CFGR1 = reg::CFGR1_MASTER | reg::CFGR1_OUTCFG |
                  (s_delayed_sample ? reg::CFGR1_SAMPLE : 0u);
    // SCK = root / (SCKDIV + 2). DBT = 0 keeps the gap between frames as short as possible,
    // so row boundaries cost the sensor as little as possible (spec.md section 6.5).
    LPSPI.CCR = (uint32_t)s_clock->sckdiv;
    LPSPI.FCR = 0;  // RX watermark 0: request DMA as soon as one word has arrived
    LPSPI.DER = reg::DER_RDDE;
    LPSPI.SR = reg::SR_STICKY;
    LPSPI.CR = reg::CR_RTF | reg::CR_RRF;
    LPSPI.CR = reg::CR_MEN;
}

// Bit-bang `n` clocks. LPSPI cannot make frames shorter than 8 bits, and the start-up
// sequence needs exactly 1 and then 10 clocks (AN000611 section 3.3). The reference host
// bit-banged these too (spec.md section 3.2).
//
// drive_sdat MUST be false once idle mode has been cleared: by then the sensor has entered
// INITIAL PRE-SYNC MODE and is driving SDAT itself, and the datasheet makes tristating the
// upstream driver before that point the host's responsibility.
static void bitbang_clocks(uint32_t n, bool drive_sdat) {
    pinMode(board::PIN_SCLK, OUTPUT);
    digitalWriteFast(board::PIN_SCLK, LOW);
    if (drive_sdat) {
        pinMode(board::PIN_SDAT_OUT, OUTPUT);
        digitalWriteFast(board::PIN_SDAT_OUT, LOW);
    } else {
        sdat_hiz();
    }
    for (uint32_t i = 0; i < n; i++) {
        delayNanoseconds(200);
        digitalWriteFast(board::PIN_SCLK, HIGH);
        delayNanoseconds(200);
        digitalWriteFast(board::PIN_SCLK, LOW);
    }
    // Hand SCLK back to LPSPI, restoring the pad settings pinMode() overwrote as well as
    // the mux: at 49.5 MHz the drive strength and slew configured by SPI1.begin() matter.
    *portControlRegister(board::PIN_SCLK) = s_pad_sclk;
    *portConfigRegister(board::PIN_SCLK) = s_mux_sclk;
    if (drive_sdat) {
        *portControlRegister(board::PIN_SDAT_OUT) = s_pad_sdat;
        *portConfigRegister(board::PIN_SDAT_OUT) = s_mux_sdat;
    }
}

// One 24-bit register write, bit-banged at about 1 MHz. At power-up output_mode defaults
// to LVDS, and in LVDS mode the configuration interface is specified only up to
// fSCLK_LVDS = 2.5 MHz (DS000503 Table 7). The writes that select SEIM therefore have to go
// out slowly; only once SEIM is selected does the 75 MHz fSCLK_SEIM limit apply.
// SDAT changes while SCLK is low; the sensor captures on the rising edge.
static void bitbang_write24(uint32_t word) {
    pinMode(board::PIN_SCLK, OUTPUT);
    pinMode(board::PIN_SDAT_OUT, OUTPUT);
    digitalWriteFast(board::PIN_SCLK, LOW);
    for (int b = 23; b >= 0; b--) {
        digitalWriteFast(board::PIN_SDAT_OUT, (word >> b) & 1);
        delayNanoseconds(500);
        digitalWriteFast(board::PIN_SCLK, HIGH);
        delayNanoseconds(500);
        digitalWriteFast(board::PIN_SCLK, LOW);
    }
    digitalWriteFast(board::PIN_SDAT_OUT, LOW);
    delayNanoseconds(500);
    *portControlRegister(board::PIN_SCLK) = s_pad_sclk;
    *portConfigRegister(board::PIN_SCLK) = s_mux_sclk;
    *portControlRegister(board::PIN_SDAT_OUT) = s_pad_sdat;
    *portConfigRegister(board::PIN_SDAT_OUT) = s_mux_sdat;
}

// --- Setup -----------------------------------------------------------------------------
void begin() {
    pinMode(board::PIN_SENSOR_EN, OUTPUT);
    digitalWriteFast(board::PIN_SENSOR_EN, LOW);  // sensor stays off until asked
    pinMode(board::PIN_SDAT_IN, INPUT);

    // Let the core library mux the pins, then remember the values so the SDAT direction can
    // be flipped, and the pins reclaimed after bit-banging, with single register writes.
    SPI1.begin();

    // LPSPI root clock = PLL2_PFD2 (396 MHz) / 4 = 99 MHz (spec.md section 5.3).
    //
    // This MUST come after SPI1.begin(): the core's begin() writes CBCMR itself, selecting
    // PLL3_PFD0 / 3 = 240 MHz. Done the other way round -- as it was until the first test
    // on hardware -- every rate came out 2.4x too fast: "12.375 MHz" ran at 30 MHz and
    // "49.5 MHz" at 120 MHz, past the sensor's 75 MHz maximum. Measured, not assumed:
    // 22.5 fps where 9.6 fps is the ceiling at 12.375 MHz.
    //
    // The clock gate must be off while CBCMR is changed.
    CCM_CCGR1 &= ~CCM_CCGR1_LPSPI3(CCM_CCGR_ON);
    uint32_t cbcmr = CCM_CBCMR;
    cbcmr &= ~(CCM_CBCMR_LPSPI_PODF_MASK | CCM_CBCMR_LPSPI_CLK_SEL_MASK);
    cbcmr |= CCM_CBCMR_LPSPI_PODF(3) | CCM_CBCMR_LPSPI_CLK_SEL(3);  // /4, PLL2_PFD2
    CCM_CBCMR = cbcmr;
    CCM_CCGR1 |= CCM_CCGR1_LPSPI3(CCM_CCGR_ON);
    add_pulldown(board::PIN_SCLK);
    add_pulldown(board::PIN_SDAT_OUT);
    add_pulldown(board::PIN_SDAT_IN);
    s_mux_sdat = *portConfigRegister(board::PIN_SDAT_OUT);
    s_mux_sclk = *portConfigRegister(board::PIN_SCLK);
    s_pad_sdat = *portControlRegister(board::PIN_SDAT_OUT);
    s_pad_sclk = *portControlRegister(board::PIN_SCLK);

    s_rx.begin();
    s_rx.source((volatile uint32_t&)LPSPI.RDR);
    s_rx.triggerAtHardwareEvent(DMAMUX_SOURCE_LPSPI3_RX);
    s_rx.disable();

    configure_lpspi();
    sdat_hiz();
}

// The NanoBerry's sensor rail has no discharge path: measured after EN goes low, it is
// still at 0.5 V after 143 ms and only below 0.1 V after 630 ms. Powering back up sooner
// risks a sensor that never saw a clean power-on reset, and the start-up sequence depends
// on one -- starts after a 0.4 s off-time failed. So power(true) makes sure the rail has
// been off at least this long, however soon after power(false) it is called.
constexpr uint32_t POR_OFF_MS = 1000;
static uint32_t s_off_since_ms = 0;
static bool s_ever_powered = false;

void power(bool on) {
    if (on && !s_powered && s_ever_powered) {
        while ((uint32_t)(millis() - s_off_since_ms) < POR_OFF_MS) {
            watchdog::feed();
            delay(10);
        }
    }
    digitalWriteFast(board::PIN_SENSOR_EN, on ? HIGH : LOW);
    if (s_powered && !on) s_off_since_ms = millis();
    s_powered = on;
    s_streaming = false;
    if (on) {
        s_ever_powered = true;
        delay(5);  // LDO ramp plus the sensor's internal power-on reset
        s_first_frame_after_por = true;
    }
}

bool powered() { return s_powered; }

const ClockSetting& set_clock(uint32_t want_hz) {
    s_clock = &nearest_clock(want_hz);
    Config1 c = Config1::unpack(s_cfg1);
    c.mclk_mode = s_clock->mclk_mode;
    c.high_speed = s_clock->high_speed;
    s_cfg1 = c.pack();
    configure_lpspi();
    return *s_clock;
}

// The LPSPI root clock as the hardware is actually configured, decoded from CBCMR the same
// way the core library does. Derived rather than assumed, so a frame header can never
// report a rate the peripheral is not running at -- which is exactly what happened when
// SPI1.begin() silently replaced our setting.
uint32_t lpspi_root_hz() {
    static const uint32_t sel_hz[4] = {
        664615384u,  // PLL3 PFD1
        720000000u,  // PLL3 PFD0
        528000000u,  // PLL2
        396000000u,  // PLL2 PFD2
    };
    const uint32_t cbcmr = CCM_CBCMR;
    return sel_hz[(cbcmr >> 4) & 0x3u] / (((cbcmr >> 26) & 0x7u) + 1u);
}

uint32_t sclk_hz() { return lpspi_root_hz() / ((uint32_t)s_clock->sckdiv + 2u); }

uint32_t nominal_sclk_hz() { return s_clock->sclk_hz; }

// Time real SCLK cycles against the CPU cycle counter: an independent check of the clock
// that needs no logic analyser. Clocks max-size frames with output and input masked, so it
// includes the small gap between frames and reads a fraction of a percent low.
//
// Only with the sensor unpowered: the sensor advances its state machine on these clocks.
uint32_t measure_sclk_hz() {
    if (s_powered) return 0;
    const uint32_t frames = 24;
    const uint32_t t0 = ARM_DWT_CYCCNT;
    for (uint32_t i = 0; i < frames; i++) {
        LPSPI.TCR = tcr_base() | reg::framesz(MAX_FRAME_BITS) | reg::TCR_TXMSK |
                    reg::TCR_RXMSK;
        if (!wait_frame()) return 0;
    }
    const uint32_t cycles = ARM_DWT_CYCCNT - t0;
    if (!cycles) return 0;
    return (uint32_t)((uint64_t)frames * MAX_FRAME_BITS * F_CPU_ACTUAL / cycles);
}

void set_delayed_sample(bool on) {
    s_delayed_sample = on;
    configure_lpspi();
}

bool delayed_sample() { return s_delayed_sample; }

void set_rx_phase(bool falling) { s_rx_cpha = falling; }
bool rx_phase() { return s_rx_cpha; }

// Schmitt-trigger input on the receive pin (IOMUXC pad HYS). Slow, capacitively loaded edges
// at the higher clock rates are where it might matter.
void set_input_hysteresis(bool on) {
    volatile uint32_t* pad = portControlRegister(board::PIN_SDAT_IN);
    *pad = on ? (*pad | IOMUXC_PAD_HYS) : (*pad & ~IOMUXC_PAD_HYS);
}
bool input_hysteresis() { return (*portControlRegister(board::PIN_SDAT_IN) & IOMUXC_PAD_HYS) != 0; }

void set_align_clocks(uint32_t n) { s_align_clocks = n; }
uint32_t align_clocks() { return s_align_clocks; }

void set_config(uint16_t cfg0, uint16_t cfg1) {
    s_cfg0 = cfg0;
    s_cfg1 = cfg1;
}

uint16_t config0() { return s_cfg0; }
uint16_t config1() { return s_cfg1; }

// --- Phases ----------------------------------------------------------------------------
// INTERFACE MODE: exactly 324 frames of 24 bits with SDAT driven. The two register writes
// go first; the datasheet forbids writing in the last pixel period and asks for the bus to
// stay driven for the whole window to keep EMI off the floating line.
// Receive one pixel period with SDAT released, polling rather than using DMA.
static bool receive_one_pp(uint16_t& out) {
    LPSPI.DER = 0;                         // keep this word out of the row DMA
    LPSPI.CR = reg::CR_MEN | reg::CR_RRF;  // nothing stale in the RX FIFO
    LPSPI.TCR = tcr_rx() | reg::framesz(PP_BITS) | reg::TCR_TXMSK;
    bool ok = wait_frame();
    uint32_t guard = 0;
    while (ok && (LPSPI.RSR & reg::RSR_RXEMPTY)) {
        if (++guard > SPIN_LIMIT) ok = false;
    }
    out = ok ? (uint16_t)(LPSPI.RDR & 0xFFFu) : 0xFFFF;
    LPSPI.DER = reg::DER_RDDE;
    return ok;
}

// INTERFACE MODE, 648 PP. We drive the first 647 and hand the last to the sensor.
//
// DS000503 section 6.4.3: "To signalize the end of the INTERFACE MODE, the device transmits
// a specific word in the last PP" -- 0x015 in SEIM -- and register writes are forbidden
// there. AN000611's recipe, which the reference host follows, drives all 648 instead. The
// reference capture cannot settle which is right: if the sensor did transmit, the host's
// GPIO would simply out-drive its current-limited output and the analyser would still read
// 0x000. Releasing costs nothing if the sensor is silent and avoids a fight every frame if
// it is not. Receiving that PP rather than discarding it turns the question into a
// measurement, reported by PROBE and STATS.
static void interface_window(uint16_t cfg0, uint16_t cfg1) {
    sdat_drive();
    send24(reg_write_packet(0, cfg0));
    send24(reg_write_packet(1, cfg1));
    drive_zeros(INTERFACE_BITS - 2 * REG_WRITE_BITS - PP_BITS);
    sdat_hiz();  // released a whole PP before SYNC: no overlap at the phase boundary
    receive_one_pp(s_last_interface_pp);
}

uint16_t last_interface_pp() { return s_last_interface_pp; }

void listen(uint32_t rows, ListenReport& rep, char* map, uint32_t map_len) {
    memset(&rep, 0, sizeof(rep));
    rep.first_active_row = -1;
    sdat_hiz();
    for (uint32_t r = 0; r < rows; r++) {
        watchdog::feed();  // LISTEN 2000 takes ~0.7 s
        start_row(s_row[0]);
        if (!wait_row()) {
            s_rx.disable();
            break;
        }
        uint32_t n555 = 0, nAAA = 0, nzero = 0, npix = 0;
        for (uint32_t i = 0; i < ROW_PP; i++) {
            const uint16_t w = pp_at(s_row[0], i);
            if (w == WORD_TRAINING) n555++;
            else if (w == WORD_PRESYNC) nAAA++;
            else if (w == WORD_EOF) nzero++;
            else if (word_is_pixel(w)) npix++;
        }
        const uint32_t nother = ROW_PP - n555 - nAAA - nzero - npix;
        rep.words_555 += n555;
        rep.words_AAA += nAAA;
        rep.words_zero += nzero;
        rep.words_pixel += npix;
        rep.words_other += nother;
        if (rep.first_active_row < 0 && nzero < ROW_PP) {
            rep.first_active_row = (int32_t)r;
            for (uint32_t i = 0; i < 12; i++) rep.first_active_words[i] = pp_at(s_row[0], i);
        }
        char c = '?';
        if (nzero == ROW_PP) c = '.';
        else if (n555 * 2 > ROW_PP) c = 'S';
        else if (nAAA * 2 > ROW_PP) c = 'A';
        else if (npix * 2 > ROW_PP) c = 'P';
        if (r + 1 < map_len) map[r] = c;
        rep.rows = r + 1;
    }
    if (map_len) map[rep.rows < map_len ? rep.rows : map_len - 1] = 0;
}

static inline uint32_t sync_delay_pp() {
    return SYNC_PP + rows_delay_pp(Config1::unpack(s_cfg1).rows_delay);
}

uint32_t presync_training() { return s_presync_training; }

// The reference's CONFIG_1 values with the clock bits replaced to match the SCLK actually
// in use: the reference ran at 31.25 MHz and its 0x0065 says so (mclk default, high speed).
static uint16_t ref_cfg1(uint16_t reference) {
    Config1 c = Config1::unpack(reference);
    c.mclk_mode = s_clock->mclk_mode;
    c.high_speed = s_clock->high_speed;
    return c.pack();
}

void start_reference(bool verbatim, bool fast_first, bool early_release, bool first_only) {
    s_streaming = false;
    if (s_powered) power(false);  // needs a fresh power-on reset, like start()
    power(true);
    // verbatim: the reference's exact register values, clock bits and all.
    const uint16_t cfg1_idle = verbatim ? 0x009F : ref_cfg1(0x009F);
    const uint16_t cfg1_run = verbatim ? 0x0065 : ref_cfg1(0x0065);
    s_cfg0 = 0x009F;
    s_cfg1 = cfg1_run;
    bitbang_clocks(1, true);
    if (fast_first) {
        sdat_drive();
        send24(reg_write_packet(0, 0x009F));
        send24(reg_write_packet(1, cfg1_idle));
    } else {
        bitbang_write24(reg_write_packet(0, 0x009F));
        bitbang_write24(reg_write_packet(1, cfg1_idle));
    }
    if (first_only) {
        sdat_hiz();
        return;
    }
    // The reference clocked exactly this many times, SDAT low, before releasing idle -- the
    // count of 10 alignment clocks + INITIAL PRE-SYNC + SYNC/DELAY + one frame.
    sdat_drive();
    drive_zeros(10 + (PRESYNC_PP + 2 * SYNC_PP + READOUT_PP) * PP_BITS);
    send24(reg_write_packet(0, 0x009F));
    send24(reg_write_packet(1, cfg1_run));
    if (!early_release) drive_zeros(INTERFACE_BITS - 2 * REG_WRITE_BITS);
    sdat_hiz();
}

// Clock `bits` clocks with the output masked, discarding everything received. Bit-granular,
// unlike clock_pp_discard(): it is what moves the transfers onto the sensor's row phase.
static bool clock_bits_discard(uint32_t bits) {
    while (bits) {
        const uint32_t chunk = bits > MAX_FRAME_BITS ? MAX_FRAME_BITS : bits;
        LPSPI.TCR = tcr_base() | reg::framesz(chunk) | reg::TCR_TXMSK | reg::TCR_RXMSK;
        if (!wait_frame()) return false;
        bits -= chunk;
    }
    return true;
}

static inline uint32_t row_bit(const uint32_t* w, uint32_t i) {
    return (w[i >> 5] >> (31u - (i & 31u))) & 1u;
}

// --- Sampling-point calibration --------------------------------------------------------
// Where in each bit the receiver samples decides whether the link works at all at the
// higher clock rates. The sensor launches a bit ~10 ns after the SCLK edge reaches it, and
// the edge and the data both cross the wiring, so at 49.5 MHz (20 ns bits) the round trip
// is a large fraction of a bit: sampling on the rising edge landed on the transition (79 %
// of words corrupt), on the falling edge in the middle of the bit (no errors in 420,000
// words). At 12.375 MHz every point works. Measured with tools/link_quality.py.
//
// So START measures instead of assuming. Straight after SDAT is released the sensor sends
// ~12,000 bits of pure alternating training pattern: a known signal, and the hardest one
// for the link. Each of the four sampling points (rising or falling edge, with or without
// CFGR1[SAMPLE]'s extra delay) receives CAL_BITS of it, and the one with the fewest breaks
// in the alternation wins; ties go to the earlier entry in SAMPLE_POINTS. This uses about
// a third of the training, which is thrown away anyway, and leaves plenty for the row lock.
struct SamplePoint {
    bool delayed;
    bool falling;
};
static constexpr SamplePoint SAMPLE_POINTS[4] = {
    {false, false},  // rising edge: the reference host's choice, proven at <= 24.75 MHz
    {false, true},   // falling edge: the only clean point at 49.5 MHz on the bench wiring
    {true, false},   // rising edge + one LPSPI clock
    {true, true},    // falling edge + one LPSPI clock
};
static constexpr uint32_t CAL_BITS = 1024;
static constexpr uint32_t VERIFY_ROWS = 8;
static bool s_auto_sample = true;
static SampleCal s_cal = {};

static void apply_sample_point(const SamplePoint& p) {
    s_rx_cpha = p.falling;
    if (s_delayed_sample != p.delayed) {
        s_delayed_sample = p.delayed;
        configure_lpspi();
    }
}

// Breaks in what should be a perfectly alternating bit stream.
static uint32_t alternation_errors(const uint32_t* w, uint32_t bits) {
    uint32_t errors = 0;
    for (uint32_t i = 0; i + 1 < bits; i++)
        if (row_bit(w, i) == row_bit(w, i + 1)) errors++;
    return errors;
}

static bool calibrate_sampling() {
    s_cal.automatic = s_auto_sample;
    if (!s_auto_sample) return true;
    uint32_t best = 0;
    for (uint32_t k = 0; k < 4; k++) {
        apply_sample_point(SAMPLE_POINTS[k]);
        start_rx(s_row[0], CAL_BITS);
        if (!wait_row()) {
            s_rx.disable();
            return false;
        }
        s_cal.errors[k] = alternation_errors(s_row[0], CAL_BITS);
        if (s_cal.errors[k] < s_cal.errors[best]) best = k;
    }
    s_cal.choice = (uint8_t)best;
    apply_sample_point(SAMPLE_POINTS[best]);
    return true;
}

const SampleCal& sample_calibration() { return s_cal; }
void set_auto_sample(bool on) { s_auto_sample = on; }
bool auto_sample() { return s_auto_sample; }

// Find the first frame's row phase in the received bits and move the transfers onto it,
// then discard the rest of that frame, leaving the link at the start of INTERFACE MODE.
//
// How long the first frame's training lasts, counted in SCLK clocks, varies from start to
// start (12,074 to 12,084 bits measured, and not in whole pixel periods), so the row phase
// cannot be counted from the idle-off write: it has to be found, as any serial receiver
// would. Row 0 of the first frame is trained with 0xAAA, which runs straight on into its
// first pixel's start bit, so the lock is on row 1: a run of about 96 alternating bits
// (8 x 0x555, ending in 1) broken by two 1s -- the second is pixel 0's start bit. The row
// after that is then checked for its 8 training words before anything is trusted.
static uint32_t s_lock_false_candidates = 0;

static bool lock_row_phase() {
    // Long enough for the longest training seen on any module: the mono part sends about
    // 3 rows' worth, the colour part about 6, and the search costs nothing once it hits.
    constexpr uint32_t MAX_SEARCH_ROWS = 48;
    uint32_t last = 2;                      // no previous bit yet
    uint32_t run = 0;                       // alternations in the current run
    bool seen_long = false;                 // the first frame's long training run
    for (uint32_t row = 0; row < MAX_SEARCH_ROWS; row++) {
        start_row(s_row[0]);
        if (!wait_row()) {
            s_rx.disable();
            return false;
        }
        for (uint32_t i = 0; i < ROW_BITS; i++) {
            const uint32_t b = row_bit(s_row[0], i);
            if (b != last && last != 2) {
                run++;
            } else {
                if (seen_long && run >= 90 && run < 200 && b == 1) {
                    // Bit i is row 1's pixel-0 start bit; row 1 began 96 bits earlier.
                    // Move to the next row boundary still ahead of us.
                    const uint32_t next_row = (i >= 96) ? 2 : 3;
                    const uint32_t skip = (i >= 96) ? i - 96 : i - 96 + ROW_BITS;
                    if (skip && !clock_bits_discard(skip)) return false;
                    start_row(s_row[0]);
                    if (!wait_row()) {
                        s_rx.disable();
                        return false;
                    }
                    if (count_training(s_row[0], WORD_TRAINING) < TRAINING_PP) {
                        // A false candidate: some other pair of equal bits inside the
                        // training. We are still on a row boundary, so keep looking from
                        // here rather than failing the whole start, which is what this
                        // used to do -- one unlucky bit pattern and nothing streamed.
                        s_lock_false_candidates++;
                        last = 2;
                        run = 0;
                        break;
                    }
                    // The first frame is thrown away, so use some of it to check the
                    // chosen sampling point on real pixel data before trusting it.
                    const uint32_t verify = VERIFY_ROWS < HEIGHT - 1 - next_row
                                                ? VERIFY_ROWS : HEIGHT - 1 - next_row;
                    s_cal.verify_rows = verify;
                    s_cal.verify_bad_words = 0;
                    for (uint32_t v = 0; v < verify; v++) {
                        start_row(s_row[0]);
                        if (!wait_row()) {
                            s_rx.disable();
                            return false;
                        }
                        uint16_t px[WIDTH];
                        s_cal.verify_bad_words += extract_row(s_row[0], px) +
                            (TRAINING_PP - count_training(s_row[0], WORD_TRAINING));
                    }
                    // Rest of the frame: the remaining rows, then EOF.
                    return clock_pp_discard((HEIGHT - 1 - next_row - verify) * ROW_PP +
                                            EOF_PP);
                }
                if (run >= 1000) seen_long = true;
                run = 0;
            }
            last = b;
        }
    }
    return false;
}

// The start-up that works on hardware, every time: the reference host's sequence, with our
// own register values (spec.md section 3.2):
//
//   1 activation clock, CONFIG_0 + CONFIG_1 (SEIM, idle on), bit-banged at ~1 MHz
//   REF_IDLE_CLOCKS clocks with SDAT driven low -- what the reference does, one frame's worth
//   CONFIG_0 + CONFIG_1 (idle off) at SCLK rate, then zeros to the end of a 648 PP window
//   release SDAT: the sensor sends ~1000 PP of training (INITIAL PRE-SYNC + SYNC, no DELAY
//   in this first frame), then the first frame's 320 rows, then EOF
//
// and then lock_row_phase(). The datasheet's own sequence (AN000611) did not start reliably
// on this board, and its fixed phase count left every row 2 clocks off.
static constexpr uint32_t REF_IDLE_CLOCKS =
    10 + (PRESYNC_PP + 2 * SYNC_PP + READOUT_PP) * PP_BITS;

static bool start_like_reference(Config1 c1, bool require_sensor) {
    // The first frame always runs with the minimum delay, so the frame lock_row_phase()
    // discards has a known length. The real value goes out in the first capture's
    // interface window.
    Config1 first = c1;
    first.rows_delay = 0;
    first.idle_mode = 1;
    const uint16_t cfg1_idle = first.pack();
    first.idle_mode = 0;
    const uint16_t cfg1_run = first.pack();
    c1.idle_mode = 0;
    s_cfg1 = c1.pack();

    bitbang_clocks(1, true);
    bitbang_write24(reg_write_packet(0, s_cfg0));
    bitbang_write24(reg_write_packet(1, cfg1_idle));
    sdat_drive();
    if (!drive_zeros(REF_IDLE_CLOCKS)) return false;
    if (!send24(reg_write_packet(0, s_cfg0))) return false;
    if (!send24(reg_write_packet(1, cfg1_run))) return false;
    if (!drive_zeros(INTERFACE_BITS - 2 * REG_WRITE_BITS)) return false;
    sdat_hiz();

    // Choose where in each bit to sample, on the training pattern now arriving.
    s_cal.verify_rows = 0;
    if (!calibrate_sampling()) return false;

    // First training row: the "is there a sensor at all?" check.
    start_row(s_row[0]);
    if (!wait_row()) {
        s_rx.disable();
        return false;
    }
    for (uint32_t i = 0; i < ROW_PP; i++) {
        const uint16_t w = pp_at(s_row[0], i);
        if (w == WORD_PRESYNC || w == WORD_TRAINING) s_presync_training++;
    }
    if (require_sensor && s_presync_training < ROW_PP / 2) return false;

    // Lock onto the first frame's rows, then discard the rest of it: its exposure is invalid
    // (datasheet 6.3.2.2; confirmed saturated in the reference capture, spec.md section 3.5).
    if (!lock_row_phase() && require_sensor) return false;

    s_first_frame_after_por = false;
    s_streaming = true;
    return true;
}

constexpr uint32_t START_ATTEMPTS = 3;
static uint32_t s_start_attempts = 0;

uint32_t start_attempts() { return s_start_attempts; }
uint32_t lock_false_candidates() { return s_lock_false_candidates; }

bool start(bool require_sensor, bool an_sequence) {
    s_streaming = false;
    s_start_attempts = 1;
    s_lock_false_candidates = 0;
    s_presync_training = 0;
    // Both sequences need a sensor fresh from power-on reset.
    if (s_powered) power(false);
    power(true);

    Config1 c1 = Config1::unpack(s_cfg1);
    c1.output_mode = 0;  // SEIM
    c1.mclk_mode = s_clock->mclk_mode;
    c1.high_speed = s_clock->high_speed;

    if (!an_sequence) {
        // Locking depends on catching one transition in one pass of the first frame. If it
        // misses, the only way back to a known state is another power-on reset, so try a
        // few times before giving up on the sensor.
        for (uint32_t attempt = 0; attempt < START_ATTEMPTS; attempt++) {
            if (attempt) {
                power(false);
                power(true);
                s_presync_training = 0;
            }
            if (start_like_reference(c1, require_sensor)) {
                s_start_attempts = attempt + 1;
                return true;
            }
        }
        s_start_attempts = START_ATTEMPTS;
        return false;
    }

    // AN000611's single-write sequence, kept for comparison only. On hardware it is not
    // reliable: some starts see no pre-sync at all, and when it does start, the phase count
    // below lands every row 2 clocks late (tools/check_alignment.py), so every row fails.
    bitbang_clocks(1, true);  // activation clock, SDAT low as in the reference capture

    // Release idle: the sensor starts streaming after this write.
    c1.idle_mode = 0;
    s_cfg1 = c1.pack();
    bitbang_write24(reg_write_packet(0, s_cfg0));
    bitbang_write24(reg_write_packet(1, s_cfg1));
    sdat_hiz();

    // AN000611 section 3.3 waits here -- after the idle-off write, before the alignment
    // clocks -- "for IDLE start-up", 10 us minimum, "longer times are also possible". This
    // pause used to sit between the two write pairs instead, so the alignment clocks arrived
    // about a microsecond after idle was released, while the sensor was still starting up.
    delayMicroseconds(100);

    // 10 alignment clocks fix the 12-bit word phase, then INITIAL PRE-SYNC MODE. SDAT is
    // left released: the sensor is already transmitting by this point.
    bitbang_clocks(s_align_clocks, false);

    // INITIAL PRE-SYNC is 329 PP of training pattern. Receive the first 328 as one row --
    // this is the only moment the sensor is guaranteed to be sending a known pattern before
    // any image, so it is where "is there a sensor at all?" gets answered -- and clock the
    // remaining PP, so the phase count is exactly what it would have been.
    start_row(s_row[0]);
    if (!wait_row()) {
        s_rx.disable();
        return false;
    }
    for (uint32_t i = 0; i < ROW_PP; i++) {
        const uint16_t w = pp_at(s_row[0], i);
        if (w == WORD_PRESYNC || w == WORD_TRAINING) s_presync_training++;
    }
    if (!clock_pp_discard(PRESYNC_PP - ROW_PP)) return false;
    if (require_sensor && s_presync_training < ROW_PP / 2) return false;

    // SYNC + DELAY, then the first frame, which is discarded: its exposure is invalid
    // (confirmed saturated in the reference capture, spec.md section 3.5).
    if (!clock_pp_discard(sync_delay_pp())) return false;
    if (!clock_pp_discard(READOUT_PP)) return false;

    s_first_frame_after_por = false;
    s_streaming = true;
    return true;
}

void stop() {
    Config1 c = Config1::unpack(s_cfg1);
    c.idle_mode = 1;
    s_cfg1 = c.pack();
    interface_window(s_cfg0, s_cfg1);
    s_streaming = false;
}

bool streaming() { return s_streaming; }

// Fault injection (INJECT): corrupt this many random pixel words per frame after they have
// been received, as a real bit error would -- start bit knocked out, data scrambled -- so
// detection, concealment and the counters can be exercised on a clean link.
static uint32_t s_inject_per_frame = 0;
static uint32_t s_rng = 0x2545F491u;
static inline uint32_t xorshift() {
    s_rng ^= s_rng << 13;
    s_rng ^= s_rng >> 17;
    s_rng ^= s_rng << 5;
    return s_rng;
}
void set_inject(uint32_t per_frame) { s_inject_per_frame = per_frame; }

static bool s_conceal = true;
void set_conceal(bool on) { s_conceal = on; }
bool conceal() { return s_conceal; }
uint32_t inject() { return s_inject_per_frame; }

static void inject_errors(uint32_t* row) {
    const uint32_t n = s_inject_per_frame / HEIGHT +
                       ((xorshift() % HEIGHT) < s_inject_per_frame % HEIGHT ? 1u : 0u);
    for (uint32_t k = 0; k < n; k++)
        set_pp(row, TRAINING_PP + xorshift() % WIDTH, (uint16_t)(xorshift() & 0x7FEu));
}

bool capture_frame(uint8_t* dst, uint8_t format, FrameInfo& info, IdleFn idle) {
    memset(&info, 0, sizeof(info));

    // 1. INTERFACE MODE, rewriting both registers as the reference host does.
    interface_window(s_cfg0, s_cfg1);

    // 2. SYNC + DELAY, discarded.
    if (!clock_pp_discard(sync_delay_pp())) return false;

    // 3. READOUT. Row n's DMA runs while row n-1 is unpacked and the host is serviced.
    info.timestamp_us = micros();
    const uint32_t row_bytes = row_payload_bytes(format);
    const uint16_t expect = s_first_frame_after_por ? WORD_PRESYNC : WORD_TRAINING;

    start_row(s_row[0]);
    for (uint32_t r = 0; r < HEIGHT; r++) {
        uint32_t* cur = s_row[r & 1];
        if (!wait_row()) {
            s_rx.disable();  // do not leave a transfer armed for a later stray request
            info.rows_failed += HEIGHT - r;
            return false;
        }
        // Arm the next row first, then do the slow work while it is in flight.
        if (r + 1 < HEIGHT) start_row(s_row[(r + 1) & 1]);

        if (s_inject_per_frame) inject_errors(cur);
        bool row_bad = count_training(cur, expect) < TRAINING_PP;
        uint8_t* out = dst + (size_t)r * row_bytes;
        uint32_t bad;
        uint32_t concealed = 0;
        switch (format) {
            case 1: bad = unpack_row_gray10(cur, out, &concealed, s_conceal); break;
            case 2: bad = unpack_row_raw12(cur, out); break;
            default: bad = unpack_row_gray8(cur, out, &concealed, s_conceal); break;
        }
        info.pixels_failed += bad;
        info.pixels_concealed += concealed;
        if (row_bad) info.rows_sync_lost++;
        if (bad || row_bad) info.rows_failed++;
        if (idle) idle();
    }
    info.duration_us = micros() - info.timestamp_us;

    // 4. End of frame.
    if (!clock_pp_discard(EOF_PP)) return false;
    return true;
}

// Bring-up diagnostic (M2). Runs a complete, phase-correct frame cycle but tallies word
// statistics over the first `rows` rows instead of producing an image, then clocks out the
// rest of the frame so the sensor's state machine stays aligned.
void probe_sync(SyncReport& report, uint32_t rows) {
    memset(&report, 0, sizeof(report));
    if (rows > HEIGHT) rows = HEIGHT;

    interface_window(s_cfg0, s_cfg1);
    if (!clock_pp_discard(sync_delay_pp())) return;

    for (uint32_t r = 0; r < rows; r++) {
        start_row(s_row[0]);
        if (!wait_row()) {
            s_rx.disable();
            return;
        }
        for (uint32_t i = 0; i < ROW_PP; i++) {
            const uint16_t w = pp_at(s_row[0], i);
            if (report.words < 16) report.first_words[report.words] = w;
            report.words++;
            if (w == WORD_TRAINING) report.training_555++;
            else if (w == WORD_PRESYNC) report.training_AAA++;
            else if (w == WORD_EOF) report.zeros++;
            if (word_is_pixel(w)) report.pixel_like++;
        }
    }
    clock_pp_discard((HEIGHT - rows) * ROW_PP + EOF_PP);
}

}  // namespace seim
