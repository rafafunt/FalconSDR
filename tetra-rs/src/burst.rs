//! TETRA Downlink Burst Synchronizer (ETSI EN 300 392-2 Clause 9.4.4).
//!
//! Continuous downlink timeslot = 255 symbols = 510 bits (14.167 ms):
//! - Synchronization Continuous Downlink Burst (SB):
//!     [0..14] start | [14..94] FC (80) | [94..214] BSCH (120) | [214..252] STS (38)
//!     | [252..282] BBK/AACH (30) | [282..498] BKN2 (216) | [498..510] end
//! - Normal Continuous Downlink Burst (NDB):
//!     [0..14] start | [14..230] BKN1 (216) | [230..244] BBK1 (14) | [244..266] NTS (22)
//!     | [266..282] BBK2 (16) | [282..498] BKN2 (216) | [498..510] end

pub const SLOT_BITS: usize = 510;

/// Normal Training Sequence 1 (22 bits, ETSI EN 300 392-2 Clause 9.4.4.3.2):
/// indicates full-slot channel (TCH or SCH/F across BKN1 + BKN2).
pub const NTS1: [u8; 22] = [1, 1, 0, 1, 0, 0, 0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1, 1, 0, 1, 0, 0];

/// Normal Training Sequence 2 (22 bits, Clause 9.4.4.3.2):
/// indicates split half-slot channels (two independent 216-bit blocks, e.g. STCH/SCH/HD/BNCH).
pub const NTS2: [u8; 22] = [0, 1, 1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0];

/// Synchronization Training Sequence (38 bits, Clause 9.4.4.3.2):
/// marks a Synchronization Burst (SB) carrying BSCH + AACH + BKN2 (BNCH/SCH/HD).
pub const STS: [u8; 38] = [
    1, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 1, 1, 0, 0, 1, 1, 1,
    0, 1, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 1, 1,
];

/// Extracted downlink burst ready for Layer 1 channel decoding.
#[derive(Clone, Debug)]
pub enum DownlinkBurst {
    Sync {
        bsch: [f32; 120],
        bbk: [f32; 30],
        bkn2: [f32; 216],
        corr: f32,
    },
    Normal {
        bkn1: [f32; 216],
        bbk: [f32; 30],
        bkn2: [f32; 216],
        split_slot: bool, // true = NTS2, false = NTS1
    },
}

#[inline]
fn correlate(soft: &[f32], seq: &[u8]) -> f32 {
    let mut dot = 0.0f32;
    let mut mag = 0.0f32;
    for (&v, &b) in soft.iter().zip(seq.iter()) {
        dot += if b == 0 { v } else { -v };
        mag += v.abs();
    }
    if mag > 1e-6 { dot / mag } else { 0.0 }
}

/// Sliding-window soft-bit burst synchronizer for continuous TETRA downlinks.
pub struct BurstSync {
    buf: Vec<f32>,
    locked: bool,
    missed: usize,
}

impl BurstSync {
    const TRACK_SLACK: usize = 6; // symmetric ±6-bit search window around expected slot start

    pub fn new() -> Self {
        Self {
            buf: Vec::with_capacity(SLOT_BITS * 4),
            locked: false,
            missed: 0,
        }
    }

    /// Feeds new soft bits and returns any complete synchronized downlink bursts.
    pub fn push(&mut self, soft_bits: &[f32]) -> Vec<DownlinkBurst> {
        self.buf.extend_from_slice(soft_bits);
        let mut out = Vec::new();

        // Thresholds: normalized correlation in [-1, 1].
        const ACQ_STS_THRESH: f32 = 0.72;
        const ACQ_NTS_THRESH: f32 = 0.78;
        const TRACK_THRESH: f32 = 0.52;

        loop {
            if self.buf.len() < SLOT_BITS + 2 * Self::TRACK_SLACK {
                break;
            }

            if !self.locked {
                // Acquisition scan: stop at the FIRST valid burst peak (do not skip earlier
                // bursts in a large chunk just because a later burst has slightly higher score).
                let search_end = self.buf.len() - SLOT_BITS;
                let mut best_slot_start: Option<usize> = None;
                let mut best_kind = 0u8; // 0=STS, 1=NTS1, 2=NTS2
                let mut best_score = 0.0f32;
                let mut peak_limit = search_end;

                for start in 0..=search_end {
                    if start > peak_limit {
                        break;
                    }
                    let s_sts = correlate(&self.buf[start + 214..start + 252], &STS);
                    let s_nts1 = correlate(&self.buf[start + 244..start + 266], &NTS1);
                    let s_nts2 = correlate(&self.buf[start + 244..start + 266], &NTS2);

                    let mut cand: Option<(u8, f32)> = None;
                    if s_sts >= ACQ_STS_THRESH {
                        cand = Some((0, s_sts));
                    }
                    if s_nts1 >= ACQ_NTS_THRESH && cand.map_or(true, |(_, sc)| s_nts1 > sc) {
                        cand = Some((1, s_nts1));
                    }
                    if s_nts2 >= ACQ_NTS_THRESH && cand.map_or(true, |(_, sc)| s_nts2 > sc) {
                        cand = Some((2, s_nts2));
                    }
                    if let Some((kind, score)) = cand {
                        if best_slot_start.is_none() {
                            peak_limit = (start + 16).min(search_end);
                        }
                        if score > best_score {
                            best_score = score;
                            best_kind = kind;
                            best_slot_start = Some(start);
                        }
                    }
                }

                if let Some(start) = best_slot_start {
                    let burst = Self::extract(&self.buf[start..start + SLOT_BITS], best_kind, best_score);
                    out.push(burst);
                    // Retain TRACK_SLACK lookbehind bits so the next slot can search [-TRACK_SLACK..+TRACK_SLACK].
                    let drain_to = (start + SLOT_BITS).saturating_sub(Self::TRACK_SLACK);
                    self.buf.drain(0..drain_to);
                    self.locked = true;
                    self.missed = 0;
                } else {
                    let drop_n = search_end;
                    if drop_n > 0 {
                        self.buf.drain(0..drop_n);
                    }
                    break;
                }
            } else {
                // Tracked mode: expected slot starts at index TRACK_SLACK; search [0..=2*TRACK_SLACK].
                let max_off = (2 * Self::TRACK_SLACK).min(self.buf.len() - SLOT_BITS);
                let mut best_off = Self::TRACK_SLACK.min(max_off);
                let mut best_kind = 1u8;
                let mut best_score = -1.0f32;

                for off in 0..=max_off {
                    let s_sts = correlate(&self.buf[off + 214..off + 252], &STS);
                    if s_sts > best_score {
                        best_score = s_sts;
                        best_kind = 0;
                        best_off = off;
                    }
                    let s_nts1 = correlate(&self.buf[off + 244..off + 266], &NTS1);
                    if s_nts1 > best_score {
                        best_score = s_nts1;
                        best_kind = 1;
                        best_off = off;
                    }
                    let s_nts2 = correlate(&self.buf[off + 244..off + 266], &NTS2);
                    if s_nts2 > best_score {
                        best_score = s_nts2;
                        best_kind = 2;
                        best_off = off;
                    }
                }

                if best_score >= TRACK_THRESH {
                    let burst = Self::extract(&self.buf[best_off..best_off + SLOT_BITS], best_kind, best_score);
                    out.push(burst);
                    let drain_to = (best_off + SLOT_BITS).saturating_sub(Self::TRACK_SLACK);
                    self.buf.drain(0..drain_to);
                    self.missed = 0;
                } else {
                    self.missed += 1;
                    self.buf.drain(0..SLOT_BITS);
                    if self.missed >= 8 {
                        self.locked = false;
                        self.missed = 0;
                    }
                }
            }
        }

        out
    }

    fn extract(slot: &[f32], kind: u8, corr: f32) -> DownlinkBurst {
        if kind == 0 {
            let mut bsch = [0.0f32; 120];
            let mut bbk = [0.0f32; 30];
            let mut bkn2 = [0.0f32; 216];
            bsch.copy_from_slice(&slot[94..214]);
            bbk.copy_from_slice(&slot[252..282]);
            bkn2.copy_from_slice(&slot[282..498]);
            DownlinkBurst::Sync { bsch, bbk, bkn2, corr }
        } else {
            let mut bkn1 = [0.0f32; 216];
            let mut bbk = [0.0f32; 30];
            let mut bkn2 = [0.0f32; 216];
            bkn1.copy_from_slice(&slot[14..230]);
            bbk[..14].copy_from_slice(&slot[230..244]);
            bbk[14..].copy_from_slice(&slot[266..282]);
            bkn2.copy_from_slice(&slot[282..498]);
            DownlinkBurst::Normal {
                bkn1,
                bbk,
                bkn2,
                split_slot: kind == 2,
            }
        }
    }
}

/// Builds a 510-bit Synchronization Burst (SB) for synthetic test generation.
pub fn build_sync_burst(bsch: &[u8], bbk: &[u8; 30], bkn2: &[u8]) -> [u8; SLOT_BITS] {
    let mut slot = [0u8; SLOT_BITS];
    // [14..94]: frequency correction field (ETSI Clause 9.4.4.2.5: 80 bits of all 1s -> constant -3pi/4 shift)
    for b in &mut slot[14..94] {
        *b = 1;
    }
    slot[94..214].copy_from_slice(&bsch[..120]);
    slot[214..252].copy_from_slice(&STS);
    slot[252..282].copy_from_slice(bbk);
    slot[282..498].copy_from_slice(&bkn2[..216]);
    slot
}

/// Builds a 510-bit Normal Downlink Burst (NDB) for synthetic test generation.
pub fn build_normal_burst(bkn1: &[u8], bbk: &[u8; 30], bkn2: &[u8], split_slot: bool) -> [u8; SLOT_BITS] {
    let mut slot = [0u8; SLOT_BITS];
    slot[14..230].copy_from_slice(&bkn1[..216]);
    slot[230..244].copy_from_slice(&bbk[..14]);
    slot[244..266].copy_from_slice(if split_slot { &NTS2 } else { &NTS1 });
    slot[266..282].copy_from_slice(&bbk[14..30]);
    slot[282..498].copy_from_slice(&bkn2[..216]);
    slot
}
