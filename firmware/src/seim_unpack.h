// Extraction of 12-bit pixel periods from the 32-bit words delivered by LPSPI/DMA.
//
// The sensor sends MSB first and LPSPI (LSBF=0) puts the first received bit in bit 31 of
// each received word, so a row is simply a big-endian bit stream. One row is
// 328 PP * 12 = 3936 bits = 123 words exactly, and 3 words = 96 bits = 8 PP exactly.
//
// Pure logic, no hardware. Mirrored by host/naneye/decode.py::unpack_words(); the two are
// checked against each other and against the golden capture in tests/test_unpack.py.
#pragma once

#include <stdint.h>

#include "naneye_regs.h"

namespace naneye {

// Words per row, plus one word of padding so pp_at() may always read w[wi + 1].
constexpr uint32_t ROW_WORDS = ROW_PP * PP_BITS / 32;  // 123
constexpr uint32_t ROW_WORDS_PADDED = ROW_WORDS + 1;

// The idx-th 12-bit pixel period of a big-endian bit stream.
// Requires w[] to hold at least (idx * 12 + 12 + 31) / 32 + 1 words.
static inline uint16_t pp_at(const uint32_t* w, uint32_t idx) {
    const uint32_t bit = idx * PP_BITS;
    const uint32_t wi = bit >> 5;
    const uint32_t off = bit & 31u;
    const uint64_t acc = ((uint64_t)w[wi] << 32) | (uint64_t)w[wi + 1];
    return (uint16_t)((acc >> (52u - off)) & 0xFFFu);
}

// Beyond this many broken words the row is lost rather than damaged: concealment would
// only invent data, and the row is reported as failed either way.
constexpr uint32_t CONCEAL_MAX_PER_ROW = 32;

// Extract one row's 320 pixel values, skipping the 8 training words.
//
// Error concealment: a word whose start or stop bit is wrong is known to be corrupt, so
// its value is replaced by the mean of the nearest intact pixels to the left and right on
// the same row (or the one that exists, at an edge). This cannot correct errors in the ten
// data bits of a word whose framing survived -- SEIM carries no other redundancy -- but it
// keeps a detected error from reaching the image as a full-scale speck.
//
// Returns the number of words that failed validation; *concealed (if given) receives how
// many were replaced.
static inline uint32_t extract_row(const uint32_t* w, uint16_t* px,
                                   uint32_t* concealed = nullptr) {
    uint32_t bad = 0;
    uint8_t ok[WIDTH];
    for (uint32_t i = 0; i < WIDTH; i++) {
        const uint16_t word = pp_at(w, TRAINING_PP + i);
        ok[i] = word_is_pixel(word) ? 1 : 0;
        bad += 1u - ok[i];
        px[i] = word_pixel(word);
    }
    uint32_t fixed = 0;
    if (bad && bad <= CONCEAL_MAX_PER_ROW) {
        for (uint32_t i = 0; i < WIDTH; i++) {
            if (ok[i]) continue;
            int32_t l = (int32_t)i - 1, r = (int32_t)i + 1;
            while (l >= 0 && !ok[l]) l--;
            while (r < (int32_t)WIDTH && !ok[r]) r++;
            if (l >= 0 && r < (int32_t)WIDTH) px[i] = (uint16_t)((px[l] + px[r] + 1u) / 2u);
            else if (l >= 0) px[i] = px[l];
            else if (r < (int32_t)WIDTH) px[i] = px[r];
            else continue;
            fixed++;
        }
    }
    if (concealed) *concealed = fixed;
    return bad;
}

// Unpack one captured row into packed 10-bit pixels: 4 pixels per 5 bytes, little-endian
// within the group (p0 low 8 bits, then the spare 2 bits of each pixel in the 5th byte).
static inline uint32_t unpack_row_gray10(const uint32_t* w, uint8_t* out,
                                         uint32_t* concealed = nullptr) {
    uint16_t all[WIDTH];
    const uint32_t bad = extract_row(w, all, concealed);
    for (uint32_t i = 0; i < WIDTH; i += 4) {
        const uint16_t* px = all + i;
        uint8_t* o = out + (i >> 2) * 5;
        o[0] = (uint8_t)(px[0] & 0xFF);
        o[1] = (uint8_t)(px[1] & 0xFF);
        o[2] = (uint8_t)(px[2] & 0xFF);
        o[3] = (uint8_t)(px[3] & 0xFF);
        o[4] = (uint8_t)(((px[0] >> 8) & 3) | (((px[1] >> 8) & 3) << 2) |
                         (((px[2] >> 8) & 3) << 4) | (((px[3] >> 8) & 3) << 6));
    }
    return bad;
}

// Raw diagnostic mode: every pixel period as a 16-bit value, training words included.
static inline uint32_t unpack_row_raw12(const uint32_t* w, uint8_t* out) {
    uint16_t* o = (uint16_t*)out;
    for (uint32_t i = 0; i < ROW_PP; i++) o[i] = pp_at(w, i);
    return 0;
}

// Overwrite the idx-th 12-bit pixel period of a big-endian bit stream. Test support: this is
// how SELFTEST manufactures a corrupt word.
static inline void set_pp(uint32_t* w, uint32_t idx, uint16_t value) {
    for (uint32_t b = 0; b < PP_BITS; b++) {
        const uint32_t bit = idx * PP_BITS + b;
        const uint32_t mask = 1u << (31u - (bit & 31u));
        if ((value >> (PP_BITS - 1u - b)) & 1u) w[bit >> 5] |= mask;
        else w[bit >> 5] &= ~mask;
    }
}

// Bytes a single row occupies in the outgoing payload: packed 10-bit pixels, or whole
// pixel periods for the raw diagnostic format.
static inline uint32_t row_payload_bytes(uint8_t format) {
    return (format == 2) ? ROW_PP * 2 : WIDTH / 4 * 5;   // FMT_RAW12 : FMT_GRAY10
}

// How many of the 8 leading training words match the expected pattern. Used for sync
// checking: 0x555 normally, 0xAAA for the very first row after power-on reset.
static inline uint32_t count_training(const uint32_t* w, uint16_t expect) {
    uint32_t n = 0;
    for (uint32_t i = 0; i < TRAINING_PP; i++)
        if (pp_at(w, i) == expect) n++;
    return n;
}

}  // namespace naneye
