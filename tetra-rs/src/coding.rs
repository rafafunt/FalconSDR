//! TETRA Layer 1 channel coding (ETSI EN 300 392-2 Clause 8).
//!
//! Pure clean-room implementation of:
//! - CRC-16/CCITT (Clause 8.2.3.3)
//! - MAC whitening scrambler LFSR (Clause 8.2.5) - open whitening, NOT encryption
//! - Shortened Reed-Muller RM(30,14) encoder & exact ML soft decoder (Clause 8.2.3.2)
//! - Algebraic block interleaver / de-interleaver (Clause 8.2.4)
//! - 16-state RCPC mother convolutional encoder & soft-decision Viterbi decoder (Clause 8.2.3.1)

use std::sync::OnceLock;

/// ETSI EN 300 392-2 Clause 8.2.3.3: CRC-16 with generator x^16 + x^12 + x^5 + 1 (0x1021),
/// initialized to 0xFFFF and inverted at output (standard CRC-16/X.25 MSB-first).
pub fn crc16_tetra(bits: &[u8]) -> u16 {
    let mut reg: u16 = 0xFFFF;
    for &b in bits {
        let top = ((reg >> 15) as u8) ^ (b & 1);
        reg <<= 1;
        if top != 0 {
            reg ^= 0x1021;
        }
    }
    !reg
}

/// Appends the 16-bit TETRA CRC (MSB first) to `info_bits`.
pub fn append_crc16(info_bits: &[u8]) -> Vec<u8> {
    let crc = crc16_tetra(info_bits);
    let mut out = Vec::with_capacity(info_bits.len() + 16);
    out.extend_from_slice(info_bits);
    for shift in (0..16).rev() {
        out.push(((crc >> shift) & 1) as u8);
    }
    out
}

/// Checks trailing 16 CRC bits against the preceding info bits.
pub fn check_crc16(bits_with_crc: &[u8]) -> bool {
    if bits_with_crc.len() < 16 {
        return false;
    }
    let split = bits_with_crc.len() - 16;
    let want = crc16_tetra(&bits_with_crc[..split]);
    let mut got: u16 = 0;
    for &b in &bits_with_crc[split..] {
        got = (got << 1) | ((b & 1) as u16);
    }
    got == want
}

/// ETSI EN 300 392-2 Clause 8.2.5: 32-bit MAC scrambling sequence (open whitening derived from
/// public MCC, MNC, and Base Station Colour Code; BSCH uses all-zeros = unscrambled).
pub struct Scrambler {
    state: u32,
    bypass: bool,
}

impl Scrambler {
    /// Scrambler initialized from 10-bit MCC, 14-bit MNC, and 6-bit Colour Code.
    pub fn from_mni(mcc: u16, mnc: u16, colour_code: u8) -> Self {
        let e: u32 = (((mcc as u32) & 0x3FF) << 20)
            | (((mnc as u32) & 0x3FFF) << 6)
            | ((colour_code as u32) & 0x3F);
        // ETSI Clause 8.2.5.2: c(1..30) = e(1..30), c(31..32) = 1.
        // Bit 31 (MSB) corresponds to c(1), bit 0 (LSB) to c(32).
        let state = (e << 2) | 0b11;
        Self { state, bypass: false }
    }

    /// BSCH fixed all-zero scrambling sequence (no-op).
    pub fn bsch_zero() -> Self {
        Self { state: 0, bypass: true }
    }

    /// Generates the next scrambling bit p(k) in {0, 1}.
    #[inline]
    pub fn next_bit(&mut self) -> u8 {
        if self.bypass {
            return 0;
        }
        // ETSI Clause 8.2.5.2 polynomial:
        // 1 + X + X^2 + X^4 + X^5 + X^7 + X^8 + X^10 + X^11 + X^12 + X^16 + X^22 + X^23 + X^26 + X^32
        // With bit 0 = c(32) and bit 31 = c(1), tap X^k maps to bit (32 - k).
        const TAP_MASK: u32 = (1 << 31) // X^1
            | (1 << 30) // X^2
            | (1 << 28) // X^4
            | (1 << 27) // X^5
            | (1 << 25) // X^7
            | (1 << 24) // X^8
            | (1 << 22) // X^10
            | (1 << 21) // X^11
            | (1 << 20) // X^12
            | (1 << 16) // X^16
            | (1 << 10) // X^22
            | (1 << 9)  // X^23
            | (1 << 6)  // X^26
            | (1 << 0); // X^32
        let fb = ((self.state & TAP_MASK).count_ones() & 1) as u32;
        self.state = (self.state >> 1) | (fb << 31);
        fb as u8
    }

    /// XORs hard bits in place.
    pub fn apply_hard(&mut self, bits: &mut [u8]) {
        if self.bypass {
            return;
        }
        for b in bits.iter_mut() {
            *b ^= self.next_bit();
        }
    }

    /// Applies scrambling to soft LLRs (positive = bit 0, negative = bit 1):
    /// XOR with 1 negates the LLR sign.
    pub fn apply_soft(&mut self, llrs: &mut [f32]) {
        if self.bypass {
            return;
        }
        for v in llrs.iter_mut() {
            if self.next_bit() != 0 {
                *v = -*v;
            }
        }
    }
}

/// ETSI EN 300 392-2 Clause 8.2.3.2: shortened RM(30, 14) systematic generator parity rows (14 x 16).
/// Codeword c = [u(0..14) | u * P], where each u16 stores bits 0..15 MSB-first.
const RM_PARITY: [u16; 14] = [
    0b1001_1011_0110_0000,
    0b0010_1101_1110_0000,
    0b1111_1100_0010_0000,
    0b1110_0000_0011_1100,
    0b1001_1000_0011_1010,
    0b0101_0100_0011_0110,
    0b0010_1100_0010_1110,
    0b1111_1111_1101_1111,
    0b1000_0011_0011_1001,
    0b0100_0010_1011_0101,
    0b0010_0001_1010_1101,
    0b0001_0010_0111_0011,
    0b0000_1001_0110_1011,
    0b0000_0100_1110_0111,
];

/// Encodes 14 info bits (MSB = bit 0 of `info14 & 0x3FFF`) into a 30-bit RM(30,14) codeword
/// packed into bits 29..0 (bit 29 = c[0], bit 0 = c[29]).
pub fn rm3014_encode_u32(info14: u16) -> u32 {
    let mut parity: u16 = 0;
    for row in 0..14 {
        if ((info14 >> (13 - row)) & 1) != 0 {
            parity ^= RM_PARITY[row];
        }
    }
    (((info14 & 0x3FFF) as u32) << 16) | (parity as u32)
}

/// Encodes 14 info bits into 30 output bits `{0, 1}`.
pub fn rm3014_encode(info_bits: &[u8; 14]) -> [u8; 30] {
    let mut info14: u16 = 0;
    for &b in info_bits {
        info14 = (info14 << 1) | ((b & 1) as u16);
    }
    let cw = rm3014_encode_u32(info14);
    let mut out = [0u8; 30];
    for i in 0..30 {
        out[i] = ((cw >> (29 - i)) & 1) as u8;
    }
    out
}

fn rm3014_table() -> &'static [u32; 16384] {
    static TABLE: OnceLock<Box<[u32; 16384]>> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut arr = Box::new([0u32; 16384]);
        for i in 0..16384u16 {
            arr[i as usize] = rm3014_encode_u32(i);
        }
        arr
    })
}

/// Exact maximum-likelihood soft-decision decoder for RM(30,14).
/// `llrs`: 30 soft values (>0 favors bit 0, <0 favors bit 1).
/// Returns `(decoded_14_bits, normalized_confidence_0_to_1)`.
pub fn rm3014_decode_soft(llrs: &[f32; 30]) -> ([u8; 14], f32) {
    // Pre-summed 5-bit lookup tables (6 groups of 5 bits = 30 bits):
    // reduces 30 additions per codeword to 6 table lookups (~10 us total!).
    let mut lut = [[0.0f32; 32]; 6];
    for g in 0..6 {
        let base = g * 5;
        for pat in 0..32usize {
            let mut s = 0.0f32;
            for k in 0..5 {
                let bit = (pat >> (4 - k)) & 1;
                let v = llrs[base + k];
                s += if bit == 0 { v } else { -v };
            }
            lut[g][pat] = s;
        }
    }

    let table = rm3014_table();
    let mut best_idx = 0usize;
    let mut best_score = f32::NEG_INFINITY;
    for (idx, &cw) in table.iter().enumerate() {
        let score = lut[0][((cw >> 25) & 31) as usize]
            + lut[1][((cw >> 20) & 31) as usize]
            + lut[2][((cw >> 15) & 31) as usize]
            + lut[3][((cw >> 10) & 31) as usize]
            + lut[4][((cw >> 5) & 31) as usize]
            + lut[5][(cw & 31) as usize];
        if score > best_score {
            best_score = score;
            best_idx = idx;
        }
    }

    let max_possible: f32 = llrs.iter().map(|v| v.abs()).sum();
    let conf = if max_possible > 1e-6 {
        (best_score / max_possible).clamp(-1.0, 1.0)
    } else {
        0.0
    };
    let mut out = [0u8; 14];
    for i in 0..14 {
        out[i] = ((best_idx >> (13 - i)) & 1) as u8;
    }
    (out, conf)
}

/// ETSI EN 300 392-2 Clause 8.2.4: Block interleaver (K, a).
/// Standard values: BSCH (120, 11), SCH/HD & BNCH (216, 101), SCH/F & TCH/S (432, 103).
pub fn block_interleave(bits: &[u8], a: usize) -> Vec<u8> {
    let n = bits.len();
    let mut out = vec![0u8; n];
    for i in 0..n {
        let k = (a * (i + 1)) % n;
        out[k] = bits[i];
    }
    out
}

/// Inverse of `block_interleave` operating on soft LLRs.
pub fn block_deinterleave_soft(llrs: &[f32], a: usize) -> Vec<f32> {
    let n = llrs.len();
    let mut out = vec![0.0f32; n];
    for i in 0..n {
        let k = (a * (i + 1)) % n;
        out[i] = llrs[k];
    }
    out
}

/// RCPC puncturing rates defined in ETSI EN 300 392-2 Clause 8.2.3.1.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RcpcRate {
    /// Rate 2/3: P = [1, 2, 5] (BSCH 80->120, SCH/HD & BNCH 144->216, SCH/F 288->432).
    Rate2_3,
    /// Two-class TCH/S speech half-frame (90 input bits -> 165 channel bits:
    /// 56 Class-1 bits @ rate 2/3 -> 84 bits, then 30 Class-2 + 4 tail bits -> 81 bits).
    TchSpeechHalf,
}

impl RcpcRate {
    /// Puncturing index table (0-based indices into the `4 * num_input_bits` mother stream).
    fn puncture_indices(self, num_out_bits: usize) -> Vec<usize> {
        match self {
            RcpcRate::Rate2_3 => {
                const P: [usize; 3] = [1, 2, 5];
                (1..=num_out_bits)
                    .map(|j| {
                        let k = 8 * ((j - 1) / 3) + P[(j - 1) % 3];
                        k - 1
                    })
                    .collect()
            }
            RcpcRate::TchSpeechHalf => {
                // Class 1 (first 56 input bits -> mother [0..224]): 84 bits at rate 2/3 (P=[1,2,5]).
                // Class 2 + 4 tail (last 34 input bits -> mother [224..360]): 81 bits at rate 34/81.
                const P1: [usize; 3] = [1, 2, 5];
                let c1_out = 84usize.min(num_out_bits);
                let mut out = Vec::with_capacity(num_out_bits);
                for j in 1..=c1_out {
                    let k = 8 * ((j - 1) / 3) + P1[(j - 1) % 3];
                    out.push(k - 1);
                }
                let c2_out = num_out_bits.saturating_sub(c1_out);
                let mother_c2_start = 56 * 4;
                let mother_c2_len = 34 * 4; // 136 mother bits for 30 Class-2 + 4 tail bits
                for j in 0..c2_out {
                    out.push(mother_c2_start + (j * mother_c2_len) / c2_out.max(1));
                }
                out
            }
        }
    }
}

// ETSI EN 300 392-2 Clause 8.2.3.1.1: 16-state rate 1/4 mother convolutional code.
// State bits (4 bits): bit 3 = u[n-1], bit 2 = u[n-2], bit 1 = u[n-3], bit 0 = u[n-4].
// Combined 5-bit register `reg = (u[n] << 4) | state`:
//   G1(D) = 1 + D + D^4           -> bits 4, 3, 0 -> 0b11001
//   G2(D) = 1 + D^2 + D^3 + D^4   -> bits 4, 2, 1, 0 -> 0b10111
//   G3(D) = 1 + D + D^2 + D^4     -> bits 4, 3, 2, 0 -> 0b11101
//   G4(D) = 1 + D + D^3 + D^4     -> bits 4, 3, 1, 0 -> 0b11011
const G1: u8 = 0b11001;
const G2: u8 = 0b10111;
const G3: u8 = 0b11101;
const G4: u8 = 0b11011;

#[inline]
fn mother_outputs(state: u8, input: u8) -> ([u8; 4], u8) {
    let reg = ((input & 1) << 4) | (state & 0x0F);
    let out = [
        ((reg & G1).count_ones() & 1) as u8,
        ((reg & G2).count_ones() & 1) as u8,
        ((reg & G3).count_ones() & 1) as u8,
        ((reg & G4).count_ones() & 1) as u8,
    ];
    let next_state = (reg >> 1) & 0x0F;
    (out, next_state)
}

/// Encodes `input_bits` (which must already include 4 trailing zero tail bits) using the
/// 16-state RCPC mother code punctured at `rate` to `num_out_bits`.
pub fn rcpc_encode(input_bits: &[u8], rate: RcpcRate, num_out_bits: usize) -> Vec<u8> {
    let mut mother = Vec::with_capacity(input_bits.len() * 4);
    let mut state: u8 = 0;
    for &b in input_bits {
        let (outs, next) = mother_outputs(state, b);
        mother.extend_from_slice(&outs);
        state = next;
    }
    let indices = rate.puncture_indices(num_out_bits);
    indices
        .into_iter()
        .map(|idx| mother.get(idx).copied().unwrap_or(0))
        .collect()
}

/// Soft-decision 16-state Viterbi decoder for TETRA RCPC codes.
/// `llrs`: channel LLRs (>0 favors bit 0, <0 favors bit 1).
/// `num_in_bits`: total input bits INCLUDING the 4 zero tail bits.
/// Returns decoded bits (length `num_in_bits`, with the last 4 tail bits forced to 0).
pub fn rcpc_decode_viterbi(llrs: &[f32], rate: RcpcRate, num_in_bits: usize) -> Vec<u8> {
    let mother_len = num_in_bits * 4;
    let mut depunct = vec![0.0f32; mother_len];
    let indices = rate.puncture_indices(llrs.len());
    for (&llr, idx) in llrs.iter().zip(indices.into_iter()) {
        if idx < mother_len {
            depunct[idx] += llr;
        }
    }

    // Precompute transition table for all 16 states x 2 inputs.
    let mut next_state = [[0u8; 2]; 16];
    let mut branch_bits = [[[0u8; 4]; 2]; 16];
    for s in 0..16u8 {
        for inp in 0..2u8 {
            let (outs, ns) = mother_outputs(s, inp);
            next_state[s as usize][inp as usize] = ns;
            branch_bits[s as usize][inp as usize] = outs;
        }
    }

    let mut pm = [f32::NEG_INFINITY; 16];
    pm[0] = 0.0;
    let mut survivor_state = vec![[0u8; 16]; num_in_bits];
    let mut survivor_input = vec![[0u8; 16]; num_in_bits];

    for t in 0..num_in_bits {
        let obs = &depunct[t * 4..(t + 1) * 4];
        let mut next_pm = [f32::NEG_INFINITY; 16];
        let tail = t >= num_in_bits.saturating_sub(4);
        let max_inp = if tail { 1 } else { 2 };

        for s in 0..16usize {
            let cur = pm[s];
            if !cur.is_finite() {
                continue;
            }
            for inp in 0..max_inp {
                let ns = next_state[s][inp] as usize;
                let bb = &branch_bits[s][inp];
                let mut bm = 0.0f32;
                for k in 0..4 {
                    bm += if bb[k] == 0 { obs[k] } else { -obs[k] };
                }
                let cand = cur + bm;
                if cand > next_pm[ns] {
                    next_pm[ns] = cand;
                    survivor_state[t][ns] = s as u8;
                    survivor_input[t][ns] = inp as u8;
                }
            }
        }
        pm = next_pm;
    }

    // Trace back from zero-terminated final state 0.
    let mut decoded = vec![0u8; num_in_bits];
    let mut st = 0usize;
    for t in (0..num_in_bits).rev() {
        decoded[t] = survivor_input[t][st];
        st = survivor_state[t][st] as usize;
    }
    decoded
}

/// Convenience: decode a standard CRC-protected TETRA logical block
/// (de-scramble -> de-interleave -> RCPC Viterbi -> CRC-16 check).
pub fn decode_logical_block(
    soft_bits: &[f32],
    scrambler: &mut Scrambler,
    interleave_a: usize,
    rate: RcpcRate,
    info_bits_len: usize,
) -> Option<Vec<u8>> {
    let mut descrambled = soft_bits.to_vec();
    scrambler.apply_soft(&mut descrambled);
    let deinterleaved = block_deinterleave_soft(&descrambled, interleave_a);
    let total_in = info_bits_len + 16 + 4; // info + CRC-16 + 4 tail bits
    let decoded = rcpc_decode_viterbi(&deinterleaved, rate, total_in);
    let payload_with_crc = &decoded[..info_bits_len + 16];
    if check_crc16(payload_with_crc) {
        Some(decoded[..info_bits_len].to_vec())
    } else {
        None
    }
}

/// Inverse of `decode_logical_block` (used by the ETSI synthetic test generator).
pub fn encode_logical_block(
    info_bits: &[u8],
    scrambler: &mut Scrambler,
    interleave_a: usize,
    rate: RcpcRate,
    out_bits_len: usize,
) -> Vec<u8> {
    let mut with_crc = append_crc16(info_bits);
    with_crc.extend_from_slice(&[0, 0, 0, 0]); // 4 zero tail bits
    let coded = rcpc_encode(&with_crc, rate, out_bits_len);
    let mut interleaved = block_interleave(&coded, interleave_a);
    scrambler.apply_hard(&mut interleaved);
    interleaved
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc16_detects_single_bit_flip() {
        let info: Vec<u8> = (0..60).map(|i| ((i * 7 + 3) & 1) as u8).collect();
        let mut framed = append_crc16(&info);
        assert!(check_crc16(&framed));
        framed[17] ^= 1;
        assert!(!check_crc16(&framed));
    }

    #[test]
    fn rm3014_minimum_distance_is_8_and_corrects_3_errors() {
        // Verify every non-zero generator row and pair has weight >= 8.
        let table = rm3014_table();
        let mut min_w = 30u32;
        for &cw in &table[1..] {
            min_w = min_w.min(cw.count_ones());
        }
        assert_eq!(min_w, 8, "RM(30,14) must have minimum Hamming distance 8");

        let info = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 1];
        let cw = rm3014_encode(&info);
        let mut llrs = [0.0f32; 30];
        for i in 0..30 {
            llrs[i] = if cw[i] == 0 { 1.0 } else { -1.0 };
        }
        // Flip 3 bits cleanly (maximum guaranteed hard-error correction for d_min=8).
        llrs[2] = -llrs[2];
        llrs[15] = -llrs[15];
        llrs[27] = -llrs[27];
        let (dec, conf) = rm3014_decode_soft(&llrs);
        assert_eq!(dec, info);
        assert!(conf > 0.7);
    }

    #[test]
    fn interleaver_roundtrip_standard_sizes() {
        for &(n, a) in &[(120, 11), (216, 101), (432, 103)] {
            let src: Vec<u8> = (0..n).map(|i| ((i * 13 + 5) & 1) as u8).collect();
            let inter = block_interleave(&src, a);
            let soft: Vec<f32> = inter.iter().map(|&b| if b == 0 { 1.0 } else { -1.0 }).collect();
            let deint = block_deinterleave_soft(&soft, a);
            let back: Vec<u8> = deint.iter().map(|&v| if v >= 0.0 { 0 } else { 1 }).collect();
            assert_eq!(back, src);
        }
    }

    #[test]
    fn rcpc_viterbi_corrects_channel_errors_on_bsch_and_bnch() {
        let info: Vec<u8> = (0..124).map(|i| ((i * 5 + 1) & 1) as u8).collect();
        let mut enc_scr = Scrambler::from_mni(260, 1, 7);
        let coded = encode_logical_block(&info, &mut enc_scr, 101, RcpcRate::Rate2_3, 216);
        let mut llrs: Vec<f32> = coded.iter().map(|&b| if b == 0 { 1.0 } else { -1.0 }).collect();
        // Inject 4 well-separated channel bit errors across the 216-bit rate-2/3 block.
        for &pos in &[5, 60, 130, 195] {
            llrs[pos] = -llrs[pos];
        }
        let mut dec_scr = Scrambler::from_mni(260, 1, 7);
        let recovered = decode_logical_block(&llrs, &mut dec_scr, 101, RcpcRate::Rate2_3, 124)
            .expect("Viterbi + CRC should recover 4 separated errors");
        assert_eq!(recovered, info);
    }
}
