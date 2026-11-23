//! Synthetic ETSI EN 300 392-2 TETRA downlink IQ generator for offline/CI validation.
//!
//! Produces realistic π/4-DQPSK RRC-shaped complex baseband IQ (with configurable CFO
//! and AWGN noise) carrying SYNC, SYSINFO, clear D-SETUP call, encrypted call, SDS text,
//! and LIP GPS location bursts.

use std::f32::consts::PI;

use crate::burst::{build_normal_burst, build_sync_burst};
use crate::coding::{encode_logical_block, rm3014_encode, RcpcRate, Scrambler};
use crate::mac::{Aach, ChannelAlloc, MacResource, SyncPdu, SysinfoPdu};
use crate::phy::{dibit_to_dphi, rrc_taps, C32, DEFAULT_IQ_RATE, RRC_ALPHA};
use crate::upper::{encode_d_sds_lip, encode_d_sds_text, encode_d_setup};

/// Simple deterministic xorshift64 + Box-Muller Gaussian noise generator (zero deps).
struct Rng(u64);
impl Rng {
    fn next_f32(&mut self) -> f32 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        ((x >> 40) as f32 + 1.0) / ((1u32 << 24) as f32 + 2.0)
    }

    fn gauss(&mut self, sigma: f32) -> (f32, f32) {
        let u1 = self.next_f32().max(1e-7);
        let u2 = self.next_f32();
        let r = (-2.0 * u1.ln()).sqrt() * sigma;
        let th = 2.0 * PI * u2;
        (r * th.cos(), r * th.sin())
    }
}

/// Modulates a bitstream into π/4-DQPSK RRC-filtered IQ at 72 kS/s (4 sps) with CFO and AWGN.
pub fn modulate_tetra_bits(bits: &[u8], cfo_hz: f32, snr_db: f32, seed: u64) -> Vec<C32> {
    let sps = 4usize;
    let taps = rrc_taps(sps as f32, RRC_ALPHA, 10);
    let mut syms = Vec::with_capacity(bits.len() / 2 + 1);
    let mut phase = 0.25f32;
    syms.push(C32::from_polar(1.0, phase));
    for ch in bits.chunks_exact(2) {
        phase += dibit_to_dphi(ch[0], ch[1]);
        syms.push(C32::from_polar(1.0, phase));
    }

    let mut up = vec![C32::default(); syms.len() * sps + taps.len()];
    for (i, &s) in syms.iter().enumerate() {
        up[i * sps] = s.scale(sps as f32);
    }
    let mut iq = vec![C32::default(); up.len()];
    for i in 0..up.len() {
        let mut acc = C32::default();
        for (k, &h) in taps.iter().enumerate() {
            if i >= k {
                acc.re += up[i - k].re * h;
                acc.im += up[i - k].im * h;
            }
        }
        iq[i] = acc;
    }

    let sig_pow: f32 = iq.iter().map(|s| s.norm_sq()).sum::<f32>() / (iq.len().max(1) as f32);
    let noise_pow = sig_pow / 10.0f32.powf(snr_db / 10.0);
    let sigma = (noise_pow * 0.5).sqrt();
    let mut rng = Rng(seed | 1);

    for (n, s) in iq.iter_mut().enumerate() {
        let th = 2.0 * PI * cfo_hz * (n as f32) / DEFAULT_IQ_RATE;
        let rot = s.mul(C32::from_polar(1.0, th));
        let (nr, ni) = rng.gauss(sigma);
        *s = C32::new(rot.re + nr, rot.im + ni);
    }
    iq
}

/// Generates a complete synthetic multi-burst TETRA downlink IQ stream exercising:
/// - preamble (loop convergence)
/// - SB with SYNC (MCC=260, MNC=42, CC=7) + BNCH SYSINFO (LA=101, AIE=false)
/// - NDB (SCH/F) with clear D-SETUP voice call (GSSI=1001, ISSI=2601111)
/// - NDB (SCH/F) with encrypted MAC-RESOURCE (GSSI=9999, encryption_mode=1)
/// - NDB (SCH/F) with SDS text ("TEST CLEAR SDS")
/// - NDB (SCH/F) with LIP GPS location (50.0619, 19.9368)
pub fn generate_test_iq(cfo_hz: f32, snr_db: f32) -> Vec<C32> {
    let mcc = 260u16;
    let mnc = 42u16;
    let cc = 7u8;

    let mut all_bits: Vec<u8> = Vec::with_capacity(510 * 14);
    // 2 pseudo-random preamble slots for Gardner + AFC convergence before first burst.
    let mut prng: u32 = 0xACE1_BEEF;
    for _ in 0..(510 * 2) {
        prng ^= prng << 13;
        prng ^= prng >> 17;
        prng ^= prng << 5;
        all_bits.push((prng & 1) as u8);
    }

    let make_bbk = |dl_raw: u8| -> [u8; 30] {
        let aach = Aach::encode(1, dl_raw, 0);
        let mut cw = rm3014_encode(&aach);
        let mut scr = Scrambler::from_mni(mcc, mnc, cc);
        scr.apply_hard(&mut cw);
        cw
    };

    // 1. Synchronization Burst (BSCH SYNC + BNCH SYSINFO)
    let sync = SyncPdu {
        system_code: 0,
        colour_code: cc,
        timeslot: 1,
        frame: 18,
        multiframe: 1,
        sharing_mode: 0,
        ts_reserved: 0,
        u_plane_dtx: false,
        frame_18_ext: false,
        mcc,
        mnc,
        cell_service_level: 1,
        late_entry: false,
    };
    let mut bsch_scr = Scrambler::bsch_zero();
    let bsch_coded = encode_logical_block(&sync.encode(), &mut bsch_scr, 11, RcpcRate::Rate2_3, 120);

    let sys = SysinfoPdu {
        main_carrier: 3696,
        freq_band: 3,
        offset_hz: 12500,
        duplex_spacing: 2,
        reverse_op: false,
        location_area: 101,
        voice_service: true,
        circuit_data: false,
        air_interface_encryption: false,
    };
    let mut bnch_scr = Scrambler::from_mni(mcc, mnc, cc);
    let bnch_coded = encode_logical_block(&sys.encode(), &mut bnch_scr, 101, RcpcRate::Rate2_3, 216);
    let sb = build_sync_burst(&bsch_coded, &make_bbk(2), &bnch_coded);
    all_bits.extend_from_slice(&sb);

    // Helper: push a full-slot SCH/F Normal Downlink Burst carrying a 268-bit MAC PDU.
    let mut push_schf = |pdu_bits: &[u8]| {
        let mut scr = Scrambler::from_mni(mcc, mnc, cc);
        let coded = encode_logical_block(pdu_bits, &mut scr, 103, RcpcRate::Rate2_3, 432);
        let ndb = build_normal_burst(&coded[..216], &make_bbk(2), &coded[216..432], false);
        all_bits.extend_from_slice(&ndb);
    };

    // 2. Clear voice D-SETUP (GSSI 1001, ISSI 2601111, assigned to TS2)
    let setup_sdu = encode_d_setup(512, 2601111, true, false);
    let setup_mac = MacResource::encode(
        0,
        1001,
        Some(ChannelAlloc { alloc_type: 0, timeslot_mask: 0b0100, carrier: None }),
        &setup_sdu,
        268,
    );
    push_schf(&setup_mac);

    // 3. Encrypted call (encryption_mode = 1, GSSI 9999 -> must be flagged encrypted & muted)
    let enc_mac = MacResource::encode(1, 9999, None, &[0u8; 32], 268);
    push_schf(&enc_mac);

    // 4. Clear SDS text message
    let sds_sdu = encode_d_sds_text(2602222, "TEST CLEAR SDS");
    let sds_mac = MacResource::encode(0, 1001, None, &sds_sdu, 268);
    push_schf(&sds_mac);

    // 5. Clear LIP GPS location (Krakow 50.0619, 19.9368)
    let lip_sdu = encode_d_sds_lip(2603333, 50.0619, 19.9368);
    let lip_mac = MacResource::encode(0, 1001, None, &lip_sdu, 268);
    push_schf(&lip_mac);

    // Trailing dummy slot to flush the burst synchronizer cleanly.
    for i in 0..510 {
        all_bits.push(((i * 11 + 1) & 1) as u8);
    }

    modulate_tetra_bits(&all_bits, cfo_hz, snr_db, 0xF00D_CAFE_1234_5678)
}
