// USB framing between the Teensy and the PC host. See spec.md section 7.
//
// One framed binary transport carries images, command responses and log lines, so ASCII
// output can never corrupt image parsing. Mirrored by host/naneye/protocol.py; the layout
// is asserted at compile time here and in tests/test_protocol.py on the host side.
#pragma once

#include <stdint.h>
#include <stddef.h>

namespace proto {

constexpr uint32_t MAGIC = 0x454E414Eu;  // "NANE" little-endian
constexpr uint8_t VERSION = 1;

enum Type : uint8_t {
    TYPE_IMAGE = 0,
    TYPE_COMMAND = 1,
    TYPE_RESPONSE = 2,
    TYPE_LOG = 3,
};

enum Format : uint8_t {
    FMT_GRAY8 = 0,   // 1 byte per pixel, 10-bit truncated to 8
    FMT_GRAY10 = 1,  // packed, 5 bytes per 4 pixels
    FMT_RAW12 = 2,   // raw pixel periods, 2 bytes each, for diagnostics
};

enum Flags : uint8_t {
    FLAG_SYNC_LOST = 1u << 0,
    FLAG_CLOCK_GAP = 1u << 1,
    FLAG_FIRST_DISCARDED = 1u << 2,
    FLAG_CONCEALED = 1u << 3,  // some pixels were corrupt and replaced by their neighbours
    // Bits 4-6 carry the colour filter array, so a frame says for itself whether it is a
    // mosaic and which phase it starts on. Mono and colour NanEyeCs are the same part
    // number to the link: nothing in the data distinguishes them, so it is configured once
    // (CFA command, kept in EEPROM) and reported with every frame.
    FLAG_CFA_SHIFT = 4,
    FLAG_CFA_MASK = 7u << 4,
};

// The 2x2 the first received pixel starts: the sensor reads pixel (1,1), the bottom left
// one, first, and on a colour part that pixel is blue (datasheet 6.3.1) -> CFA_BGGR.
enum Cfa : uint8_t {
    CFA_MONO = 0,
    CFA_BGGR = 1,
    CFA_GBRG = 2,
    CFA_GRBG = 3,
    CFA_RGGB = 4,
};

inline uint8_t cfa_of(uint8_t flags) { return (flags & FLAG_CFA_MASK) >> FLAG_CFA_SHIFT; }
inline uint8_t flags_with_cfa(uint8_t flags, uint8_t cfa) {
    return (uint8_t)((flags & ~FLAG_CFA_MASK) | ((cfa << FLAG_CFA_SHIFT) & FLAG_CFA_MASK));
}

// 52-byte header. All fields little-endian; crc32 covers header[0..47] plus the payload.
struct __attribute__((packed)) Header {
    uint32_t magic;
    uint8_t version;
    uint8_t type;
    uint16_t header_len;
    uint32_t payload_len;
    uint32_t frame_counter;
    uint32_t timestamp_us;
    uint16_t width;
    uint16_t height;
    uint8_t format;
    uint8_t flags;
    uint16_t rows_failed;
    uint32_t sclk_hz;
    uint32_t exposure_pp;
    uint16_t cfg0;
    uint16_t cfg1;
    uint32_t frames_dropped;
    uint32_t pixels_concealed;  // was reserved (always 0); see seim_unpack.h extract_row()
    uint32_t crc32;
};

static_assert(sizeof(Header) == 52, "protocol header must be 52 bytes");
static_assert(offsetof(Header, payload_len) == 8, "layout drift");
static_assert(offsetof(Header, frame_counter) == 12, "layout drift");
static_assert(offsetof(Header, timestamp_us) == 16, "layout drift");
static_assert(offsetof(Header, format) == 24, "layout drift");
static_assert(offsetof(Header, sclk_hz) == 28, "layout drift");
static_assert(offsetof(Header, exposure_pp) == 32, "layout drift");
static_assert(offsetof(Header, cfg0) == 36, "layout drift");
static_assert(offsetof(Header, frames_dropped) == 40, "layout drift");
static_assert(offsetof(Header, crc32) == 48, "layout drift");

constexpr size_t HEADER_CRC_BYTES = 48;  // header bytes covered by the CRC

void crc32_init();
uint32_t crc32_update(uint32_t crc, const void* data, size_t len);
inline uint32_t crc32_begin() { return 0xFFFFFFFFu; }
inline uint32_t crc32_final(uint32_t crc) { return crc ^ 0xFFFFFFFFu; }

// Fill in the invariant header fields. Caller sets the rest, then calls finish().
void header_init(Header& h, uint8_t type, uint32_t payload_len);
// Compute and store the CRC over the header and payload.
void header_finish(Header& h, const void* payload, size_t payload_len);

// Send a framed packet on the USB serial port (blocking, with a bounded wait).
bool send(const Header& h, const void* payload, size_t payload_len);
// Convenience: a text packet (response or log).
void send_text(uint8_t type, const char* fmt, ...);

}  // namespace proto
