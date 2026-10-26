//! Stream the project unchanged and retain only the audible portion of its added plugin tail.
use std::io::{Seek, Write};

// An amplitude of 1e-6 is -120 dBFS. Quiet gaps are retained if a later echo is audible.
const SILENCE_PEAK: f32 = 1e-6;

pub(crate) struct TailWriter<W: Write + Seek> {
    writer: hound::WavWriter<W>,
    program_frames: u64,
    seen: u64,
    written: u64,
    padding: usize,
    pending: Vec<[f32; 2]>,
    has_tail: bool,
}

pub(crate) struct TailSummary {
    pub frames: u64,
    pub audible_at_limit: bool,
}

fn write_stereo<W: Write + Seek>(
    writer: &mut hound::WavWriter<W>,
    frame: [f32; 2],
) -> Result<(), String> {
    writer
        .write_sample(frame[0])
        .map_err(|error| error.to_string())?;
    writer
        .write_sample(frame[1])
        .map_err(|error| error.to_string())
}

impl<W: Write + Seek> TailWriter<W> {
    pub fn new(writer: hound::WavWriter<W>, program_frames: u64, sample_rate: u32) -> Self {
        Self {
            writer,
            program_frames,
            seen: 0,
            written: 0,
            padding: (sample_rate as usize).div_ceil(20),
            pending: Vec::new(),
            has_tail: false,
        }
    }

    pub fn write_frame(&mut self, frame: [f32; 2]) -> Result<(), String> {
        let program = self.seen < self.program_frames;
        self.seen += 1;
        if program || frame[0].abs().max(frame[1].abs()) >= SILENCE_PEAK {
            for quiet in self.pending.drain(..) {
                write_stereo(&mut self.writer, quiet)?;
                self.written += 1;
            }
            write_stereo(&mut self.writer, frame)?;
            self.written += 1;
            self.has_tail |= !program;
        } else {
            // The renderer bounds the tail to 30 seconds. Never stop at a quiet gap:
            // a delayed echo may still arrive later within that processing budget.
            self.pending.push(frame);
        }
        Ok(())
    }

    pub fn finish(mut self) -> Result<TailSummary, String> {
        let audible_at_limit = self.has_tail && self.pending.len() < self.padding;
        if self.has_tail {
            for quiet in self.pending.drain(..).take(self.padding) {
                write_stereo(&mut self.writer, quiet)?;
                self.written += 1;
            }
        }
        // Finalize using the committed sample count, so the WAV header and reported
        // duration agree with the actual file rather than its plugin-declared budget.
        self.writer.finalize().map_err(|error| error.to_string())?;
        Ok(TailSummary {
            frames: self.written,
            audible_at_limit,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn render(program: usize, frames: &[[f32; 2]]) -> (TailSummary, Vec<[f32; 2]>) {
        let mut bytes = Cursor::new(Vec::new());
        let writer = hound::WavWriter::new(
            &mut bytes,
            hound::WavSpec {
                channels: 2,
                sample_rate: 8000,
                bits_per_sample: 32,
                sample_format: hound::SampleFormat::Float,
            },
        )
        .unwrap();
        let mut tail = TailWriter::new(writer, program as u64, 8000);
        for frame in frames {
            tail.write_frame(*frame).unwrap();
        }
        let result = tail.finish().unwrap();
        bytes.set_position(0);
        let reader = hound::WavReader::new(bytes).unwrap();
        assert_eq!(u64::from(reader.duration()), result.frames);
        let samples: Vec<f32> = reader.into_samples::<f32>().map(Result::unwrap).collect();
        (
            result,
            samples
                .chunks_exact(2)
                .map(|frame| [frame[0], frame[1]])
                .collect(),
        )
    }

    #[test]
    fn silent_tail_is_removed_but_the_entire_original_timeline_is_preserved() {
        let mut frames = vec![[0.0; 2]; 8000 + 240_000];
        frames[0] = [0.5, -0.25];
        frames[7999] = [1e-8, -1e-9];
        let (result, output) = render(8000, &frames);
        assert_eq!(output, frames[..8000]);
        assert!(!result.audible_at_limit);
        let (_, output) = render(8000, &vec![[0.0; 2]; 9000]);
        assert_eq!(output.len(), 8000);
    }

    #[test]
    fn delayed_echo_after_a_long_quiet_gap_and_a_right_only_tail_are_preserved() {
        let mut frames = vec![[1e-8, -1e-8]; 3000];
        frames[0] = [0.5, 0.25];
        frames[100] = [0.01, 0.0];
        frames[2000] = [0.0, -0.001];
        let (result, output) = render(10, &frames);
        assert_eq!(output.len(), 2001 + 400);
        assert_eq!(
            output,
            frames[..2401],
            "quiet gaps and the final 50ms retain their original samples"
        );
        assert!(!result.audible_at_limit);
    }

    #[test]
    fn quiet_noise_floor_does_not_add_tail_and_audible_tail_at_the_budget_is_flagged() {
        let (_, output) = render(10, &vec![[1e-8, -1e-8]; 5000]);
        assert_eq!(output.len(), 10);
        let (result, output) = render(10, &vec![[0.0, -1e-6]; 5000]);
        assert_eq!(output.len(), 5000);
        assert!(result.audible_at_limit);
        let (_, output) = render(0, &vec![[0.0; 2]; 128]);
        assert!(output.is_empty());
    }
}
