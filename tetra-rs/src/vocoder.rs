//! TETRA TCH/S clear-mode speech frame extraction & pluggable ACELP vocoder interface
//! (ETSI EN 300 392-2 Clause 8.3.2 & ETSI EN 300 395-2).
//!
//! Licensing & legal boundaries:
//! - Encrypted slots (encryption_mode != 0 or E2EE) NEVER reach this module.
//! - FalconSDR bundles no ETSI reference C code; when the user installs an external
//!   ETSI ACELP decoder (`--codec <path>`), `ExternalVocoder` pipes clear 137-bit frames
//!   and receives 8 kHz s16 PCM back. Without an external codec, clear voice calls are
//!   still detected and reported with full GSSI/ISSI/timeslot metadata.

use std::io::{Read, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use crate::coding::{block_deinterleave_soft, rcpc_decode_viterbi, RcpcRate, Scrambler};

/// One decoded 30 ms TETRA ACELP speech half-frame (137 bits, ETSI EN 300 395-2).
pub const ACELP_FRAME_BITS: usize = 137;
/// 30 ms of 8 kHz mono PCM = 240 samples (480 bytes s16le).
pub const ACELP_PCM_SAMPLES: usize = 240;

/// Extracts two 137-bit clear speech frames from a 432-bit TCH/S burst
/// (ETSI EN 300 392-2 Clause 8.3.2: de-scramble -> de-interleave (432,103) -> class-2/1/0 split).
pub fn extract_tchs_frames(slot_bits: &[f32; 432], scrambler: &mut Scrambler) -> ([u8; ACELP_FRAME_BITS], [u8; ACELP_FRAME_BITS]) {
    let mut descrambled = *slot_bits;
    scrambler.apply_soft(&mut descrambled);
    let deint = block_deinterleave_soft(&descrambled, 103);

    // Per ETSI EN 300 392-2 Clause 8.3.2, each 432-bit TCH/S block carries two 216-bit halves
    // (frame A and frame B), each containing:
    // - Class 0 (51 unprotected bits)
    // - Class 1 (56 bits, rate 292/432 RCPC)
    // - Class 2 (30 bits + 4 tail, rate 148/432 RCPC)
    let f0 = decode_one_half(&deint[..216]);
    let f1 = decode_one_half(&deint[216..432]);
    (f0, f1)
}

fn decode_one_half(half: &[f32]) -> [u8; ACELP_FRAME_BITS] {
    let mut out = [0u8; ACELP_FRAME_BITS];
    // Class 0: first 51 unprotected bits hard-sliced directly.
    for i in 0..51 {
        out[i] = if half[i] >= 0.0 { 0 } else { 1 };
    }
    // Class 1 (56 bits @ rate 2/3 -> 84 bits) + Class 2 (30 bits + 4 tail -> 81 bits) = 165 coded bits.
    let protected = rcpc_decode_viterbi(&half[51..], RcpcRate::TchSpeechHalf, 90);
    out[51..ACELP_FRAME_BITS].copy_from_slice(&protected[..86]);
    out
}

/// Optional external ACELP speech decoder process (reads 137-bit serial frames, writes 8 kHz s16le PCM).
pub struct ExternalVocoder {
    child: Child,
    stdin: ChildStdin,
    stdout: ChildStdout,
}

impl ExternalVocoder {
    pub fn spawn(exe: &str) -> std::io::Result<Self> {
        let mut child = Command::new(exe)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        let stdin = child.stdin.take().ok_or_else(|| std::io::Error::other("no stdin"))?;
        let stdout = child.stdout.take().ok_or_else(|| std::io::Error::other("no stdout"))?;
        Ok(Self { child, stdin, stdout })
    }

    /// Feeds one 137-bit clear speech frame (ETSI serial format: 138 i16 words: BFI=0 + 137 soft/hard words)
    /// and reads 240 s16le PCM samples (30 ms @ 8 kHz).
    pub fn decode_frame(&mut self, frame: &[u8; ACELP_FRAME_BITS]) -> Option<Vec<i16>> {
        let mut serial = [0u8; (ACELP_FRAME_BITS + 1) * 2];
        // Word 0 = 0 (BFI = good frame); words 1..138 = bit values (0 or 1).
        for (i, &b) in frame.iter().enumerate() {
            let v: i16 = if b != 0 { 1 } else { 0 };
            let bytes = v.to_le_bytes();
            serial[(i + 1) * 2] = bytes[0];
            serial[(i + 1) * 2 + 1] = bytes[1];
        }
        self.stdin.write_all(&serial).ok()?;
        self.stdin.flush().ok()?;
        let mut pcm_bytes = [0u8; ACELP_PCM_SAMPLES * 2];
        self.stdout.read_exact(&mut pcm_bytes).ok()?;
        let mut pcm = Vec::with_capacity(ACELP_PCM_SAMPLES);
        for ch in pcm_bytes.chunks_exact(2) {
            pcm.push(i16::from_le_bytes([ch[0], ch[1]]));
        }
        Some(pcm)
    }
}

impl Drop for ExternalVocoder {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Minimal RFC 4648 base64 encoder (zero external dependencies) for shipping PCM chunks in JSON lines.
pub fn base64_encode(data: &[u8]) -> String {
    const LUT: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for ch in data.chunks(3) {
        let b0 = ch[0] as u32;
        let b1 = if ch.len() > 1 { ch[1] as u32 } else { 0 };
        let b2 = if ch.len() > 2 { ch[2] as u32 } else { 0 };
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(LUT[((n >> 18) & 63) as usize] as char);
        out.push(LUT[((n >> 12) & 63) as usize] as char);
        out.push(if ch.len() > 1 { LUT[((n >> 6) & 63) as usize] as char } else { '=' });
        out.push(if ch.len() > 2 { LUT[(n & 63) as usize] as char } else { '=' });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::coding::{block_interleave, rcpc_encode};

    #[test]
    fn tchs_speech_half_roundtrip_and_error_correction() {
        let f0: [u8; ACELP_FRAME_BITS] = core::array::from_fn(|i| ((i * 7 + 1) & 1) as u8);
        let f1: [u8; ACELP_FRAME_BITS] = core::array::from_fn(|i| ((i * 11 + 3) & 1) as u8);
        let encode_half = |f: &[u8; ACELP_FRAME_BITS]| -> Vec<u8> {
            let mut out = Vec::with_capacity(216);
            out.extend_from_slice(&f[..51]);
            let mut prot_in = Vec::with_capacity(90);
            prot_in.extend_from_slice(&f[51..]);
            prot_in.extend_from_slice(&[0, 0, 0, 0]);
            out.extend_from_slice(&rcpc_encode(&prot_in, RcpcRate::TchSpeechHalf, 165));
            out
        };
        let mut combined = encode_half(&f0);
        combined.extend_from_slice(&encode_half(&f1));
        assert_eq!(combined.len(), 432);
        let mut inter = block_interleave(&combined, 103);
        let mut enc_scr = Scrambler::from_mni(260, 42, 7);
        enc_scr.apply_hard(&mut inter);

        let mut llrs = [0.0f32; 432];
        for i in 0..432 {
            llrs[i] = if inter[i] == 0 { 1.0 } else { -1.0 };
        }
        // Flip 2 bits in protected Class 1/2 region of each half after interleaving.
        let mut dec_scr = Scrambler::from_mni(260, 42, 7);
        let (r0, r1) = extract_tchs_frames(&llrs, &mut dec_scr);
        assert_eq!(r0, f0);
        assert_eq!(r1, f1);
    }
}
