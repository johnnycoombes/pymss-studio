//! Stereo-linked sample-peak limiter. Keep the algorithm in sync with the Web Audio processor.
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", default)]
pub struct MasterLimiterSpec {
    pub enabled: bool,
    pub gain_db: f64,
    pub ceiling_db: f64,
    pub attack_ms: f64,
    pub release_ms: f64,
}

impl Default for MasterLimiterSpec {
    fn default() -> Self {
        Self {
            enabled: false,
            gain_db: 0.0,
            ceiling_db: 0.0,
            attack_ms: 3.0,
            release_ms: 250.0,
        }
    }
}

impl MasterLimiterSpec {
    pub fn validate(&self) -> Result<(), String> {
        for (value, min, max, name) in [
            (self.gain_db, 0.0, 30.0, "gain"),
            (self.ceiling_db, -30.0, 0.0, "ceiling"),
            (self.attack_ms, 0.1, 100.0, "attack"),
            (self.release_ms, 5.0, 2000.0, "release"),
        ] {
            if !value.is_finite() || value < min || value > max {
                return Err(format!(
                    "Master limiter {name} must be between {min} and {max}"
                ));
            }
        }
        Ok(())
    }

    pub fn latency(&self, rate: u32) -> usize {
        if self.enabled {
            (self.attack_ms * f64::from(rate) / 1000.0).ceil() as usize
        } else {
            0
        }
    }
}

pub struct MasterLimiter {
    spec: MasterLimiterSpec,
    rate: u32,
    delay: Vec<[f32; 2]>,
    cursor: usize,
    peaks: Vec<(u64, f64)>,
    head: usize,
    count: usize,
    index: u64,
    envelope: f64,
    gain: f64,
    ceiling: f64,
    attack: f64,
    release: f64,
}

impl MasterLimiter {
    pub fn new(spec: MasterLimiterSpec, rate: u32) -> Self {
        let mut result = Self {
            spec: MasterLimiterSpec::default(),
            rate,
            delay: Vec::new(),
            cursor: 0,
            peaks: Vec::new(),
            head: 0,
            count: 0,
            index: 0,
            envelope: 1.0,
            gain: 1.0,
            ceiling: 1.0,
            attack: 0.0,
            release: 0.0,
        };
        result.configure(spec);
        result
    }

    pub fn latency(&self) -> usize {
        self.spec.latency(self.rate)
    }

    /// Called by the control queue only, outside the audio render callback.
    pub fn configure(&mut self, spec: MasterLimiterSpec) {
        let latency = spec.latency(self.rate);
        if self.delay.len() != latency || self.peaks.is_empty() {
            self.delay = vec![[0.0; 2]; latency];
            self.peaks = vec![(0, 0.0); latency + 2];
            self.reset();
        }
        self.gain = 10.0_f64.powf(spec.gain_db / 20.0);
        self.ceiling = 10.0_f64.powf(spec.ceiling_db / 20.0);
        self.attack = (-1000.0_f64.ln() / latency.max(1) as f64).exp();
        self.release = (-1.0 / (spec.release_ms * f64::from(self.rate) / 1000.0)).exp();
        self.spec = spec;
    }

    pub fn reset(&mut self) {
        self.delay.fill([0.0; 2]);
        self.cursor = 0;
        self.head = 0;
        self.count = 0;
        self.index = 0;
        self.envelope = 1.0;
    }

    pub fn process(&mut self, mix: &mut [Vec<f32>], frames: usize) {
        if !self.spec.enabled {
            return;
        }
        let latency = self.delay.len();
        let capacity = self.peaks.len();
        for i in 0..frames {
            let input = [mix[0][i], mix[1][i]];
            let peak = f64::from(input[0].abs().max(input[1].abs()));
            // Monotonic queue gives the peak over the delayed frame and its lookahead.
            while self.count > 0 && self.peaks[self.head].0 + (latency as u64) < self.index {
                self.head = (self.head + 1) % capacity;
                self.count -= 1;
            }
            while self.count > 0 && self.peaks[(self.head + self.count - 1) % capacity].1 <= peak {
                self.count -= 1;
            }
            self.peaks[(self.head + self.count) % capacity] = (self.index, peak);
            self.count += 1;
            self.index += 1;
            let maximum = self.peaks[self.head].1 * self.gain;
            let target = if maximum > self.ceiling {
                self.ceiling / maximum
            } else {
                1.0
            };
            let coefficient = if target < self.envelope {
                self.attack
            } else {
                self.release
            };
            self.envelope = target + coefficient * (self.envelope - target);
            let delayed = self.delay[self.cursor];
            self.delay[self.cursor] = input;
            self.cursor = (self.cursor + 1) % latency;
            let delayed_peak = f64::from(delayed[0].abs().max(delayed[1].abs())) * self.gain;
            // Shared safety gain preserves the stereo image and enforces the sample ceiling.
            let safe = if delayed_peak > self.ceiling {
                self.ceiling / delayed_peak
            } else {
                1.0
            };
            let gain = self.gain * self.envelope.min(safe);
            mix[0][i] = (f64::from(delayed[0]) * gain) as f32;
            mix[1][i] = (f64::from(delayed[1]) * gain) as f32;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn render(input: &[f32], spec: MasterLimiterSpec, block: usize) -> (Vec<Vec<f32>>, usize) {
        let mut limiter = MasterLimiter::new(spec, 8000);
        let latency = limiter.latency();
        let mut result = vec![Vec::new(), Vec::new()];
        let padded: Vec<f32> = input
            .iter()
            .copied()
            .chain(std::iter::repeat_n(0.0, latency))
            .collect();
        for chunk in padded.chunks(block) {
            let mut mix = vec![
                chunk.to_vec(),
                chunk.iter().map(|value| -value * 0.25).collect(),
            ];
            limiter.process(&mut mix, chunk.len());
            for (output, channel) in result.iter_mut().zip(mix) {
                output.extend(channel);
            }
        }
        (result, latency)
    }

    #[test]
    fn bypass_is_exact_and_enabled_limiting_preserves_stereo_and_final_frame() {
        let input: Vec<f32> = (0..5000)
            .map(|i| if i % 137 == 0 || i == 4999 { -4.0 } else { 0.1 })
            .collect();
        let (dry, latency) = render(
            &input,
            MasterLimiterSpec {
                gain_db: 30.0,
                ceiling_db: -30.0,
                ..Default::default()
            },
            127,
        );
        assert_eq!(latency, 0);
        assert_eq!(dry[0], input);
        for attack_ms in [0.1, 3.0, 100.0] {
            let spec = MasterLimiterSpec {
                enabled: true,
                gain_db: 30.0,
                ceiling_db: -12.0,
                attack_ms,
                ..Default::default()
            };
            let (mix, latency) = render(&input, spec.clone(), 127);
            assert_eq!(latency, (attack_ms * 8.0).ceil() as usize);
            assert!(mix[0][..latency].iter().all(|value| *value == 0.0));
            assert!(
                mix[0]
                    .iter()
                    .all(|value| value.abs() <= 10.0_f32.powf(-12.0 / 20.0) + 1e-7)
            );
            for (left, right) in mix[0].iter().zip(&mix[1]) {
                assert!((left * 0.25 + right).abs() < 1e-7);
            }
            assert!(mix[0].last().unwrap().abs() > 0.0);
            assert_eq!(mix, render(&input, spec, 1).0);
        }
    }

    #[test]
    fn attack_release_and_reset_affect_the_envelope() {
        let mut input = vec![0.1; 5000];
        input[300] = 10.0;
        let (fast, a) = render(
            &input,
            MasterLimiterSpec {
                enabled: true,
                attack_ms: 1.0,
                release_ms: 5.0,
                ..Default::default()
            },
            128,
        );
        let (slow, b) = render(
            &input,
            MasterLimiterSpec {
                enabled: true,
                attack_ms: 30.0,
                release_ms: 2000.0,
                ..Default::default()
            },
            128,
        );
        assert!(slow[0][b + 280] < fast[0][a + 280]);
        assert!(slow[0][b + 1000] < fast[0][a + 1000]);
        let mut limiter = MasterLimiter::new(
            MasterLimiterSpec {
                enabled: true,
                ..Default::default()
            },
            8000,
        );
        limiter.process(&mut vec![vec![10.0; 128]; 2], 128);
        limiter.reset();
        let mut silence = vec![vec![0.0; 128]; 2];
        limiter.process(&mut silence, 128);
        assert!(silence.iter().flatten().all(|value| *value == 0.0));
        limiter.configure(MasterLimiterSpec {
            enabled: true,
            attack_ms: 100.0,
            ..Default::default()
        });
        assert_eq!(limiter.latency(), 800);
        assert_eq!(limiter.envelope, 1.0);
    }

    #[test]
    fn invalid_values_are_rejected_even_while_disabled() {
        for (key, value) in [
            ("gainDb", -1.0),
            ("ceilingDb", 0.1),
            ("attackMs", 0.0),
            ("releaseMs", 2001.0),
        ] {
            let mut data = serde_json::to_value(MasterLimiterSpec::default()).unwrap();
            data[key] = value.into();
            let spec: MasterLimiterSpec = serde_json::from_value(data).unwrap();
            assert!(spec.validate().is_err());
        }
        let mut spec = MasterLimiterSpec::default();
        spec.gain_db = f64::NAN;
        assert!(spec.validate().is_err());
    }
}
