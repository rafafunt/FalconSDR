//! TETRA Physical Layer: π/4-DQPSK demodulator (ETSI EN 300 392-2 Clause 5).
//!
//! Symbol rate: 18,000 sym/s (36,000 bit/s, 2 bits/symbol), RRC roll-off alpha = 0.35.
//! Input: complex baseband IQ at >= 36 kS/s (default 72 kS/s = 4 samples/symbol).
//! Output: soft-decision dibit LLRs `(b0_llr, b1_llr)` where positive favors bit 0.

use std::f32::consts::{FRAC_PI_4, PI};

pub const SYMBOL_RATE: f32 = 18_000.0;
pub const RRC_ALPHA: f32 = 0.35;
pub const DEFAULT_IQ_RATE: f32 = 72_000.0; // 4 samples per symbol

#[derive(Clone, Copy, Debug, Default)]
pub struct C32 {
    pub re: f32,
    pub im: f32,
}

impl C32 {
    #[inline(always)]
    pub const fn new(re: f32, im: f32) -> Self {
        Self { re, im }
    }

    #[inline(always)]
    pub fn from_polar(r: f32, theta: f32) -> Self {
        let (s, c) = theta.sin_cos();
        Self { re: r * c, im: r * s }
    }

    #[inline(always)]
    pub fn conj(self) -> Self {
        Self { re: self.re, im: -self.im }
    }

    #[inline(always)]
    pub fn norm_sq(self) -> f32 {
        self.re * self.re + self.im * self.im
    }

    #[inline(always)]
    pub fn norm(self) -> f32 {
        self.norm_sq().sqrt()
    }

    #[inline(always)]
    pub fn sub(self, o: Self) -> Self {
        Self { re: self.re - o.re, im: self.im - o.im }
    }

    #[inline(always)]
    pub fn mul(self, o: Self) -> Self {
        Self {
            re: self.re * o.re - self.im * o.im,
            im: self.re * o.im + self.im * o.re,
        }
    }

    #[inline(always)]
    pub fn scale(self, s: f32) -> Self {
        Self { re: self.re * s, im: self.im * s }
    }
}

/// ETSI EN 300 392-2 Table 5.1: dibit `(b0, b1)` -> phase shift `Δφ` (radians).
#[inline]
pub fn dibit_to_dphi(b0: u8, b1: u8) -> f32 {
    match (b0 & 1, b1 & 1) {
        (0, 0) => FRAC_PI_4,
        (0, 1) => 3.0 * FRAC_PI_4,
        (1, 0) => -FRAC_PI_4,
        _ => -3.0 * FRAC_PI_4,
    }
}

/// Designs a Root-Raised-Cosine (RRC) matched filter impulse response normalized to unit energy.
pub fn rrc_taps(sps: f32, alpha: f32, span_syms: usize) -> Vec<f32> {
    let half = ((span_syms as f32 * sps) / 2.0).round() as isize;
    let len = (2 * half + 1) as usize;
    let mut taps = Vec::with_capacity(len);
    for k in -half..=half {
        let t = (k as f32) / sps;
        let h = if t.abs() < 1e-6 {
            1.0 - alpha + 4.0 * alpha / PI
        } else if (4.0 * alpha * t).abs().sub_abs_one() < 1e-4 {
            let a = (1.0 + 2.0 / PI) * (PI / (4.0 * alpha)).sin();
            let b = (1.0 - 2.0 / PI) * (PI / (4.0 * alpha)).cos();
            (alpha / 2.0f32.sqrt()) * (a + b)
        } else {
            let num = (PI * t * (1.0 - alpha)).sin()
                + 4.0 * alpha * t * (PI * t * (1.0 + alpha)).cos();
            let den = PI * t * (1.0 - (4.0 * alpha * t).powi(2));
            num / den
        };
        taps.push(h);
    }
    let energy: f32 = taps.iter().map(|v| v * v).sum::<f32>().sqrt();
    if energy > 0.0 {
        for v in &mut taps {
            *v /= energy;
        }
    }
    taps
}

trait SubAbsOne {
    fn sub_abs_one(self) -> f32;
}
impl SubAbsOne for f32 {
    #[inline]
    fn sub_abs_one(self) -> f32 {
        (self - 1.0).abs()
    }
}

/// Simple complex frequency mixer + integer decimator (used by the offline CLI
/// when ingesting wideband IQ like 2.4 MSPS SigMF recordings directly).
pub struct SimpleChannelizer {
    phase: f64,
    phase_step: f64,
    decim: usize,
    taps: Vec<f32>,
    ring: Vec<C32>,
    pos: usize,
    count: usize,
}

impl SimpleChannelizer {
    pub fn new(in_rate: f32, out_rate: f32, offset_hz: f32) -> Self {
        let decim = (in_rate / out_rate).round().max(1.0) as usize;
        let cutoff = (out_rate * 0.42) / in_rate;
        let num_taps = (decim * 8 + 1).min(255) | 1;
        let half = (num_taps / 2) as isize;
        let mut taps = Vec::with_capacity(num_taps);
        for k in -half..=half {
            let x = k as f32;
            let sinc = if k == 0 {
                2.0 * cutoff
            } else {
                (2.0 * PI * cutoff * x).sin() / (PI * x)
            };
            // Hamming window
            let w = 0.54 + 0.46 * (PI * x / (half.max(1) as f32)).cos();
            taps.push(sinc * w);
        }
        let sum: f32 = taps.iter().sum();
        if sum > 0.0 {
            for t in &mut taps {
                *t /= sum;
            }
        }
        let phase_step = -2.0 * std::f64::consts::PI * (offset_hz as f64) / (in_rate as f64);
        Self {
            phase: 0.0,
            phase_step,
            decim,
            ring: vec![C32::default(); num_taps],
            taps,
            pos: 0,
            count: 0,
        }
    }

    pub fn out_rate(&self, in_rate: f32) -> f32 {
        in_rate / (self.decim as f32)
    }

    pub fn process(&mut self, input: &[C32], out: &mut Vec<C32>) {
        if self.decim == 1 && self.phase_step.abs() < 1e-12 {
            out.extend_from_slice(input);
            return;
        }
        let n = self.taps.len();
        for &s in input {
            let mixed = if self.phase_step.abs() > 1e-12 {
                let (sn, cs) = (self.phase as f32).sin_cos();
                self.phase += self.phase_step;
                if self.phase > std::f64::consts::TAU || self.phase < -std::f64::consts::TAU {
                    self.phase %= std::f64::consts::TAU;
                }
                s.mul(C32::new(cs, sn))
            } else {
                s
            };
            self.ring[self.pos] = mixed;
            self.pos = (self.pos + 1) % n;
            self.count += 1;
            if self.count >= self.decim {
                self.count = 0;
                let mut acc = C32::default();
                let mut idx = self.pos;
                for &tap in &self.taps {
                    let v = self.ring[idx];
                    acc.re += v.re * tap;
                    acc.im += v.im * tap;
                    idx += 1;
                    if idx == n {
                        idx = 0;
                    }
                }
                out.push(acc);
            }
        }
    }
}

/// Streaming π/4-DQPSK demodulator with RRC matched filter, Gardner timing recovery,
/// and 4th-power non-data-aided fine frequency offset tracker (AFC).
pub struct Pi4DqpskDemod {
    sps: f32,
    taps: Vec<f32>,
    fir_buf: Vec<C32>,
    fir_pos: usize,

    // Interpolator sample history (holds filtered samples)
    hist: Vec<C32>,
    mu: f32,          // fractional sample offset into `hist`
    omega: f32,       // tracked samples/symbol (~sps)
    omega_min: f32,
    omega_max: f32,
    prev_sym: C32,

    // Fine CFO tracker (radians per symbol, tracked via arg(-d^4)/4)
    cfo_rad: f32,
    cfo_max: f32,

    // RMS amplitude tracker for well-scaled LLRs
    avg_mag: f32,
}

impl Pi4DqpskDemod {
    pub fn new(sample_rate: f32) -> Self {
        let sps = (sample_rate / SYMBOL_RATE).max(2.0);
        let taps = rrc_taps(sps, RRC_ALPHA, 10);
        let n = taps.len();
        Self {
            sps,
            taps,
            fir_buf: vec![C32::default(); n],
            fir_pos: 0,
            hist: Vec::with_capacity(2048),
            mu: sps,
            omega: sps,
            omega_min: sps * 0.995,
            omega_max: sps * 1.005,
            prev_sym: C32::new(1.0, 0.0),
            cfo_rad: 0.0,
            cfo_max: 0.72, // ~2.0 kHz fine CFO pull-in range at 18 ksym/s
            avg_mag: 1.0,
        }
    }

    pub fn cfo_hz(&self) -> f32 {
        self.cfo_rad * SYMBOL_RATE / (2.0 * PI)
    }

    #[inline]
    fn fir_push(&mut self, sample: C32) -> C32 {
        let n = self.taps.len();
        self.fir_buf[self.fir_pos] = sample;
        self.fir_pos += 1;
        if self.fir_pos == n {
            self.fir_pos = 0;
        }
        let mut acc = C32::default();
        let mut idx = self.fir_pos;
        for &tap in &self.taps {
            let v = self.fir_buf[idx];
            acc.re += v.re * tap;
            acc.im += v.im * tap;
            idx += 1;
            if idx == n {
                idx = 0;
            }
        }
        acc
    }

    #[inline]
    fn interp(buf: &[C32], pos: f32) -> C32 {
        let i = pos.floor() as usize;
        let f = pos - (i as f32);
        // 4-point cubic Catmull-Rom interpolation when interior samples exist, else linear.
        if i >= 1 && i + 2 < buf.len() {
            let p0 = buf[i - 1];
            let p1 = buf[i];
            let p2 = buf[i + 1];
            let p3 = buf[i + 2];
            let f2 = f * f;
            let f3 = f2 * f;
            let c0 = -0.5 * f3 + f2 - 0.5 * f;
            let c1 = 1.5 * f3 - 2.5 * f2 + 1.0;
            let c2 = -1.5 * f3 + 2.0 * f2 + 0.5 * f;
            let c3 = 0.5 * f3 - 0.5 * f2;
            C32::new(
                c0 * p0.re + c1 * p1.re + c2 * p2.re + c3 * p3.re,
                c0 * p0.im + c1 * p1.im + c2 * p2.im + c3 * p3.im,
            )
        } else if i + 1 < buf.len() {
            let a = buf[i];
            let b = buf[i + 1];
            C32::new(a.re + f * (b.re - a.re), a.im + f * (b.im - a.im))
        } else {
            buf[buf.len() - 1]
        }
    }

    /// Processes a block of complex baseband IQ and appends soft bits `(b0_llr, b1_llr)` to `soft_bits`.
    pub fn process(&mut self, iq: &[C32], soft_bits: &mut Vec<f32>) {
        self.hist.reserve(iq.len());
        for &s in iq {
            let y = self.fir_push(s);
            self.hist.push(y);
        }

        // Gardner TED + 4th-power AFC loop gains (critically damped for TETRA).
        const TIMING_KP: f32 = 0.04;
        const TIMING_KI: f32 = 0.0005;
        const CFO_GAIN: f32 = 0.015;

        let margin = 3.0;
        while self.mu + self.omega + margin < self.hist.len() as f32 {
            let mid_pos = self.mu + 0.5 * self.omega;
            let sym_pos = self.mu + self.omega;
            let mid = Self::interp(&self.hist, mid_pos);
            let sym = Self::interp(&self.hist, sym_pos);

            // Normalize energy for scale-invariant Gardner error.
            let mag = sym.norm();
            if mag > 1e-6 {
                self.avg_mag = 0.98 * self.avg_mag + 0.02 * mag;
            }
            let norm = self.avg_mag.max(1e-6);
            let sym_n = sym.scale(1.0 / norm);
            let prev_n = self.prev_sym.scale(1.0 / norm);
            let mid_n = mid.scale(1.0 / norm);

            // Gardner Timing Error Detector: Re{ mid * conj(prev - sym) }
            // Positive when sampling early -> advance mu/omega; negative when late -> retard.
            let diff = prev_n.sub(sym_n);
            let ted = (mid_n.re * diff.re + mid_n.im * diff.im).clamp(-1.0, 1.0);

            self.omega = (self.omega + TIMING_KI * ted).clamp(self.omega_min, self.omega_max);
            self.mu += self.omega + TIMING_KP * ted;

            // Differential symbol d = sym * conj(prev), rotated by -cfo_rad.
            let d_raw = sym_n.mul(prev_n.conj());
            let d = d_raw.mul(C32::from_polar(1.0, -self.cfo_rad));

            // 4th-power fine CFO error: for ideal ±π/4, ±3π/4, d^4 lies on the negative real axis.
            let d2 = d.mul(d);
            let d4 = d2.mul(d2);
            if d4.norm_sq() > 1e-4 {
                let err = (-d4.im).atan2(-d4.re) * 0.25;
                self.cfo_rad = (self.cfo_rad + CFO_GAIN * err).clamp(-self.cfo_max, self.cfo_max);
            }

            // Gray-coded π/4-DQPSK soft LLRs (ETSI Table 5.1):
            // b0 = 0 iff Im(d) > 0; b1 = 0 iff Re(d) > 0.
            let scale = 1.0 / d.norm().max(0.25);
            soft_bits.push((d.im * scale).clamp(-2.0, 2.0));
            soft_bits.push((d.re * scale).clamp(-2.0, 2.0));

            self.prev_sym = sym;
        }

        // Discard already-consumed samples while keeping 4 samples of cubic-interp lookbehind.
        let drop_n = (self.mu.floor() as isize - 4).max(0) as usize;
        if drop_n > 0 && drop_n < self.hist.len() {
            self.hist.drain(0..drop_n);
            self.mu -= drop_n as f32;
        }
        let _ = self.sps;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pi4_dqpsk_recovers_bits_with_cfo_and_timing_offset() {
        // Generate 600 known dibits modulated with π/4-DQPSK + RRC at 72 kS/s,
        // apply +800 Hz carrier offset and a fractional sample shift, then demodulate.
        let sps = 4usize;
        let taps = rrc_taps(sps as f32, RRC_ALPHA, 10);
        let bits: Vec<u8> = (0..1200).map(|i| ((i * 29 + 7) & 1) as u8).collect();
        let mut syms = Vec::with_capacity(bits.len() / 2 + 1);
        let mut phase = 0.0f32;
        syms.push(C32::from_polar(1.0, phase));
        for ch in bits.chunks_exact(2) {
            phase += dibit_to_dphi(ch[0], ch[1]);
            syms.push(C32::from_polar(1.0, phase));
        }

        // Upsample by 4 and RRC pulse-shape.
        let mut up = vec![C32::default(); syms.len() * sps + taps.len()];
        for (i, &s) in syms.iter().enumerate() {
            up[i * sps] = s.scale(sps as f32);
        }
        let mut tx = vec![C32::default(); up.len()];
        for i in 0..up.len() {
            let mut acc = C32::default();
            for (k, &h) in taps.iter().enumerate() {
                if i >= k {
                    acc.re += up[i - k].re * h;
                    acc.im += up[i - k].im * h;
                }
            }
            tx[i] = acc;
        }

        // Apply +800 Hz CFO and drop 1 sample (fractional timing offset).
        let cfo_hz = 800.0f32;
        let rx: Vec<C32> = tx[1..]
            .iter()
            .enumerate()
            .map(|(n, &s)| {
                let th = 2.0 * PI * cfo_hz * (n as f32) / DEFAULT_IQ_RATE;
                s.mul(C32::from_polar(1.0, th))
            })
            .collect();

        let mut demod = Pi4DqpskDemod::new(DEFAULT_IQ_RATE);
        let mut soft = Vec::new();
        demod.process(&rx, &mut soft);
        let hard: Vec<u8> = soft.iter().map(|&v| if v >= 0.0 { 0 } else { 1 }).collect();

        // Find the transmitted bit stream after loop convergence (bits[400..1000]).
        let needle = &bits[400..600];
        let found = hard.windows(needle.len()).position(|w| {
            let errs = w.iter().zip(needle.iter()).filter(|(a, b)| a != b).count();
            errs <= 2
        });
        assert!(found.is_some(), "Failed to recover π/4-DQPSK bits under +800 Hz CFO (tracked CFO = {:.1} Hz)", demod.cfo_hz());
        assert!((demod.cfo_hz() - cfo_hz).abs() < 80.0, "CFO tracker should converge near 800 Hz, got {:.1}", demod.cfo_hz());
    }
}
