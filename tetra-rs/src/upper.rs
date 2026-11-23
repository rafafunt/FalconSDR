//! TETRA Upper Layer (MLE / CMCE / SDS / LIP) clear-mode PDU parser
//! (ETSI EN 300 392-2 Clause 14, 18, 29 and ETSI TS 100 392-18-1 LIP).
//!
//! Only ever invoked on unencrypted (encryption_mode == 0) TM-SDU payloads.

use crate::mac::{BitReader, BitWriter};

/// MLE protocol discriminator (3 bits, ETSI Clause 18.5.21).
#[allow(dead_code)]
pub const MLE_MM: u8 = 0b001;
pub const MLE_CMCE: u8 = 0b010;
#[allow(dead_code)]
pub const MLE_SNDCP: u8 = 0b100;

/// CMCE PDU types (5 bits, ETSI Clause 14.8.28).
pub const CMCE_D_CONNECT: u8 = 0x02;
pub const CMCE_D_RELEASE: u8 = 0x06;
pub const CMCE_D_SETUP: u8 = 0x07;
pub const CMCE_D_TX_CEASED: u8 = 0x08;
pub const CMCE_D_TX_GRANTED: u8 = 0x09;
pub const CMCE_D_SDS_DATA: u8 = 0x0F;

#[derive(Clone, Debug, PartialEq)]
pub enum UpperEvent {
    CallSetup {
        call_id: u16,
        calling_ssi: Option<u32>,
        speech: bool,
        encrypted_e2e: bool,
    },
    CallGrant {
        call_id: u16,
        transmitting_ssi: Option<u32>,
        encrypted_e2e: bool,
    },
    CallCeased {
        call_id: u16,
    },
    CallRelease {
        call_id: u16,
        cause: u8,
    },
    SdsText {
        calling_ssi: Option<u32>,
        protocol_id: u8,
        text: String,
    },
    LipLocation {
        calling_ssi: Option<u32>,
        lat: f64,
        lon: f64,
        position_error_code: u8,
    },
}

/// Parses an unencrypted TM-SDU (MLE header + CMCE PDU).
pub fn parse_tm_sdu(bits: &[u8]) -> Option<UpperEvent> {
    if bits.len() < 8 {
        return None;
    }
    let mut r = BitReader::new(bits);
    let proto = r.read_u32(3)? as u8;
    if proto != MLE_CMCE {
        return None;
    }
    let pdu_type = r.read_u32(5)? as u8;
    match pdu_type {
        CMCE_D_SETUP => parse_d_setup(&mut r),
        CMCE_D_TX_GRANTED => parse_d_tx_granted(&mut r),
        CMCE_D_TX_CEASED => {
            let call_id = r.read_u32(14)? as u16;
            Some(UpperEvent::CallCeased { call_id })
        }
        CMCE_D_RELEASE => {
            let call_id = r.read_u32(14)? as u16;
            let cause = r.read_u32(5).unwrap_or(0) as u8;
            Some(UpperEvent::CallRelease { call_id, cause })
        }
        CMCE_D_CONNECT => {
            let call_id = r.read_u32(14)? as u16;
            Some(UpperEvent::CallGrant { call_id, transmitting_ssi: None, encrypted_e2e: false })
        }
        CMCE_D_SDS_DATA => parse_d_sds_data(&mut r),
        _ => None,
    }
}

fn parse_d_setup(r: &mut BitReader<'_>) -> Option<UpperEvent> {
    let call_id = r.read_u32(14)? as u16;
    let _call_timeout = r.read_u32(4)?;
    let _hook = r.read_bool()?;
    let _simplex = r.read_bool()?;
    // Basic service information (8 bits, Clause 14.8.3):
    // bit 7 = circuit_mode (0=speech, 1=data), bit 6 = encryption flag (E2EE)
    let bsi = r.read_u32(8)? as u8;
    let speech = (bsi & 0x80) == 0;
    let encrypted_e2e = (bsi & 0x40) != 0;
    let _tx_grant = r.read_u32(2)?;
    let _tx_req_perm = r.read_bool()?;
    let _call_prio = r.read_u32(4)?;
    // Calling party type identifier (2 bits: 1 = SSI 24 bits)
    let calling_ssi = match r.read_u32(2) {
        Some(1) => r.read_u32(24),
        Some(2) => {
            let ssi = r.read_u32(24);
            let _ = r.read_u32(24); // ext
            ssi
        }
        _ => None,
    };
    Some(UpperEvent::CallSetup { call_id, calling_ssi, speech, encrypted_e2e })
}

fn parse_d_tx_granted(r: &mut BitReader<'_>) -> Option<UpperEvent> {
    let call_id = r.read_u32(14)? as u16;
    let _tx_grant = r.read_u32(2)?;
    let _tx_req_perm = r.read_bool()?;
    let encrypted_e2e = r.read_bool()?;
    let _reserved = r.read_bool()?;
    // Optional elements flag + transmitting party type identifier
    let transmitting_ssi = if r.remaining() >= 26 {
        let o_bit = r.read_bool().unwrap_or(false);
        if o_bit {
            match r.read_u32(2) {
                Some(1) => r.read_u32(24),
                _ => None,
            }
        } else {
            None
        }
    } else {
        None
    };
    Some(UpperEvent::CallGrant { call_id, transmitting_ssi, encrypted_e2e })
}

fn parse_d_sds_data(r: &mut BitReader<'_>) -> Option<UpperEvent> {
    // Calling party type identifier (2 bits)
    let calling_ssi = match r.read_u32(2)? {
        1 => r.read_u32(24),
        2 => {
            let ssi = r.read_u32(24);
            let _ = r.read_u32(24);
            ssi
        }
        _ => None,
    };
    let sds_type = r.read_u32(2)? as u8;
    let payload_bits = match sds_type {
        0 => 16usize,
        1 => 32,
        2 => 64,
        _ => {
            // Type 4: 11-bit length indicator (in bits)
            let len = r.read_u32(11)? as usize;
            len.min(r.remaining())
        }
    };
    if r.remaining() < payload_bits || payload_bits < 8 {
        return None;
    }
    let start = r.pos();
    let payload = &r.rest()[..payload_bits];
    let _ = start;

    let mut pr = BitReader::new(payload);
    let pid = pr.read_u32(8)? as u8;

    // ETSI TS 100 392-18-1: LIP (Location Information Protocol), PID = 0x0A.
    if pid == 0x0A {
        if let Some((lat, lon, err_code)) = parse_lip_short(pr.rest()) {
            return Some(UpperEvent::LipLocation {
                calling_ssi,
                lat,
                lon,
                position_error_code: err_code,
            });
        }
    }

    // SDS-TL Text messaging (PID 0x82 = simple text, 0x02 = text messaging, or printable ASCII payload).
    let text = if pid == 0x82 || pid == 0x02 {
        decode_sds_tl_text(pr.rest())
    } else {
        decode_printable_bytes(pr.rest())
    };
    if text.is_empty() {
        return None;
    }
    Some(UpperEvent::SdsText { calling_ssi, protocol_id: pid, text })
}

/// Parses an ETSI TS 100 392-18-1 LIP Short Location Report (PDU type 0).
fn parse_lip_short(bits: &[u8]) -> Option<(f64, f64, u8)> {
    // PDU type (2) + Time elapsed (2) + Longitude (25, signed) + Latitude (24, signed) + Position error (3) = 56 bits
    if bits.len() < 56 {
        return None;
    }
    let mut r = BitReader::new(bits);
    let pdu_type = r.read_u32(2)?;
    if pdu_type != 0 {
        return None;
    }
    let _time_elapsed = r.read_u32(2)?;
    let lon_raw = r.read_u32(25)?;
    let lat_raw = r.read_u32(24)?;
    let err_code = r.read_u32(3)? as u8;

    // Two's complement sign extension for 25-bit longitude and 24-bit latitude.
    let lon_signed = if (lon_raw & (1 << 24)) != 0 {
        (lon_raw as i32) - (1 << 25)
    } else {
        lon_raw as i32
    };
    let lat_signed = if (lat_raw & (1 << 23)) != 0 {
        (lat_raw as i32) - (1 << 24)
    } else {
        lat_raw as i32
    };

    let lon = (lon_signed as f64) * 360.0 / ((1u64 << 25) as f64);
    let lat = (lat_signed as f64) * 180.0 / ((1u64 << 24) as f64);
    if !(-90.0..=90.0).contains(&lat) || !(-180.0..=180.0).contains(&lon) {
        return None;
    }
    Some((lat, lon, err_code))
}

fn decode_sds_tl_text(bits: &[u8]) -> String {
    // Simple Text Messaging (PID 0x82): 1-bit timestamp flag + 7-bit coding scheme (0=7-bit, 1=ISO-8859-1) + text
    if bits.len() < 16 {
        return String::new();
    }
    let mut r = BitReader::new(bits);
    let ts_used = r.read_bool().unwrap_or(false);
    let scheme = r.read_u32(7).unwrap_or(1);
    if ts_used {
        let _ = r.read_u32(24);
    }
    let rest = r.rest();
    if scheme == 0 {
        // 7-bit packed ASCII
        let mut s = String::new();
        for ch in rest.chunks_exact(7) {
            let mut v = 0u8;
            for &b in ch {
                v = (v << 1) | (b & 1);
            }
            if v == 0 {
                break;
            }
            if (0x20..=0x7E).contains(&v) {
                s.push(v as char);
            }
        }
        s.trim().to_string()
    } else {
        decode_printable_bytes(rest)
    }
}

fn decode_printable_bytes(bits: &[u8]) -> String {
    let mut bytes = Vec::new();
    for ch in bits.chunks_exact(8) {
        let mut v = 0u8;
        for &b in ch {
            v = (v << 1) | (b & 1);
        }
        if v == 0 {
            break;
        }
        if (0x20..=0x7E).contains(&v) || v >= 0xA0 {
            bytes.push(v);
        }
    }
    String::from_utf8_lossy(&bytes).trim().to_string()
}

/// Encoders for synthetic ETSI test generation.
pub fn encode_d_setup(call_id: u16, calling_ssi: u32, speech: bool, encrypted_e2e: bool) -> Vec<u8> {
    let mut w = BitWriter::new(64);
    w.write_u32(MLE_CMCE as u32, 3);
    w.write_u32(CMCE_D_SETUP as u32, 5);
    w.write_u32(call_id as u32, 14);
    w.write_u32(0, 4);
    w.write_bool(false);
    w.write_bool(true);
    let bsi = (if speech { 0 } else { 0x80 }) | (if encrypted_e2e { 0x40 } else { 0 });
    w.write_u32(bsi, 8);
    w.write_u32(0, 2);
    w.write_bool(false);
    w.write_u32(0, 4);
    w.write_u32(1, 2); // calling party = SSI
    w.write_u32(calling_ssi, 24);
    w.bits
}

pub fn encode_d_sds_text(calling_ssi: u32, text: &str) -> Vec<u8> {
    let mut payload = BitWriter::new(16 + text.len() * 8);
    payload.write_u32(0x82, 8); // Simple Text Messaging
    payload.write_bool(false);  // no timestamp
    payload.write_u32(1, 7);    // 8-bit ISO-8859-1 / UTF-8
    for &b in text.as_bytes() {
        payload.write_u32(b as u32, 8);
    }

    let mut w = BitWriter::new(64 + payload.bits.len());
    w.write_u32(MLE_CMCE as u32, 3);
    w.write_u32(CMCE_D_SDS_DATA as u32, 5);
    w.write_u32(1, 2); // calling party = SSI
    w.write_u32(calling_ssi, 24);
    w.write_u32(3, 2); // Type 4
    w.write_u32(payload.bits.len() as u32, 11);
    w.write_bits(&payload.bits);
    w.bits
}

pub fn encode_d_sds_lip(calling_ssi: u32, lat: f64, lon: f64) -> Vec<u8> {
    let lat_i = (lat * ((1u64 << 24) as f64) / 180.0).round() as i32;
    let lon_i = (lon * ((1u64 << 25) as f64) / 360.0).round() as i32;
    let lat_u = (lat_i as u32) & ((1 << 24) - 1);
    let lon_u = (lon_i as u32) & ((1 << 25) - 1);

    let mut payload = BitWriter::new(64);
    payload.write_u32(0x0A, 8); // LIP PID
    payload.write_u32(0, 2);    // Short Location Report
    payload.write_u32(0, 2);    // time elapsed
    payload.write_u32(lon_u, 25);
    payload.write_u32(lat_u, 24);
    payload.write_u32(1, 3);    // position error code

    let mut w = BitWriter::new(128);
    w.write_u32(MLE_CMCE as u32, 3);
    w.write_u32(CMCE_D_SDS_DATA as u32, 5);
    w.write_u32(1, 2);
    w.write_u32(calling_ssi, 24);
    w.write_u32(3, 2);
    w.write_u32(payload.bits.len() as u32, 11);
    w.write_bits(&payload.bits);
    w.bits
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cmce_setup_sds_and_lip_roundtrip() {
        let setup = encode_d_setup(1042, 2601234, true, false);
        assert_eq!(
            parse_tm_sdu(&setup),
            Some(UpperEvent::CallSetup { call_id: 1042, calling_ssi: Some(2601234), speech: true, encrypted_e2e: false })
        );

        let sds = encode_d_sds_text(2605555, "HELLO TETRA");
        assert_eq!(
            parse_tm_sdu(&sds),
            Some(UpperEvent::SdsText { calling_ssi: Some(2605555), protocol_id: 0x82, text: "HELLO TETRA".into() })
        );

        let lip = encode_d_sds_lip(2607777, 50.0619, 19.9368);
        match parse_tm_sdu(&lip) {
            Some(UpperEvent::LipLocation { calling_ssi: Some(2607777), lat, lon, .. }) => {
                assert!((lat - 50.0619).abs() < 1e-4);
                assert!((lon - 19.9368).abs() < 1e-4);
            }
            other => panic!("Unexpected LIP parse result: {other:?}"),
        }
    }
}
