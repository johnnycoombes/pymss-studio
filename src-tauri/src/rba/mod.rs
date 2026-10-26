//! Tempo and key analysis, vendored from [rba](https://github.com/SUC-DriverOld/rba)
//! (GPL-3.0, compatible with this project's AGPL-3.0).
//!
//! `detect`, `fft`, `impact`, `keyfinder` and `thumbnail` follow the upstream
//! algorithms, with cancellation checks and bounded audio storage. This file
//! replaces upstream's `analyze.rs` / `audio.rs` so decoding uses the bundled
//! FFmpeg and the CLI-only dependencies (clap, rayon, indicatif...) stay out.

mod detect;
mod fft;
mod impact;
mod keyfinder;
mod thumbnail;

use std::io::{Read, Result as IoResult};
use std::path::Path;
use std::process::{ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

/// FFT / thumbnail frame size (upstream spec §2).
pub const FFT_SIZE: usize = 256;
/// The original analyzer hard-codes a 44100 Hz mono client format.
pub const SAMPLE_RATE: u32 = 44_100;
const MAX_DURATION_SECONDS: usize = 30 * 60;
const MAX_SAMPLES: usize = SAMPLE_RATE as usize * MAX_DURATION_SECONDS;
const DURATION_ERROR: &str = "Audio exceeds the 30-minute analysis limit";

pub(super) fn check_cancelled(cancelled: &AtomicBool) -> Result<(), String> {
    if cancelled.load(Ordering::Relaxed) {
        Err("Analysis cancelled".into())
    } else {
        Ok(())
    }
}

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Debug, Clone)]
pub struct Analysis {
    pub bpm: f64,
    /// libKeyFinder key name, e.g. `"G major"`, or `"silence"`.
    pub key: String,
    pub duration: f64,
}

/// Mirrors upstream `analyze_samples` (with the onset divisor fix of commit 8944846).
#[cfg(test)]
pub fn analyze_samples(samples: &[f32], sample_rate: u32) -> Analysis {
    analyze_samples_cancellable(samples, sample_rate, &AtomicBool::new(false), true)
        .expect("uncancelled sample analysis")
}

fn analyze_samples_cancellable(
    samples: &[f32],
    sample_rate: u32,
    cancelled: &AtomicBool,
    include_key: bool,
) -> Result<Analysis, String> {
    check_cancelled(cancelled)?;
    let total_samples = samples.len() as i64;
    let spec = thumbnail::spec_thumbnail_cancellable(samples, sample_rate, cancelled)?;
    let mut peak = spec.clone();
    thumbnail::peak_hold(&mut peak);
    let ip = impact::build_impact_points_cancellable(&peak, total_samples, 4, cancelled)?;
    let base_bpm = detect::detect_base_bpm_cancellable(&ip, cancelled)?;
    let smooth = thumbnail::smoothed(&spec);
    let ipc =
        impact::build_impact_points_classic_cancellable(&smooth, total_samples, 4, cancelled)?;
    let folded_bpm = detect::fold_up_bpm(&ipc, base_bpm);
    // Clips under 30 s are governed by their duration (upstream spec §7).
    let is_short = sample_rate != 0 && 30 * sample_rate as i64 > total_samples;
    let bpm = if base_bpm <= 0.0 {
        0.0
    } else if is_short {
        detect::short_clip_bpm_update(total_samples, sample_rate, folded_bpm) as f64
    } else {
        detect::refine_bpm(&ip, folded_bpm, sample_rate).0
    };
    check_cancelled(cancelled)?;
    let key = if include_key {
        keyfinder::detect_key_cancellable(samples, sample_rate, cancelled)?
    } else {
        String::new()
    };
    let duration = if sample_rate == 0 {
        0.0
    } else {
        total_samples as f64 / sample_rate as f64
    };
    check_cancelled(cancelled)?;
    Ok(Analysis { bpm, key, duration })
}

pub fn analyze_file(
    ffmpeg: &Path,
    ffprobe: &Path,
    path: &Path,
    cancelled: &AtomicBool,
    include_key: bool,
) -> Result<Analysis, String> {
    let samples = decode(ffmpeg, ffprobe, path, cancelled)?;
    if samples.len() < FFT_SIZE {
        return Err("No decodable audio in the input file".into());
    }
    analyze_samples_cancellable(&samples, SAMPLE_RATE, cancelled, include_key)
}

fn hidden_command(program: &Path) -> Command {
    #[allow(unused_mut)]
    let mut command = Command::new(program);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
}

/// Decode to mono f32 at [`SAMPLE_RATE`]. Downmixing is done here as a plain
/// average because FFmpeg's `-ac 1` is power preserving, and the onset detector
/// uses absolute thresholds (see upstream `audio.rs`).
fn decode(
    ffmpeg: &Path,
    ffprobe: &Path,
    path: &Path,
    cancelled: &AtomicBool,
) -> Result<Vec<f32>, String> {
    let channels = probe_channels(ffprobe, path, cancelled)?;
    let mut command = hidden_command(ffmpeg);
    command
        .args(["-v", "error", "-nostdin", "-i"])
        .arg(path)
        .args(["-map", "0:a:0", "-vn", "-ac"])
        .arg(channels.to_string())
        .arg("-ar")
        .arg(SAMPLE_RATE.to_string())
        .args(["-f", "f32le", "-acodec", "pcm_f32le", "-"]);
    run_command(command, cancelled, |stdout| {
        read_pcm(stdout, channels, MAX_SAMPLES, cancelled)
    })
}

fn probe_channels(ffprobe: &Path, path: &Path, cancelled: &AtomicBool) -> Result<usize, String> {
    let mut command = hidden_command(ffprobe);
    command
        .args([
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels,duration:format=duration",
            "-of",
            "json",
        ])
        .arg(path);
    let output = run_command(command, cancelled, |stdout| {
        let mut bytes = Vec::new();
        stdout
            .take(4097)
            .read_to_end(&mut bytes)
            .map_err(|error| error.to_string())?;
        if bytes.len() > 4096 {
            return Err("Audio metadata exceeds the analysis limit".into());
        }
        Ok(bytes)
    })?;
    parse_probe(&output)
}

fn parse_probe(output: &[u8]) -> Result<usize, String> {
    let value: serde_json::Value =
        serde_json::from_slice(output).map_err(|error| error.to_string())?;
    let stream = &value["streams"][0];
    let duration = stream["duration"]
        .as_str()
        .or_else(|| value["format"]["duration"].as_str())
        .and_then(|text| text.parse::<f64>().ok());
    if duration.is_some_and(|seconds| seconds > MAX_DURATION_SECONDS as f64) {
        return Err(DURATION_ERROR.into());
    }
    stream["channels"]
        .as_u64()
        .filter(|n| (1..=64).contains(n))
        .map(|n| n as usize)
        .ok_or_else(|| "No supported audio stream was found".into())
}

fn read_pcm(
    mut reader: impl Read,
    channels: usize,
    limit: usize,
    cancelled: &AtomicBool,
) -> Result<Vec<f32>, String> {
    let frame_bytes = channels * 4;
    let mut buffer = vec![0u8; 64 * 1024 + frame_bytes];
    let mut carried = 0;
    let mut samples = Vec::new();
    loop {
        check_cancelled(cancelled)?;
        let count = reader
            .read(&mut buffer[carried..64 * 1024])
            .map_err(|error| format!("Unable to read decoded audio: {error}"))?;
        if count == 0 {
            break;
        }
        let available = carried + count;
        let complete = available / frame_bytes * frame_bytes;
        let frames = complete / frame_bytes;
        if frames > limit - samples.len() {
            return Err(DURATION_ERROR.into());
        }
        // Reserve exact blocks rather than doubling a potentially large PCM allocation.
        if samples.capacity() - samples.len() < frames {
            let growth = (SAMPLE_RATE as usize * 10).min(limit - samples.len());
            samples
                .try_reserve_exact(growth.max(frames))
                .map_err(|_| "Insufficient memory for audio analysis".to_string())?;
        }
        for frame in buffer[..complete].chunks_exact(frame_bytes) {
            let mono = frame
                .chunks_exact(4)
                .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
                .sum::<f32>()
                / channels as f32;
            samples.push(if mono.is_finite() { mono } else { 0.0 });
        }
        carried = available - complete;
        buffer.copy_within(complete..available, 0);
    }
    if carried != 0 {
        return Err("Decoded audio ended with an incomplete sample frame".into());
    }
    Ok(samples)
}

fn drain_stderr(mut reader: impl Read) -> IoResult<Vec<u8>> {
    let mut tail = Vec::new();
    let mut buffer = [0u8; 4096];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            return Ok(tail);
        }
        tail.extend_from_slice(&buffer[..count]);
        if tail.len() > 8192 {
            tail.drain(..tail.len() - 8192);
        }
    }
}

/// Read pipes concurrently so neither FFmpeg diagnostics nor PCM can deadlock.
/// Every exit path terminates/reaps the child before joining its pipe readers.
fn run_command<T: Send>(
    mut command: Command,
    cancelled: &AtomicBool,
    read: impl FnOnce(ChildStdout) -> Result<T, String> + Send,
) -> Result<T, String> {
    check_cancelled(cancelled)?;
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("Unable to start audio decoder: {error}"))?;
    let stdout = child.stdout.take().expect("piped decoder stdout");
    let stderr = child.stderr.take().expect("piped decoder stderr");
    std::thread::scope(|scope| {
        let mut reader = Some(scope.spawn(move || read(stdout)));
        let errors = scope.spawn(move || drain_stderr(stderr));
        let mut output = None;
        let status = loop {
            if let Err(error) = check_cancelled(cancelled) {
                break Err(error);
            }
            if reader.as_ref().is_some_and(|reader| reader.is_finished()) {
                let result = reader
                    .take()
                    .unwrap()
                    .join()
                    .unwrap_or_else(|_| Err("Audio reader failed".into()));
                match result {
                    Ok(value) => output = Some(value),
                    Err(error) => break Err(error),
                }
            }
            match child.try_wait() {
                Ok(Some(status)) => break Ok(status),
                Ok(None) => std::thread::sleep(Duration::from_millis(10)),
                Err(error) => break Err(format!("Unable to wait for audio decoder: {error}")),
            }
        };
        if status.is_err() {
            let _ = child.kill();
        }
        let _ = child.wait();
        let remaining = reader.map(|reader| {
            reader
                .join()
                .unwrap_or_else(|_| Err("Audio reader failed".into()))
        });
        let diagnostics = errors.join().ok().and_then(Result::ok).unwrap_or_default();
        let status = status?;
        if !status.success() {
            let detail = String::from_utf8_lossy(&diagnostics).trim().to_string();
            return Err(if detail.is_empty() {
                "FFmpeg failed to decode the audio".into()
            } else {
                detail
            });
        }
        check_cancelled(cancelled)?;
        match remaining {
            Some(result) => result,
            None => output.ok_or_else(|| "Audio decoder returned no data".into()),
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, Write};
    use std::sync::Arc;
    use std::time::Instant;

    #[test]
    fn pcm_reader_handles_split_frames_and_preserves_plain_channel_average() {
        struct SmallReads(Cursor<Vec<u8>>);
        impl Read for SmallReads {
            fn read(&mut self, bytes: &mut [u8]) -> IoResult<usize> {
                let count = bytes.len().min(3);
                self.0.read(&mut bytes[..count])
            }
        }
        let bytes = [1.0f32, -0.5, 0.25, 0.75]
            .into_iter()
            .flat_map(f32::to_le_bytes)
            .collect();
        let samples = read_pcm(
            SmallReads(Cursor::new(bytes)),
            2,
            2,
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(samples, [0.25, 0.5]);
    }

    #[test]
    fn pcm_reader_rejects_one_frame_beyond_limit_instead_of_truncating() {
        let cancelled = AtomicBool::new(false);
        assert_eq!(
            read_pcm(Cursor::new(vec![0u8; 8]), 1, 2, &cancelled)
                .unwrap()
                .len(),
            2
        );
        assert_eq!(
            read_pcm(Cursor::new(vec![0u8; 12]), 1, 2, &cancelled).unwrap_err(),
            DURATION_ERROR
        );
    }

    #[test]
    fn metadata_duration_boundary_is_checked_before_decoding() {
        assert_eq!(
            parse_probe(br#"{"streams":[{"channels":2,"duration":"1800"}]}"#).unwrap(),
            2
        );
        assert_eq!(
            parse_probe(br#"{"streams":[{"channels":2,"duration":"1800.001"}]}"#).unwrap_err(),
            DURATION_ERROR
        );
        assert_eq!(
            parse_probe(br#"{"streams":[{"channels":1}],"format":{"duration":"1801"}}"#)
                .unwrap_err(),
            DURATION_ERROR
        );
        assert_eq!(parse_probe(br#"{"streams":[{"channels":1}]}"#).unwrap(), 1);
    }

    #[test]
    fn ffmpeg_decode_preserves_channel_average_and_silent_bpm() {
        let ffmpeg = Path::new("ffmpeg");
        let ffprobe = Path::new("ffprobe");
        for tool in [ffmpeg, ffprobe] {
            if hidden_command(tool).arg("-version").output().is_err() {
                eprintln!(
                    "Skipping FFmpeg integration: {} is not available",
                    tool.display()
                );
                return;
            }
        }
        let input =
            std::env::temp_dir().join(format!("pymss-rba-stereo-{}-音频.wav", std::process::id()));
        let output = hidden_command(ffmpeg)
            .args([
                "-v",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=0.1*sin(2*PI*440*t)|-0.1*sin(2*PI*440*t):s=44100:d=1",
                "-c:a",
                "pcm_f32le",
                "-y",
            ])
            .arg(&input)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let result = analyze_file(ffmpeg, ffprobe, &input, &AtomicBool::new(false), true);
        let _ = std::fs::remove_file(input);
        let result = result.unwrap();
        assert_eq!(result.bpm, 0.0);
        assert_eq!(result.key, "silence");
        assert!((result.duration - 1.0).abs() < 1e-6);
    }

    #[test]
    fn analysis_and_each_expensive_stage_respect_cancellation() {
        let cancelled = AtomicBool::new(true);
        let samples = vec![0.0; SAMPLE_RATE as usize];
        assert!(analyze_samples_cancellable(&samples, SAMPLE_RATE, &cancelled, true).is_err());
        assert!(thumbnail::spec_thumbnail_cancellable(&samples, SAMPLE_RATE, &cancelled).is_err());
        assert!(keyfinder::detect_key_cancellable(&samples, SAMPLE_RATE, &cancelled).is_err());
        assert!(
            detect::detect_base_bpm_cancellable(&impact::ImpactPoints::default(), &cancelled)
                .is_err()
        );
    }

    #[test]
    fn key_analysis_can_be_cancelled_after_it_starts() {
        let cancelled = Arc::new(AtomicBool::new(false));
        let flag = cancelled.clone();
        let samples = vec![0.25; SAMPLE_RATE as usize * 120];
        let setter = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(10));
            flag.store(true, Ordering::SeqCst);
        });
        let result = keyfinder::detect_key_cancellable(&samples, SAMPLE_RATE, &cancelled);
        setter.join().unwrap();
        assert_eq!(result.unwrap_err(), "Analysis cancelled");
    }

    // A child test process supplies blocking pipes without requiring FFmpeg.
    #[test]
    fn decoder_process_fixture() {
        let Ok(mode) = std::env::var("PYMSS_RBA_FIXTURE_MODE") else {
            return;
        };
        if mode == "blocked" {
            std::io::stderr().write_all(&vec![b'x'; 20_000]).unwrap();
            std::fs::write(
                std::env::var_os("PYMSS_RBA_FIXTURE_READY").unwrap(),
                b"ready",
            )
            .unwrap();
            std::thread::sleep(Duration::from_secs(10));
        } else if mode == "overflow" {
            let mut stdout = std::io::stdout().lock();
            loop {
                if stdout.write_all(&[0u8; 4096]).is_err() {
                    break;
                }
            }
        }
    }

    fn fixture_command(mode: &str) -> Command {
        let mut command = hidden_command(&std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "rba::tests::decoder_process_fixture",
                "--nocapture",
            ])
            .env("PYMSS_RBA_FIXTURE_MODE", mode);
        command
    }

    #[test]
    fn cancellation_terminates_and_reaps_a_child_with_blocked_stdout() {
        let ready = std::env::temp_dir().join(format!("pymss-rba-ready-{}", std::process::id()));
        let _ = std::fs::remove_file(&ready);
        let mut command = fixture_command("blocked");
        command.env("PYMSS_RBA_FIXTURE_READY", &ready);
        let cancelled = Arc::new(AtomicBool::new(false));
        let flag = cancelled.clone();
        let marker = ready.clone();
        let setter = std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(3);
            while !marker.is_file() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(5));
            }
            let started = marker.is_file();
            flag.store(true, Ordering::SeqCst);
            started
        });
        let started = Instant::now();
        let result = run_command(command, &cancelled, |mut stdout| {
            let mut output = Vec::new();
            stdout
                .read_to_end(&mut output)
                .map_err(|error| error.to_string())?;
            Ok(output)
        });
        assert!(
            setter.join().unwrap(),
            "child did not reach its blocking read"
        );
        let _ = std::fs::remove_file(ready);
        assert_eq!(result.unwrap_err(), "Analysis cancelled");
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn exceeding_pcm_limit_terminates_the_decoder() {
        let cancelled = AtomicBool::new(false);
        let started = Instant::now();
        let result = run_command(fixture_command("overflow"), &cancelled, |stdout| {
            read_pcm(stdout, 1, 32, &cancelled)
        });
        assert_eq!(result.unwrap_err(), DURATION_ERROR);
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn click_track_reports_its_tempo() {
        // 120 BPM clicks for 40 s, long enough to skip the short-clip rule.
        let rate = SAMPLE_RATE as usize;
        let mut samples = vec![0f32; rate * 40];
        let period = rate / 2;
        for start in (0..samples.len()).step_by(period) {
            for (offset, sample) in samples[start..].iter_mut().take(400).enumerate() {
                *sample = (offset as f32 * 0.35).sin() * (1.0 - offset as f32 / 400.0);
            }
        }
        let analysis = analyze_samples(&samples, SAMPLE_RATE);
        assert!((analysis.bpm - 120.0).abs() < 1.0, "bpm = {}", analysis.bpm);
        assert!((analysis.duration - 40.0).abs() < 1e-6);
    }

    #[test]
    fn silence_is_reported_as_silence() {
        let analysis = analyze_samples(&vec![0f32; SAMPLE_RATE as usize * 5], SAMPLE_RATE);
        assert_eq!(analysis.bpm, 0.0);
        assert_eq!(analysis.key, "silence");
    }

    #[test]
    fn rhythmic_short_audio_keeps_the_duration_fallback() {
        let rate = SAMPLE_RATE as usize;
        let mut samples = vec![0.0f32; rate * 12];
        for start in (0..samples.len()).step_by(rate / 2) {
            for (offset, sample) in samples[start..].iter_mut().take(400).enumerate() {
                *sample = (offset as f32 * 0.35).sin() * (1.0 - offset as f32 / 400.0);
            }
        }
        let analysis = analyze_samples(&samples, SAMPLE_RATE);
        assert!((analysis.bpm - 160.0).abs() < 0.01, "bpm = {}", analysis.bpm);
    }

    #[test]
    fn bpm_only_analysis_keeps_tempo_without_producing_a_key() {
        let rate = SAMPLE_RATE as usize;
        let mut samples = vec![0.0f32; rate * 12];
        for start in (0..samples.len()).step_by(rate / 2) {
            for (offset, sample) in samples[start..].iter_mut().take(400).enumerate() {
                *sample = (offset as f32 * 0.35).sin() * (1.0 - offset as f32 / 400.0);
            }
        }
        let analysis = analyze_samples_cancellable(&samples, SAMPLE_RATE, &AtomicBool::new(false), false).unwrap();
        assert!((analysis.bpm - 160.0).abs() < 0.01);
        assert!(analysis.key.is_empty());
    }
}
