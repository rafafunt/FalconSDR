//! TETRA Layer 2 MAC PDUs (ETSI EN 300 392-2 Clause 21).
//!
//! Parses and builds:
//! - AACH (Access Assignment Channel, 14 bits, Clause 21.4.7)
//! - SYNC PDU (BSCH, 60 bits, Clause 21.4.4.1)
//! - SYSINFO PDU (BNCH, 124 bits, Clause 21.4.4.2)
//! - MAC-RESOURCE PDU (SCH/F & SCH/HD, Clause 21.4.3.1)

/// Safe bounds-checked MSB-first bit reader over a slice of `{0, 1}` bits.
pub struct BitReader<'a> {
    bits: &'a [u8],
    pos: usize,
}

impl<'a> BitReader<'a> {
    pub fn new(bits: &'a [u8]) -> Self {
        Self { bits, pos: 0 }
    }

    pub fn remaining(&self) -> usize {
        self.bits.len().saturating_sub(self.pos)
    }

    pub fn pos(&self) -> usize {
        self.pos
    }

    pub fn read_u32(&mut self, n: usize) -> Option<u32> {
        if n > 32 || self.remaining() < n {
            return None;
        }
        let mut v = 0u32;
        for _ in 0..n {
            v = (v << 1) | ((self.bits[self.pos] & 1) as u32);
            self.pos += 1;
        }
        Some(v)
    }

    pub fn read_bool(&mut self) -> Option<bool> {
        self.read_u32(1).map(|v| v != 0)
    }

    pub fn rest(&self) -> &'a [u8] {
        &self.bits[self.pos..]
    }
}

/// MSB-first bit writer for tests and synthetic ETSI PDU generation.
pub struct BitWriter {
    pub bits: Vec<u8>,
}

impl BitWriter {
    pub fn new(cap: usize) -> Self {
        Self { bits: Vec::with_capacity(cap) }
    }

    pub fn write_u32(&mut self, val: u32, n: usize) {
        for shift in (0..n).rev() {
            self.bits.push(((val >> shift) & 1) as u8);
        }
    }

    pub fn write_bool(&mut self, val: bool) {
        self.bits.push(if val { 1 } else { 0 });
    }

    pub fn write_bits(&mut self, src: &[u8]) {
        self.bits.extend_from_slice(src);
    }

    pub fn pad_to(&mut self, len: usize) {
        while self.bits.len() < len {
            self.bits.push(0);
        }
    }
}

/// Downlink channel usage signalled by AACH (ETSI Clause 21.4.7.2).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DownlinkUsage {
    Unallocated,
    AssignedControl,
    CommonControl,
    CommonAndAssigned,
    Traffic(u8), // usage marker 4..=63
}

/// Access Assignment Channel (14 bits carried in every downlink burst's BBK).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Aach {
    pub header: u8, // 2 bits
    pub dl_usage: DownlinkUsage,
    pub ul_usage: u8, // 6 bits
}

impl Aach {
    pub fn parse(bits: &[u8; 14]) -> Self {
        let mut r = BitReader::new(bits);
        let header = r.read_u32(2).unwrap_or(0) as u8;
        let dl_raw = r.read_u32(6).unwrap_or(0) as u8;
        let ul_usage = r.read_u32(6).unwrap_or(0) as u8;
        let dl_usage = if header == 0 {
            DownlinkUsage::CommonControl
        } else {
            match dl_raw {
                0 => DownlinkUsage::Unallocated,
                1 => DownlinkUsage::AssignedControl,
                2 => DownlinkUsage::CommonControl,
                3 => DownlinkUsage::CommonAndAssigned,
                m => DownlinkUsage::Traffic(m),
            }
        };
        Self { header, dl_usage, ul_usage }
    }

    pub fn encode(header: u8, dl_raw: u8, ul_usage: u8) -> [u8; 14] {
        let mut w = BitWriter::new(14);
        w.write_u32(header as u32, 2);
        w.write_u32(dl_raw as u32, 6);
        w.write_u32(ul_usage as u32, 6);
        let mut out = [0u8; 14];
        out.copy_from_slice(&w.bits[..14]);
        out
    }
}

/// BSCH SYNC PDU (60 bits, ETSI EN 300 392-2 Clause 21.4.4.1 + MLE SYNC Clause 18.5.14).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SyncPdu {
    pub system_code: u8,       // 4 bits
    pub colour_code: u8,       // 6 bits (0..63)
    pub timeslot: u8,          // 1..=4
    pub frame: u8,             // 1..=18
    pub multiframe: u8,        // 1..=60
    pub sharing_mode: u8,      // 2 bits
    pub ts_reserved: u8,       // 3 bits
    pub u_plane_dtx: bool,
    pub frame_18_ext: bool,
    pub mcc: u16,              // 10 bits (e.g. 260 = Poland)
    pub mnc: u16,              // 14 bits
    pub cell_service_level: u8,
    pub late_entry: bool,
}

impl SyncPdu {
    pub fn parse(bits: &[u8]) -> Option<Self> {
        if bits.len() < 60 {
            return None;
        }
        let mut r = BitReader::new(bits);
        let system_code = r.read_u32(4)? as u8;
        let colour_code = r.read_u32(6)? as u8;
        let timeslot = (r.read_u32(2)? as u8) + 1;
        let frame = r.read_u32(5)? as u8;
        let multiframe = r.read_u32(6)? as u8;
        let sharing_mode = r.read_u32(2)? as u8;
        let ts_reserved = r.read_u32(3)? as u8;
        let u_plane_dtx = r.read_bool()?;
        let frame_18_ext = r.read_bool()?;
        let _reserved = r.read_bool()?;
        // Sanity check frame/multiframe bounds per ETSI Clause 21.4.4.1
        if frame == 0 || frame > 18 || multiframe == 0 || multiframe > 60 {
            return None;
        }
        let mcc = r.read_u32(10)? as u16;
        let mnc = r.read_u32(14)? as u16;
        let _neighbor_bc = r.read_u32(2)?;
        let cell_service_level = r.read_u32(2)? as u8;
        let late_entry = r.read_bool()?;
        Some(Self {
            system_code,
            colour_code,
            timeslot,
            frame,
            multiframe,
            sharing_mode,
            ts_reserved,
            u_plane_dtx,
            frame_18_ext,
            mcc,
            mnc,
            cell_service_level,
            late_entry,
        })
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut w = BitWriter::new(60);
        w.write_u32(self.system_code as u32, 4);
        w.write_u32(self.colour_code as u32, 6);
        w.write_u32((self.timeslot.saturating_sub(1) & 3) as u32, 2);
        w.write_u32(self.frame as u32, 5);
        w.write_u32(self.multiframe as u32, 6);
        w.write_u32(self.sharing_mode as u32, 2);
        w.write_u32(self.ts_reserved as u32, 3);
        w.write_bool(self.u_plane_dtx);
        w.write_bool(self.frame_18_ext);
        w.write_bool(false);
        w.write_u32(self.mcc as u32, 10);
        w.write_u32(self.mnc as u32, 14);
        w.write_u32(0, 2);
        w.write_u32(self.cell_service_level as u32, 2);
        w.write_bool(self.late_entry);
        w.bits
    }
}

/// BNCH SYSINFO PDU (124 bits, ETSI EN 300 392-2 Clause 21.4.4.2).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SysinfoPdu {
    pub main_carrier: u16,          // 12 bits (25 kHz channel number)
    pub freq_band: u8,              // 4 bits (x 100 MHz base)
    pub offset_hz: i32,             // 0, +6250, -6250, +12500
    pub duplex_spacing: u8,         // 3 bits
    pub reverse_op: bool,
    pub location_area: u16,         // 14 bits
    pub voice_service: bool,
    pub circuit_data: bool,
    pub air_interface_encryption: bool, // AIE flag (true = cell uses encryption)
}

impl SysinfoPdu {
    /// Computes the downlink carrier frequency in Hz from `freq_band`, `main_carrier`, and `offset_hz`.
    pub fn downlink_freq_hz(&self) -> u64 {
        let base = (self.freq_band as i64) * 100_000_000 + (self.main_carrier as i64) * 25_000 + (self.offset_hz as i64);
        base.max(0) as u64
    }

    pub fn parse(bits: &[u8]) -> Option<Self> {
        if bits.len() < 122 {
            return None;
        }
        let mut r = BitReader::new(bits);
        let pdu_type = r.read_u32(2)?;
        let bcast_type = r.read_u32(2)?;
        if pdu_type != 0b10 || bcast_type != 0b00 {
            return None;
        }
        let main_carrier = r.read_u32(12)? as u16;
        let freq_band = r.read_u32(4)? as u8;
        let offset_code = r.read_u32(2)? as u8;
        let offset_hz = match offset_code {
            0 => 0,
            1 => 6250,
            2 => -6250,
            _ => 12500,
        };
        let duplex_spacing = r.read_u32(3)? as u8;
        let reverse_op = r.read_bool()?;
        let _num_common_sc = r.read_u32(2)?;
        let _ms_txpwr_max = r.read_u32(3)?;
        let _rxlev_access_min = r.read_u32(4)?;
        let _access_param = r.read_u32(4)?;
        let _radio_dl_timeout = r.read_u32(4)?;
        let _hyperframe_or_sc = r.read_u32(17)?;
        let _option_field = r.read_u32(20)?;
        let location_area = r.read_u32(14)? as u16;
        let _subscriber_class = r.read_u32(16)?;
        let bs_service = r.read_u32(12)?;
        // ETSI Clause 21.4.4.2 BS service details (12 bits, MSB = bit 11):
        // bit 5 (mask 0x0020) = voice, bit 4 (0x0010) = circuit data, bit 1 (0x0002) = AIE encryption.
        let voice_service = (bs_service & (1 << 5)) != 0;
        let circuit_data = (bs_service & (1 << 4)) != 0;
        let air_interface_encryption = (bs_service & (1 << 1)) != 0;
        Some(Self {
            main_carrier,
            freq_band,
            offset_hz,
            duplex_spacing,
            reverse_op,
            location_area,
            voice_service,
            circuit_data,
            air_interface_encryption,
        })
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut w = BitWriter::new(124);
        w.write_u32(0b10, 2); // MAC-BROADCAST
        w.write_u32(0b00, 2); // SYSINFO
        w.write_u32(self.main_carrier as u32, 12);
        w.write_u32(self.freq_band as u32, 4);
        let offset_code = match self.offset_hz {
            6250 => 1,
            -6250 => 2,
            12500 => 3,
            _ => 0,
        };
        w.write_u32(offset_code, 2);
        w.write_u32(self.duplex_spacing as u32, 3);
        w.write_bool(self.reverse_op);
        w.write_u32(0, 2);  // num_common_sc
        w.write_u32(0, 3);  // ms_txpwr_max
        w.write_u32(0, 4);  // rxlev_access_min
        w.write_u32(0, 4);  // access_param
        w.write_u32(0, 4);  // radio_dl_timeout
        w.write_u32(0, 17); // hyperframe
        w.write_u32(0, 20); // option_field
        w.write_u32(self.location_area as u32, 14);
        w.write_u32(0xFFFF, 16); // subscriber_class
        let mut bs_service = 0u32;
        if self.voice_service {
            bs_service |= 1 << 5;
        }
        if self.circuit_data {
            bs_service |= 1 << 4;
        }
        if self.air_interface_encryption {
            bs_service |= 1 << 1;
        }
        w.write_u32(bs_service, 12);
        w.pad_to(124);
        w.bits
    }
}

/// Optional channel allocation carried inside MAC-RESOURCE (ETSI Clause 21.5.2).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ChannelAlloc {
    pub alloc_type: u8,   // 2 bits (0=replace, 1=additional, 2=quit&go)
    pub timeslot_mask: u8, // 4 bits (bit 3 = TS1 .. bit 0 = TS4)
    pub carrier: Option<u16>,
}

/// Parsed MAC-RESOURCE PDU (ETSI Clause 21.4.3.1).
#[allow(dead_code)]
#[derive(Clone, Debug)]
pub struct MacResource {
    pub encryption_mode: u8, // 0 = CLEAR, 1..3 = ENCRYPTED (never decrypted)
    pub ssi: Option<u32>,    // 24-bit GSSI / ISSI
    pub event_label: Option<u16>,
    pub chan_alloc: Option<ChannelAlloc>,
    pub tm_sdu: Vec<u8>,     // Upper-layer payload (empty if encrypted)
}

impl MacResource {
    pub fn parse(bits: &[u8]) -> Option<Self> {
        if bits.len() < 16 {
            return None;
        }
        let mut r = BitReader::new(bits);
        let pdu_type = r.read_u32(2)?;
        if pdu_type != 0b00 {
            return None; // Not MAC-RESOURCE
        }
        let fill_bits = r.read_bool()?;
        let _pos_of_grant = r.read_bool()?;
        let encryption_mode = r.read_u32(2)? as u8;
        let _random_access = r.read_bool()?;
        let length_ind = r.read_u32(6)? as usize;
        if length_ind == 0 {
            return None; // Null PDU (filler)
        }
        let addr_type = r.read_u32(3)? as u8;
        let mut ssi = None;
        let mut event_label = None;
        match addr_type {
            0b001 => { ssi = Some(r.read_u32(24)?); }                    // SSI (24)
            0b010 => { event_label = Some(r.read_u32(10)? as u16); }     // Event label (10)
            0b011 => { ssi = Some(r.read_u32(24)?); }                    // USSI (24)
            0b100 => { ssi = Some(r.read_u32(24)?); }                    // SMI (24)
            0b101 => { ssi = Some(r.read_u32(24)?); event_label = Some(r.read_u32(10)? as u16); }
            0b110 => { ssi = Some(r.read_u32(24)?); let _ = r.read_u32(6)?; } // SSI + usage marker
            0b111 => { let _ = r.read_u32(24)?; }
            _ => {}
        }

        // Optional power control (1 bit flag -> 4 bits)
        if r.read_bool()? {
            let _ = r.read_u32(4)?;
        }
        // Optional slot granting (1 bit flag -> 8 bits)
        if r.read_bool()? {
            let _ = r.read_u32(8)?;
        }
        // Optional channel allocation (1 bit flag)
        let mut chan_alloc = None;
        if r.read_bool()? {
            let alloc_type = r.read_u32(2)? as u8;
            let timeslot_mask = r.read_u32(4)? as u8;
            let _ul_dl = r.read_u32(2)?;
            let _clch = r.read_bool()?;
            let _cell_change = r.read_bool()?;
            let carrier = if r.read_bool()? {
                Some(r.read_u32(12)? as u16)
            } else {
                None
            };
            let _ext_carrier = if r.read_bool()? { r.read_u32(4)?; true } else { false };
            let _monitor_pattern = r.read_u32(2)?;
            chan_alloc = Some(ChannelAlloc { alloc_type, timeslot_mask, carrier });
        }

        // Security / policy invariant: NEVER parse or expose upper-layer payload when encrypted.
        if encryption_mode != 0 {
            return Some(Self {
                encryption_mode,
                ssi,
                event_label,
                chan_alloc,
                tm_sdu: Vec::new(),
            });
        }

        let end_bit = if length_ind >= 2 && length_ind <= 62 {
            (length_ind * 8).min(bits.len())
        } else {
            bits.len()
        };
        let mut sdu = if r.pos() < end_bit {
            bits[r.pos()..end_bit].to_vec()
        } else {
            Vec::new()
        };
        if fill_bits {
            // Strip trailing 0s up to and including the last '1' fill marker (ETSI Clause 21.4.3.1).
            while let Some(&last) = sdu.last() {
                sdu.pop();
                if last == 1 {
                    break;
                }
            }
        }
        Some(Self {
            encryption_mode,
            ssi,
            event_label,
            chan_alloc,
            tm_sdu: sdu,
        })
    }

    /// Encodes a clean MAC-RESOURCE PDU (used by the ETSI synthetic test generator).
    pub fn encode(
        encryption_mode: u8,
        ssi: u32,
        chan_alloc: Option<ChannelAlloc>,
        tm_sdu: &[u8],
        target_bits: usize,
    ) -> Vec<u8> {
        let mut body = BitWriter::new(target_bits);
        body.write_u32(0b001, 3); // address_type = SSI
        body.write_u32(ssi, 24);
        body.write_bool(false); // no power control
        body.write_bool(false); // no slot granting
        if let Some(ca) = chan_alloc {
            body.write_bool(true);
            body.write_u32(ca.alloc_type as u32, 2);
            body.write_u32(ca.timeslot_mask as u32, 4);
            body.write_u32(0b11, 2); // both UL/DL
            body.write_bool(false);
            body.write_bool(false);
            if let Some(c) = ca.carrier {
                body.write_bool(true);
                body.write_u32(c as u32, 12);
            } else {
                body.write_bool(false);
            }
            body.write_bool(false); // no ext carrier
            body.write_u32(0, 2);   // monitoring pattern
        } else {
            body.write_bool(false);
        }
        body.write_bits(tm_sdu);
        let raw_bits = 13 + body.bits.len();
        let has_fill = (raw_bits % 8) != 0;
        if has_fill {
            body.write_bool(true);
            while (13 + body.bits.len()) % 8 != 0 {
                body.write_bool(false);
            }
        }
        let len_bytes = ((13 + body.bits.len()) / 8).min(62);
        let mut out = BitWriter::new(target_bits);
        out.write_u32(0b00, 2); // MAC-RESOURCE
        out.write_bool(has_fill);
        out.write_bool(false);
        out.write_u32((encryption_mode & 3) as u32, 2);
        out.write_bool(false);
        out.write_u32(len_bytes as u32, 6);
        out.write_bits(&body.bits);
        out.pad_to(target_bits);
        out.bits.truncate(target_bits);
        out.bits
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sync_and_sysinfo_roundtrip() {
        let sync = SyncPdu {
            system_code: 0,
            colour_code: 19,
            timeslot: 1,
            frame: 18,
            multiframe: 42,
            sharing_mode: 0,
            ts_reserved: 0,
            u_plane_dtx: false,
            frame_18_ext: false,
            mcc: 260,
            mnc: 1234,
            cell_service_level: 1,
            late_entry: true,
        };
        let enc = sync.encode();
        assert_eq!(enc.len(), 60);
        assert_eq!(SyncPdu::parse(&enc), Some(sync));

        let sys = SysinfoPdu {
            main_carrier: 976, // 300 MHz + 976*25 kHz = 324.4 MHz (or band 3: 392.4 MHz with carrier 3696)
            freq_band: 3,
            offset_hz: 12500,
            duplex_spacing: 2,
            reverse_op: false,
            location_area: 77,
            voice_service: true,
            circuit_data: false,
            air_interface_encryption: false,
        };
        let enc_sys = sys.encode();
        assert_eq!(enc_sys.len(), 124);
        assert_eq!(SysinfoPdu::parse(&enc_sys), Some(sys));
        assert_eq!(sys.downlink_freq_hz(), 300_000_000 + 976 * 25_000 + 12_500);
    }
}
