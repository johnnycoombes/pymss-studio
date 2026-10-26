use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{FromSample, Sample, SampleFormat, SizedSample, Stream, StreamConfig};
use rtrb::{Consumer, Producer, RingBuffer};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

#[derive(Clone, Copy, Default, Debug)]
pub(crate) struct Frame {
    pub samples: [f32; 2],
    pub position: u64,
    pub generation: u64,
    pub last: bool,
}

#[derive(Default)]
pub(crate) struct Clock {
    active: AtomicU64,
    cursor: AtomicU64,
    ready: AtomicU64,
    pub generation: AtomicU64,
    pub underruns: AtomicU64,
    pub device_failed: AtomicBool,
    pub recovering: AtomicBool,
}

impl Clock {
    const FRAME_MASK: u64 = (1 << 48) - 1;
    fn cursor(generation: u64, frame: u64) -> u64 {
        (generation << 48) | (frame & Self::FRAME_MASK)
    }
    pub fn position(&self) -> u64 {
        self.cursor.load(Ordering::Acquire) & Self::FRAME_MASK
    }
    pub fn is_playing(&self) -> bool {
        self.active.load(Ordering::Acquire) != 0
    }
    pub fn transition(&self, frame: u64, playing: bool) -> u64 {
        let generation = self.generation.fetch_add(1, Ordering::AcqRel) + 1;
        self.cursor
            .store(Self::cursor(generation, frame), Ordering::Release);
        self.ready.store(0, Ordering::Release);
        self.active
            .store(if playing { generation } else { 0 }, Ordering::Release);
        generation
    }
    pub fn stop(&self) {
        self.active.store(0, Ordering::Release);
    }
    pub fn prefilled(&self, generation: u64) {
        self.ready.store(generation, Ordering::Release);
    }
    pub fn finish(&self, generation: u64) {
        let _ = self
            .active
            .compare_exchange(generation, 0, Ordering::AcqRel, Ordering::Acquire);
    }
    /// Invalidate the failed epoch without overwriting a newer explicit seek,
    /// pause, or play. A callback that already popped an old frame also loses
    /// its cursor commit once the epoch is replaced.
    pub fn begin_recovery(&self, failed_generation: u64) {
        self.recovering.store(true, Ordering::Release);
        let replacement = self.generation.fetch_add(1, Ordering::AcqRel) + 1;
        let _ = self
            .cursor
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |cursor| {
                ((cursor >> 48) == (failed_generation & 0xffff))
                    .then_some(Self::cursor(replacement, cursor & Self::FRAME_MASK))
            });
        let _ =
            self.active
                .compare_exchange(failed_generation, 0, Ordering::AcqRel, Ordering::Acquire);
        let _ =
            self.ready
                .compare_exchange(failed_generation, 0, Ordering::AcqRel, Ordering::Acquire);
    }
    fn commit(&self, frame: Frame) {
        let _ = self
            .cursor
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |cursor| {
                ((cursor >> 48) == (frame.generation & 0xffff))
                    .then_some(Self::cursor(frame.generation, frame.position))
            });
        if frame.last {
            self.finish(frame.generation);
        }
    }
}

pub(crate) struct Output {
    _stream: Stream,
    pub sample_rate: u32,
}

pub(crate) fn audio_ring() -> (Producer<Frame>, Consumer<Frame>) {
    RingBuffer::new(4096)
}

fn fill<T: Sample + FromSample<f32>>(
    data: &mut [T],
    channels: usize,
    consumer: &mut Consumer<Frame>,
    clock: &Clock,
) {
    let mut missing = false;
    for frame in data.chunks_mut(channels) {
        let mut value = None;
        let active = clock.active.load(Ordering::Acquire);
        // Old queued audio must be drained even while a new generation is prebuffering.
        while consumer.peek().is_ok_and(|next| next.generation != active) {
            let _ = consumer.pop();
        }
        if active != 0 && clock.ready.load(Ordering::Acquire) == active {
            while let Ok(next) = consumer.pop() {
                let generation = clock.active.load(Ordering::Acquire);
                if next.generation == generation {
                    value = Some(next);
                    break;
                }
                if generation == 0 {
                    break;
                }
            }
            if value.is_none() {
                missing = true;
            }
        }
        let samples = value.map(|v| v.samples).unwrap_or([0.0; 2]);
        for (channel, sample) in frame.iter_mut().enumerate() {
            let v = if channels == 1 {
                (samples[0] + samples[1]) * 0.5
            } else if channel < 2 {
                samples[channel]
            } else {
                0.0
            };
            *sample = T::from_sample(v.clamp(-1.0, 1.0));
        }
        if let Some(v) = value {
            clock.commit(v);
        }
    }
    if missing {
        clock.underruns.fetch_add(1, Ordering::Relaxed);
    }
}

fn build<T: SizedSample + FromSample<f32>>(
    device: &cpal::Device,
    config: StreamConfig,
    mut consumer: Consumer<Frame>,
    clock: Arc<Clock>,
) -> Result<Stream, String> {
    let channels = usize::from(config.channels);
    let errors = Arc::clone(&clock);
    device
        .build_output_stream(
            config,
            move |data: &mut [T], _| fill(data, channels, &mut consumer, &clock),
            move |_error| {
                errors.device_failed.store(true, Ordering::Release);
            },
            None,
        )
        .map_err(|e| format!("Unable to open audio output: {e}"))
}

impl Output {
    pub fn open(consumer: Consumer<Frame>, clock: Arc<Clock>) -> Result<Self, String> {
        let device = cpal::default_host()
            .default_output_device()
            .ok_or("No audio output device is available")?;
        let default = device.default_output_config().map_err(|e| e.to_string())?;
        let config = StreamConfig {
            channels: default.channels(),
            sample_rate: default.sample_rate(),
            buffer_size: cpal::BufferSize::Default,
        };
        if config.channels == 0
            || config.channels > 64
            || !(8000..=192000).contains(&config.sample_rate)
        {
            return Err("Unsupported audio output configuration".into());
        }
        let sample_rate = config.sample_rate;
        let stream = match default.sample_format() {
            SampleFormat::F32 => build::<f32>(&device, config, consumer, clock),
            SampleFormat::F64 => build::<f64>(&device, config, consumer, clock),
            SampleFormat::I16 => build::<i16>(&device, config, consumer, clock),
            SampleFormat::I32 => build::<i32>(&device, config, consumer, clock),
            SampleFormat::U16 => build::<u16>(&device, config, consumer, clock),
            _ => Err("Unsupported audio output sample format".into()),
        }?;
        stream
            .play()
            .map_err(|e| format!("Unable to start audio output: {e}"))?;
        Ok(Self {
            _stream: stream,
            sample_rate,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn automatic_recovery_freezes_only_its_failed_epoch_and_rejects_old_callback_commits() {
        let clock = Clock::default();
        let failed = clock.transition(100, true);
        clock.begin_recovery(failed);
        clock.commit(Frame {
            generation: failed,
            position: 200,
            last: true,
            ..Frame::default()
        });
        assert_eq!(clock.position(), 100);
        assert!(!clock.is_playing());
        assert!(clock.recovering.load(Ordering::Acquire));
    }

    #[test]
    fn old_recovery_notification_cannot_undo_new_pause_seek_or_play() {
        for playing in [false, true] {
            let clock = Clock::default();
            let failed = clock.transition(100, true);
            let newest = clock.transition(800, playing);
            clock.prefilled(newest);
            // Deterministically deliver the old worker notification after the
            // user's newer control, then an already-popped old callback frame.
            clock.begin_recovery(failed);
            clock.commit(Frame {
                generation: failed,
                position: 201,
                last: true,
                ..Frame::default()
            });
            assert_eq!(clock.position(), 800);
            assert_eq!(clock.is_playing(), playing);
            assert_eq!(clock.ready.load(Ordering::Acquire), newest);
        }
    }
    #[test]
    fn old_completion_cannot_overwrite_a_new_seek_or_stop_new_playback() {
        let clock = Clock::default();
        let old = clock.transition(0, true);
        let current = clock.transition(800, true);
        clock.commit(Frame {
            generation: old,
            position: 90,
            last: true,
            ..Frame::default()
        });
        assert_eq!(clock.position(), 800);
        assert!(clock.is_playing());
        clock.commit(Frame {
            generation: current,
            position: 801,
            ..Frame::default()
        });
        assert_eq!(clock.position(), 801);
    }
    #[test]
    fn stale_generation_and_paused_frames_never_reach_output() {
        let (mut tx, mut rx) = audio_ring();
        let clock = Clock::default();
        clock.transition(0, true);
        clock.transition(0, true);
        clock.prefilled(2);
        tx.push(Frame {
            samples: [0.8, 0.8],
            generation: 1,
            ..Frame::default()
        })
        .unwrap();
        tx.push(Frame {
            samples: [0.2, 0.3],
            generation: 2,
            position: 6,
            last: true,
        })
        .unwrap();
        let mut samples = [1.0f32; 4];
        fill(&mut samples, 2, &mut rx, &clock);
        assert_eq!(samples, [0.2, 0.3, 0.0, 0.0]);
        assert_eq!(clock.position(), 6);
        assert!(!clock.is_playing());
    }

    #[test]
    fn seek_prebuffer_discards_only_stale_audio_and_does_not_count_startup_as_underrun() {
        let (mut tx, mut rx) = audio_ring();
        let clock = Clock::default();
        clock.transition(0, true);
        let current = clock.transition(50, true);
        tx.push(Frame {
            generation: 1,
            samples: [0.9; 2],
            ..Frame::default()
        })
        .unwrap();
        tx.push(Frame {
            generation: current,
            samples: [0.3; 2],
            position: 51,
            ..Frame::default()
        })
        .unwrap();
        let mut data = [1.0f32; 2];
        fill(&mut data, 2, &mut rx, &clock);
        assert_eq!(data, [0.0; 2]);
        assert_eq!(clock.underruns.load(Ordering::Relaxed), 0);
        clock.prefilled(current);
        fill(&mut data, 2, &mut rx, &clock);
        assert_eq!(data, [0.3; 2]);
        assert_eq!(clock.position(), 51);
    }
}
