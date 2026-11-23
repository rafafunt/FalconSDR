//! falconsdr-tetra: Experimental clear-mode TETRA (ETSI EN 300 392-2) decoder.
//!
//! Modes:
//! - `--pipe`            Read CF32 IQ from stdin, emit JSON-line events (+ clear PCM) on stdout
//! - `--iq <file>`       Offline decode of a raw CF32 / SigMF `.sigmf-data` IQ recording
//! - `--selftest`        End-to-end verification on a synthetic noisy/CFO-shifted TETRA downlink

mod burst;
mod coding;
mod mac;
mod phy;
mod receiver;
mod synth;
mod upper;
mod vocoder;

use std::env;
use std::fs::File;
use std::io::{self, BufReader, Read, Write};
use std::process;

use crate::phy::{C32, DEFAULT_IQ_RATE, SimpleChannelizer};
use crate::receiver::TetraReceiver;

fn print_help() {
    eprintln!(
        "falconsdr-tetra {} (experimental clear-mode TETRA decoder, GPL-3.0-or-later)\n\
         Usage:\n\
         \x20 falconsdr-tetra --pipe [--rate 72000] [--freq-hz 0] [--offset-hz 0] [--codec <path>]\n\
         \x20 falconsdr-tetra --iq <file.cf32> [--rate 72000] [--freq-hz 0] [--offset-hz 0] [--codec <path>]\n\
         \x20 falconsdr-tetra --selftest\n",
        env!("CARGO_PKG_VERSION")
    );
}

fn run_stream<R: Read>(mut reader: R, rate: f32, freq_hz: u64, offset_hz: f32, codec: Option<&str>) {
    let mut chan = SimpleChannelizer::new(rate, DEFAULT_IQ_RATE, offset_hz);
    let eff_rate = chan.out_rate(rate);
    let mut rx = TetraReceiver::new(eff_rate, freq_hz, codec);

    let mut stdout = io::BufWriter::new(io::stdout().lock());
    let mut raw = vec![0u8; 8192 * 8];
    let mut leftover = Vec::<u8>::new();
    let mut iq_in = Vec::<C32>::with_capacity(8192);
    let mut iq_ch = Vec::<C32>::with_capacity(8192);

    loop {
        let n = match reader.read(&mut raw) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        };
        leftover.extend_from_slice(&raw[..n]);
        let usable = (leftover.len() / 8) * 8;
        if usable == 0 {
            continue;
        }
        iq_in.clear();
        for ch in leftover[..usable].chunks_exact(8) {
            let re = f32::from_le_bytes([ch[0], ch[1], ch[2], ch[3]]);
            let im = f32::from_le_bytes([ch[4], ch[5], ch[6], ch[7]]);
            iq_in.push(C32::new(re, im));
        }
        leftover.drain(0..usable);

        iq_ch.clear();
        chan.process(&iq_in, &mut iq_ch);
        if !iq_ch.is_empty() {
            rx.process_iq(&iq_ch, |line| {
                let _ = writeln!(stdout, "{line}");
                let _ = stdout.flush();
            });
        }
    }
}

fn run_selftest() -> bool {
    let iq = synth::generate_test_iq(650.0, 18.0);
    let mut rx = TetraReceiver::new(DEFAULT_IQ_RATE, 392_412_500, None);
    let mut events = Vec::new();
    for chunk in iq.chunks(2160) {
        rx.process_iq(chunk, |line| events.push(line.to_string()));
    }
    for e in &events {
        println!("{e}");
    }
    let ok = rx.stats.sync_ok >= 1
        && rx.stats.sysinfo_ok >= 1
        && rx.stats.clear_calls >= 1
        && rx.stats.encrypted_calls >= 1
        && rx.stats.sds_ok >= 1
        && rx.stats.lip_ok >= 1;
    eprintln!(
        "selftest: {} (bursts={} sync={} sysinfo={} clear_calls={} encrypted={} sds={} lip={})",
        if ok { "PASS" } else { "FAIL" },
        rx.stats.bursts_total,
        rx.stats.sync_ok,
        rx.stats.sysinfo_ok,
        rx.stats.clear_calls,
        rx.stats.encrypted_calls,
        rx.stats.sds_ok,
        rx.stats.lip_ok
    );
    ok
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() <= 1 || args.iter().any(|a| a == "-h" || a == "--help") {
        print_help();
        return;
    }

    let mut pipe = false;
    let mut selftest = false;
    let mut emit_test_iq = false;
    let mut iq_file: Option<String> = None;
    let mut rate = DEFAULT_IQ_RATE;
    let mut freq_hz = 0u64;
    let mut offset_hz = 0.0f32;
    let mut codec: Option<String> = None;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--pipe" => pipe = true,
            "--selftest" => selftest = true,
            "--emit-test-iq" => emit_test_iq = true,
            "--version" => {
                println!("falconsdr-tetra {}", env!("CARGO_PKG_VERSION"));
                return;
            }
            "--iq" if i + 1 < args.len() => {
                i += 1;
                iq_file = Some(args[i].clone());
            }
            "--rate" if i + 1 < args.len() => {
                i += 1;
                rate = args[i].parse().unwrap_or(DEFAULT_IQ_RATE);
            }
            "--freq-hz" if i + 1 < args.len() => {
                i += 1;
                freq_hz = args[i].parse().unwrap_or(0);
            }
            "--offset-hz" if i + 1 < args.len() => {
                i += 1;
                offset_hz = args[i].parse().unwrap_or(0.0);
            }
            "--codec" if i + 1 < args.len() => {
                i += 1;
                codec = Some(args[i].clone());
            }
            other => {
                eprintln!("Unknown argument: {other}");
                print_help();
                process::exit(2);
            }
        }
        i += 1;
    }

    if emit_test_iq {
        let iq = synth::generate_test_iq(450.0, 20.0);
        let mut out = io::BufWriter::new(io::stdout().lock());
        for s in iq {
            let _ = out.write_all(&s.re.to_le_bytes());
            let _ = out.write_all(&s.im.to_le_bytes());
        }
        let _ = out.flush();
        return;
    }

    if selftest {
        if !run_selftest() {
            process::exit(1);
        }
        return;
    }

    if pipe {
        run_stream(io::stdin().lock(), rate, freq_hz, offset_hz, codec.as_deref());
    } else if let Some(path) = iq_file {
        match File::open(&path) {
            Ok(f) => run_stream(BufReader::new(f), rate, freq_hz, offset_hz, codec.as_deref()),
            Err(e) => {
                eprintln!("Cannot open {path}: {e}");
                process::exit(1);
            }
        }
    } else {
        print_help();
        process::exit(2);
    }
}

#[cfg(test)]
mod integration_tests {
    use super::*;

    #[test]
    fn full_iq_pipeline_decodes_sync_sysinfo_calls_sds_lip_and_mutes_encrypted() {
        for &(cfo, snr) in &[(0.0, 25.0), (750.0, 16.0), (-900.0, 14.0)] {
            let iq = synth::generate_test_iq(cfo, snr);
            let mut rx = TetraReceiver::new(DEFAULT_IQ_RATE, 392_412_500, None);
            let mut events = Vec::new();
            for chunk in iq.chunks(2160) {
                rx.process_iq(chunk, |line| events.push(line.to_string()));
            }
            assert!(rx.stats.sync_ok >= 1, "cfo={cfo} snr={snr}: missing SYNC");
            assert!(rx.stats.sysinfo_ok >= 1, "cfo={cfo} snr={snr}: missing SYSINFO");
            assert!(rx.stats.clear_calls >= 1, "cfo={cfo} snr={snr}: missing clear D-SETUP");
            assert!(rx.stats.encrypted_calls >= 1, "cfo={cfo} snr={snr}: missing encrypted flag");
            assert!(rx.stats.sds_ok >= 1, "cfo={cfo} snr={snr}: missing SDS text");
            assert!(rx.stats.lip_ok >= 1, "cfo={cfo} snr={snr}: missing LIP GPS");
            assert!(events.iter().any(|e| e.contains("\"TEST CLEAR SDS\"")));
            assert!(events.iter().any(|e| e.contains("\"type\":\"lip\"") && e.contains("50.061")));
            assert!(events.iter().any(|e| e.contains("\"type\":\"encrypted\"") && e.contains("\"gssi\":9999")));
        }
    }
}
