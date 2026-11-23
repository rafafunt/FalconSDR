//! Stateful TETRA downlink receiver tying PHY -> BurstSync -> Coding -> MAC -> Upper -> Vocoder.
//!
//! Emits clean, newline-delimited JSON events to FalconSDR (or CLI stdout).
//! Strictly clear-mode: encrypted slots are flagged and muted, never decrypted.

use crate::burst::{BurstSync, DownlinkBurst};
use crate::coding::{decode_logical_block, rm3014_decode_soft, RcpcRate, Scrambler};
use crate::mac::{Aach, DownlinkUsage, MacResource, SyncPdu, SysinfoPdu};
use crate::phy::{C32, Pi4DqpskDemod};
use crate::upper::{parse_tm_sdu, UpperEvent};
use crate::vocoder::{base64_encode, extract_tchs_frames, ExternalVocoder};

#[derive(Clone, Debug, Default)]
struct SlotState {
    gssi: Option<u32>,
    issi: Option<u32>,
    call_id: Option<u16>,
    encrypted: bool,
    active_voice: bool,
}

#[derive(Clone, Debug, Default)]
pub struct ReceiverStats {
    pub bursts_total: u64,
    pub sync_ok: u64,
    pub sysinfo_ok: u64,
    pub mac_ok: u64,
    pub clear_calls: u64,
    pub encrypted_calls: u64,
    pub sds_ok: u64,
    pub lip_ok: u64,
    pub tchs_frames: u64,
}

pub struct TetraReceiver {
    freq_hz: u64,
    demod: Pi4DqpskDemod,
    sync: BurstSync,
    soft_buf: Vec<f32>,

    // Network identity acquired from BSCH SYNC (required to descramble non-BSCH channels).
    mcc: Option<u16>,
    mnc: Option<u16>,
    colour_code: Option<u8>,
    location_area: Option<u16>,
    sysinfo_seen: bool,
    cell_aie: bool,
    cur_timeslot: u8, // 1..=4
    cur_frame: u8,
    cur_multiframe: u8,

    // Deduplication for periodic broadcast bursts (emit on change or every N bursts).
    last_sysinfo_sig: Option<(u16, u16, u8, u16, bool)>,
    sysinfo_bursts_since_emit: u32,

    slots: [SlotState; 4],
    vocoder: Option<ExternalVocoder>,
    pub stats: ReceiverStats,
}

impl TetraReceiver {
    pub fn new(sample_rate: f32, freq_hz: u64, codec_path: Option<&str>) -> Self {
        let vocoder = codec_path.and_then(|p| ExternalVocoder::spawn(p).ok());
        Self {
            freq_hz,
            demod: Pi4DqpskDemod::new(sample_rate),
            sync: BurstSync::new(),
            soft_buf: Vec::with_capacity(4096),
            mcc: None,
            mnc: None,
            colour_code: None,
            location_area: None,
            sysinfo_seen: false,
            cell_aie: false,
            cur_timeslot: 1,
            cur_frame: 1,
            cur_multiframe: 1,
            last_sysinfo_sig: None,
            sysinfo_bursts_since_emit: 0,
            slots: Default::default(),
            vocoder,
            stats: ReceiverStats::default(),
        }
    }

    fn make_scrambler(&self) -> Option<Scrambler> {
        Some(Scrambler::from_mni(self.mcc?, self.mnc?, self.colour_code?))
    }

    fn advance_slot(&mut self) {
        self.cur_timeslot += 1;
        if self.cur_timeslot > 4 {
            self.cur_timeslot = 1;
            self.cur_frame += 1;
            if self.cur_frame > 18 {
                self.cur_frame = 1;
                self.cur_multiframe += 1;
                if self.cur_multiframe > 60 {
                    self.cur_multiframe = 1;
                }
            }
        }
    }

    /// Processes an IQ block and invokes `emit` with each JSON line produced.
    pub fn process_iq<F: FnMut(&str)>(&mut self, iq: &[C32], mut emit: F) {
        self.soft_buf.clear();
        self.demod.process(iq, &mut self.soft_buf);
        let bursts = self.sync.push(&self.soft_buf);
        for burst in bursts {
            self.stats.bursts_total += 1;
            self.handle_burst(burst, &mut emit);
            self.advance_slot();
        }
    }

    fn handle_burst<F: FnMut(&str)>(&mut self, burst: DownlinkBurst, emit: &mut F) {
        match burst {
            DownlinkBurst::Sync { bsch, bbk, bkn2, corr } => {
                let mut zero_scr = Scrambler::bsch_zero();
                if let Some(bits) = decode_logical_block(&bsch, &mut zero_scr, 11, RcpcRate::Rate2_3, 60) {
                    if let Some(sync_pdu) = SyncPdu::parse(&bits) {
                        self.stats.sync_ok += 1;
                        let first_sync = self.mcc != Some(sync_pdu.mcc)
                            || self.mnc != Some(sync_pdu.mnc)
                            || self.colour_code != Some(sync_pdu.colour_code);
                        self.mcc = Some(sync_pdu.mcc);
                        self.mnc = Some(sync_pdu.mnc);
                        self.colour_code = Some(sync_pdu.colour_code);
                        self.cur_timeslot = sync_pdu.timeslot;
                        self.cur_frame = sync_pdu.frame;
                        self.cur_multiframe = sync_pdu.multiframe;
                        if first_sync {
                            let line = format!(
                                "{{\"type\":\"sync\",\"freq_hz\":{},\"mcc\":{},\"mnc\":{},\"cc\":{},\"ts\":{},\"fn\":{},\"mn\":{},\"cfo_hz\":{:.1},\"corr\":{:.2}}}",
                                self.freq_hz, sync_pdu.mcc, sync_pdu.mnc, sync_pdu.colour_code,
                                sync_pdu.timeslot, sync_pdu.frame, sync_pdu.multiframe,
                                self.demod.cfo_hz(), corr
                            );
                            emit(&line);
                        }
                    }
                }
                if let Some(mut scr) = self.make_scrambler() {
                    let mut bbk_s = bbk;
                    scr.apply_soft(&mut bbk_s);
                    let (aach_bits, _) = rm3014_decode_soft(&bbk_s);
                    let _aach = Aach::parse(&aach_bits);
                }
                // BKN2 of SB carries BNCH (SYSINFO) or SCH/HD.
                if let Some(mut scr) = self.make_scrambler() {
                    if let Some(bits) = decode_logical_block(&bkn2, &mut scr, 101, RcpcRate::Rate2_3, 124) {
                        self.handle_signalling_bits(&bits, emit);
                    }
                }
            }
            DownlinkBurst::Normal { bkn1, bbk, bkn2, split_slot } => {
                let Some(mut bbk_scr) = self.make_scrambler() else {
                    return; // Wait until BSCH SYNC provides MCC/MNC/CC
                };
                let mut bbk_s = bbk;
                bbk_scr.apply_soft(&mut bbk_s);
                let (aach_bits, conf) = rm3014_decode_soft(&bbk_s);
                let aach = if conf > 0.35 {
                    Aach::parse(&aach_bits)
                } else {
                    Aach { header: 0, dl_usage: DownlinkUsage::CommonControl, ul_usage: 0 }
                };

                let ts_idx = (self.cur_timeslot.saturating_sub(1) as usize).min(3);

                if !split_slot {
                    // Full-slot (NTS1): either SCH/F (268 info bits) or TCH/S (traffic voice).
                    let mut combined = [0.0f32; 432];
                    combined[..216].copy_from_slice(&bkn1);
                    combined[216..432].copy_from_slice(&bkn2);

                    match aach.dl_usage {
                        DownlinkUsage::Traffic(_marker) => {
                            let st = &self.slots[ts_idx];
                            // Fail-closed: never decode TCH/S until SYSINFO confirms no cell AIE
                            // AND a clear call setup opened this slot for speech.
                            if !self.sysinfo_seen || self.cell_aie || st.encrypted || !st.active_voice {
                                return;
                            }
                            if let Some(mut tscr) = self.make_scrambler() {
                                let (f0, f1) = extract_tchs_frames(&combined, &mut tscr);
                                self.stats.tchs_frames += 2;
                                if let Some(ref mut voc) = self.vocoder {
                                    let mut pcm_bytes = Vec::with_capacity(960);
                                    for f in [&f0, &f1] {
                                        if let Some(pcm) = voc.decode_frame(f) {
                                            for s in pcm {
                                                pcm_bytes.extend_from_slice(&s.to_le_bytes());
                                            }
                                        }
                                    }
                                    if !pcm_bytes.is_empty() {
                                        let b64 = base64_encode(&pcm_bytes);
                                        let line = format!(
                                            "{{\"type\":\"audio\",\"ts\":{},\"rate\":8000,\"pcm_b64\":\"{}\"}}",
                                            self.cur_timeslot, b64
                                        );
                                        emit(&line);
                                    }
                                }
                            }
                        }
                        _ => {
                            // Try SCH/F (268 info bits + 16 CRC + 4 tail = 288 -> 432 bits, rate 2/3, a=103).
                            if let Some(mut scr) = self.make_scrambler() {
                                if let Some(bits) = decode_logical_block(&combined, &mut scr, 103, RcpcRate::Rate2_3, 268) {
                                    self.handle_signalling_bits(&bits, emit);
                                }
                            }
                        }
                    }
                } else {
                    // Split half-slot (NTS2): two independent 216-bit blocks (SCH/HD / BNCH / STCH).
                    for blk in [&bkn1, &bkn2] {
                        if let Some(mut scr) = self.make_scrambler() {
                            if let Some(bits) = decode_logical_block(blk, &mut scr, 101, RcpcRate::Rate2_3, 124) {
                                self.handle_signalling_bits(&bits, emit);
                            }
                        }
                    }
                }
            }
        }
    }

    fn handle_signalling_bits<F: FnMut(&str)>(&mut self, bits: &[u8], emit: &mut F) {
        // 1. Check MAC-BROADCAST SYSINFO (BNCH)
        if let Some(sys) = SysinfoPdu::parse(bits) {
            self.stats.sysinfo_ok += 1;
            self.location_area = Some(sys.location_area);
            self.sysinfo_seen = true;
            self.cell_aie = sys.air_interface_encryption;
            let sig = (
                self.mcc.unwrap_or(0),
                self.mnc.unwrap_or(0),
                self.colour_code.unwrap_or(0),
                sys.location_area,
                sys.air_interface_encryption,
            );
            self.sysinfo_bursts_since_emit += 1;
            if self.last_sysinfo_sig != Some(sig) || self.sysinfo_bursts_since_emit >= 250 {
                self.last_sysinfo_sig = Some(sig);
                self.sysinfo_bursts_since_emit = 0;
                let line = format!(
                    "{{\"type\":\"sysinfo\",\"freq_hz\":{},\"dl_freq_hz\":{},\"mcc\":{},\"mnc\":{},\"cc\":{},\"la\":{},\"carrier\":{},\"voice\":{},\"encrypted\":{}}}",
                    self.freq_hz,
                    sys.downlink_freq_hz(),
                    self.mcc.unwrap_or(0),
                    self.mnc.unwrap_or(0),
                    self.colour_code.unwrap_or(0),
                    sys.location_area,
                    sys.main_carrier,
                    sys.voice_service,
                    sys.air_interface_encryption
                );
                emit(&line);
            }
            return;
        }

        // 2. Check MAC-RESOURCE (call control, channel allocation, SDS, LIP)
        if let Some(res) = MacResource::parse(bits) {
            self.stats.mac_ok += 1;
            let ts_idx = (self.cur_timeslot.saturating_sub(1) as usize).min(3);

            // Apply channel allocation mask if present (marks target timeslots).
            if let Some(ca) = res.chan_alloc {
                for bit in 0..4usize {
                    if (ca.timeslot_mask & (1 << (3 - bit))) != 0 {
                        self.slots[bit].gssi = res.ssi;
                        self.slots[bit].encrypted = res.encryption_mode != 0 || self.cell_aie;
                    }
                }
            }

            if res.encryption_mode != 0 {
                self.stats.encrypted_calls += 1;
                self.slots[ts_idx].encrypted = true;
                self.slots[ts_idx].active_voice = false;
                let gssi = res.ssi.unwrap_or(0);
                let line = format!(
                    "{{\"type\":\"encrypted\",\"freq_hz\":{},\"ts\":{},\"gssi\":{},\"enc_mode\":{},\"mcc\":{},\"mnc\":{}}}",
                    self.freq_hz, self.cur_timeslot, gssi, res.encryption_mode,
                    self.mcc.unwrap_or(0), self.mnc.unwrap_or(0)
                );
                emit(&line);
                return;
            }

            if let Some(ev) = parse_tm_sdu(&res.tm_sdu) {
                match ev {
                    UpperEvent::CallSetup { call_id, calling_ssi, speech, encrypted_e2e } => {
                        let enc = encrypted_e2e || self.cell_aie;
                        self.slots[ts_idx].gssi = res.ssi;
                        self.slots[ts_idx].issi = calling_ssi;
                        self.slots[ts_idx].call_id = Some(call_id);
                        self.slots[ts_idx].encrypted = enc;
                        self.slots[ts_idx].active_voice = speech && !enc;
                        if enc {
                            self.stats.encrypted_calls += 1;
                            let line = format!(
                                "{{\"type\":\"encrypted\",\"freq_hz\":{},\"ts\":{},\"gssi\":{},\"issi\":{},\"call_id\":{},\"e2e\":true,\"mcc\":{},\"mnc\":{}}}",
                                self.freq_hz, self.cur_timeslot, res.ssi.unwrap_or(0), calling_ssi.unwrap_or(0), call_id,
                                self.mcc.unwrap_or(0), self.mnc.unwrap_or(0)
                            );
                            emit(&line);
                        } else {
                            self.stats.clear_calls += 1;
                            let line = format!(
                                "{{\"type\":\"call\",\"event\":\"setup\",\"freq_hz\":{},\"ts\":{},\"gssi\":{},\"issi\":{},\"call_id\":{},\"speech\":{},\"mcc\":{},\"mnc\":{}}}",
                                self.freq_hz, self.cur_timeslot, res.ssi.unwrap_or(0), calling_ssi.unwrap_or(0),
                                call_id, speech, self.mcc.unwrap_or(0), self.mnc.unwrap_or(0)
                            );
                            emit(&line);
                        }
                    }
                    UpperEvent::CallGrant { call_id, transmitting_ssi, encrypted_e2e } => {
                        let enc = encrypted_e2e || self.cell_aie;
                        self.slots[ts_idx].encrypted = enc;
                        if let Some(issi) = transmitting_ssi {
                            self.slots[ts_idx].issi = Some(issi);
                        }
                        if enc {
                            self.stats.encrypted_calls += 1;
                            let line = format!(
                                "{{\"type\":\"encrypted\",\"freq_hz\":{},\"ts\":{},\"gssi\":{},\"issi\":{},\"call_id\":{}}}",
                                self.freq_hz, self.cur_timeslot, res.ssi.or(self.slots[ts_idx].gssi).unwrap_or(0),
                                transmitting_ssi.unwrap_or(0), call_id
                            );
                            emit(&line);
                        } else {
                            self.stats.clear_calls += 1;
                            let line = format!(
                                "{{\"type\":\"call\",\"event\":\"grant\",\"freq_hz\":{},\"ts\":{},\"gssi\":{},\"issi\":{},\"call_id\":{},\"mcc\":{},\"mnc\":{}}}",
                                self.freq_hz, self.cur_timeslot, res.ssi.or(self.slots[ts_idx].gssi).unwrap_or(0),
                                transmitting_ssi.or(self.slots[ts_idx].issi).unwrap_or(0), call_id,
                                self.mcc.unwrap_or(0), self.mnc.unwrap_or(0)
                            );
                            emit(&line);
                        }
                    }
                    UpperEvent::CallCeased { call_id } => {
                        self.slots[ts_idx].active_voice = false;
                        let line = format!(
                            "{{\"type\":\"call\",\"event\":\"ceased\",\"freq_hz\":{},\"ts\":{},\"call_id\":{}}}",
                            self.freq_hz, self.cur_timeslot, call_id
                        );
                        emit(&line);
                    }
                    UpperEvent::CallRelease { call_id, cause } => {
                        self.slots[ts_idx] = SlotState::default();
                        let line = format!(
                            "{{\"type\":\"call\",\"event\":\"release\",\"freq_hz\":{},\"ts\":{},\"call_id\":{},\"cause\":{}}}",
                            self.freq_hz, self.cur_timeslot, call_id, cause
                        );
                        emit(&line);
                    }
                    UpperEvent::SdsText { calling_ssi, protocol_id, text } => {
                        self.stats.sds_ok += 1;
                        let line = format!(
                            "{{\"type\":\"sds\",\"freq_hz\":{},\"ts\":{},\"dst\":{},\"src\":{},\"pid\":{},\"text\":\"{}\",\"mcc\":{},\"mnc\":{}}}",
                            self.freq_hz, self.cur_timeslot, res.ssi.unwrap_or(0), calling_ssi.unwrap_or(0),
                            protocol_id, json_escape(&text), self.mcc.unwrap_or(0), self.mnc.unwrap_or(0)
                        );
                        emit(&line);
                    }
                    UpperEvent::LipLocation { calling_ssi, lat, lon, position_error_code } => {
                        self.stats.lip_ok += 1;
                        let line = format!(
                            "{{\"type\":\"lip\",\"freq_hz\":{},\"ts\":{},\"dst\":{},\"src\":{},\"lat\":{:.6},\"lon\":{:.6},\"err\":{},\"mcc\":{},\"mnc\":{}}}",
                            self.freq_hz, self.cur_timeslot, res.ssi.unwrap_or(0), calling_ssi.unwrap_or(0),
                            lat, lon, position_error_code, self.mcc.unwrap_or(0), self.mnc.unwrap_or(0)
                        );
                        emit(&line);
                    }
                }
            }
        }
    }
}

fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {}
            c => out.push(c),
        }
    }
    out
}
