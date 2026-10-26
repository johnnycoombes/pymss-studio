//! Effect-only AUv2 adapter. Allocation and format negotiation happen while stopped.

use crate::ffi::Instance;
use crate::offline::OfflineState;
use soundcraft_dsp::{Plugin, PluginEditor, PluginInfo, Transport};

/// A loaded synchronous Audio Unit effect with a main-thread Cocoa editor/controller.
pub struct AuPlugin {
    inst: Instance,
    info: &'static PluginInfo,
    ids: Vec<u32>,
    values: Vec<f32>,
    pending: Vec<Option<f32>>,
    channels: usize,
    latency: usize,
    tail: usize,
    reset_failure: Option<i32>,
    offline: OfflineState,
}

impl AuPlugin {
    pub(crate) fn new(inst: Instance, info: &'static PluginInfo) -> Self {
        let ids: Vec<u32> = info
            .params
            .iter()
            .map(|p| p.id.parse().unwrap_or(u32::MAX))
            .collect();
        let values = info
            .params
            .iter()
            .zip(&ids)
            .map(|(p, &id)| inst.get_param(id).map_or(p.default, |v| p.clamp(v)))
            .collect();
        Self {
            pending: vec![None; ids.len()],
            inst,
            info,
            ids,
            values,
            channels: 2,
            latency: 0,
            tail: 0,
            reset_failure: None,
            offline: OfflineState::default(),
        }
    }

    /// Changes the host's render context on this same unit. DSP must be stopped. Effects without
    /// the optional OfflineRender property retain their existing processing implementation.
    pub fn set_offline(&mut self, offline: bool) -> Result<(), String> {
        self.offline.check()?;
        if self.offline.requested() == offline {
            return Ok(());
        }
        self.flush_pending()?;
        let result = self.offline.set(&mut self.inst, offline);
        // A failed rollback leaves the unit deliberately unprepared. Preserve the original
        // error instead of querying timing/parameter properties on that failed configuration.
        if result.is_ok() || self.inst.is_ready() {
            self.refresh_values();
            self.refresh_latency_control();
        }
        if result.is_ok() {
            self.reset_failure = None;
        }
        result
    }

    /// The host's requested context; an AU may omit the optional optimization property.
    pub fn is_offline(&self) -> bool {
        self.offline.requested()
    }

    fn refresh_values(&mut self) {
        for ((slot, p), &id) in self.values.iter_mut().zip(self.info.params).zip(&self.ids) {
            if let Some(v) = self.inst.get_param(id) {
                *slot = p.clamp(v);
            }
        }
    }

    /// Negotiates the main bus and allocates all render storage. Call while processing is stopped.
    pub fn try_prepare(
        &mut self,
        sample_rate: f32,
        max_block: usize,
        channels: usize,
    ) -> Result<(), String> {
        self.offline.check()?;
        if !sample_rate.is_finite()
            || !(8_000.0..=384_000.0).contains(&sample_rate)
            || max_block == 0
            || max_block > crate::ffi::MAX_SLICE
            || !(1..=2).contains(&channels)
        {
            return Err("invalid Audio Unit processing configuration".into());
        }
        self.inst
            .configure(f64::from(sample_rate), max_block, channels)?;
        self.reset_failure = None;
        self.channels = channels;
        for index in 0..self.ids.len() {
            // Initializing an AU does not replace its parameter state. Re-sending defaults
            // here could overwrite a ClassInfo value that the unit cannot expose before init.
            let Some(value) = self.pending[index] else {
                continue;
            };
            let id = self.ids[index];
            if !self.inst.set_param(id, value) {
                return Err(format!(
                    "Audio Unit rejected parameter {id} after configuration"
                ));
            }
            self.values[index] = value;
        }
        self.pending.fill(None);
        self.refresh_values();
        self.refresh_latency_control();
        Ok(())
    }

    /// Returns cached latency, or a changed sentinel when a property notification requires
    /// control-thread renegotiation. This method never calls into the Audio Unit.
    pub fn refresh_latency(&mut self) -> usize {
        if self.inst.latency_dirty() {
            self.latency ^ 1
        } else {
            self.latency
        }
    }

    /// Queries latency and tail while processing is stopped (no audio-thread property calls).
    pub fn refresh_latency_control(&mut self) -> usize {
        self.inst.clear_latency_dirty();
        self.latency = self.inst.latency();
        self.tail = self.inst.tail();
        self.latency
    }

    /// Queues one UI-converted value; the worker applies it at a block boundary.
    pub fn queue_normalized(&mut self, id: &str, normalized: f64, plain: f32) -> bool {
        if !normalized.is_finite() || !(0.0..=1.0).contains(&normalized) || !plain.is_finite() {
            return false;
        }
        let Some(i) = self.info.params.iter().position(|p| p.id == id) else {
            return false;
        };
        self.pending[i] = Some(self.info.params[i].clamp(plain));
        true
    }

    /// Applies queued parameters without allocation on success. Errors remain visible to the host.
    pub fn flush_pending(&mut self) -> Result<(), String> {
        for i in 0..self.pending.len() {
            let Some(value) = self.pending[i] else {
                continue;
            };
            if !self.inst.set_param(self.ids[i], value) {
                return Err(format!("Audio Unit rejected parameter {}", self.ids[i]));
            }
            self.values[i] = value;
            self.pending[i] = None;
        }
        Ok(())
    }

    /// Processes the configured planar main bus. Render failures never fall back to dry audio.
    pub fn try_process(&mut self, io: &mut [Vec<f32>], frames: usize) -> Result<(), String> {
        if let Some(status) = self.reset_failure {
            return Err(format!("AudioUnitReset failed ({status})"));
        }
        if frames == 0 {
            return Ok(());
        }
        if !self.inst.is_ready() {
            return Err("Audio Unit is not prepared".into());
        }
        if io.len() < self.channels || io.iter().take(self.channels).any(|ch| ch.len() < frames) {
            return Err("Audio Unit received an incomplete audio block".into());
        }
        self.flush_pending()?;
        let layout = self.inst.channel_layout();
        let mut start = 0;
        while start < frames {
            let n = (frames - start).min(self.inst.max_frames());
            let end = start + n;
            for c in 0..layout.inputs {
                let source = &io[c.min(self.channels - 1)][start..end];
                let second = (layout.inputs == 1 && self.channels == 2).then(|| &io[1][start..end]);
                let Some(dst) = self.inst.input_mut(c) else {
                    return Err("Audio Unit input buffer is unavailable".into());
                };
                crate::channels::copy_channel(&mut dst[..n], source, second)?;
            }
            self.inst
                .render(n)
                .map_err(|code| format!("AudioUnitRender failed ({code})"))?;
            for (c, dst) in io.iter_mut().take(self.channels).enumerate() {
                let dst = &mut dst[start..end];
                if self.channels == 1 && layout.outputs == 2 {
                    let (Some(left), Some(right)) =
                        (self.inst.output(0, n), self.inst.output(1, n))
                    else {
                        return Err("Audio Unit output buffer is unavailable".into());
                    };
                    crate::channels::copy_channel(dst, left, Some(right))?;
                } else {
                    let Some(src) = self.inst.output(c.min(layout.outputs - 1), n) else {
                        return Err("Audio Unit output buffer is unavailable".into());
                    };
                    crate::channels::copy_channel(dst, src, None)?;
                }
                if dst.iter().any(|sample| !sample.is_finite()) {
                    return Err("Audio Unit returned non-finite audio".into());
                }
            }
            start = end;
        }
        Ok(())
    }
}

impl Plugin for AuPlugin {
    fn info(&self) -> &'static PluginInfo {
        self.info
    }
    fn prepare(&mut self, rate: f32, block: usize, channels: usize) {
        let _ = self.try_prepare(rate, block, channels);
    }
    fn reset(&mut self) {
        self.reset_failure = self.inst.reset().err();
    }
    fn set_param(&mut self, id: &str, value: f32) -> bool {
        let Some(param) = self.info.param(id) else {
            return false;
        };
        self.queue_normalized(
            id,
            f64::from(param.to_normalized(value)),
            param.clamp(value),
        )
    }
    fn param(&self, id: &str) -> Option<f32> {
        let index = self.info.params.iter().position(|p| p.id == id)?;
        self.pending[index].or_else(|| self.values.get(index).copied())
    }
    fn process(&mut self, io: &mut [Vec<f32>], frames: usize) {
        if self.try_process(io, frames).is_err() {
            for channel in io.iter_mut().take(self.channels) {
                let count = frames.min(channel.len());
                channel[..count].fill(0.0);
            }
        }
    }
    fn latency(&self) -> usize {
        self.latency
    }
    fn tail_samples(&self) -> usize {
        self.tail
    }
    fn set_transport(&mut self, transport: &Transport) {
        self.inst.set_transport(transport);
    }
    fn load_state(&mut self, data: &[u8]) -> bool {
        if !self.inst.load_state(data) {
            return false;
        }
        self.pending.fill(None);
        self.refresh_values();
        self.refresh_latency_control();
        true
    }
    fn editor(&mut self) -> Option<Box<dyn PluginEditor>> {
        Some(self.inst.editor(self.info, &self.values))
    }
    fn open_editor(&mut self) -> Result<(), String> {
        self.inst.editor(self.info, &self.values).open()
    }
    fn close_editor(&mut self) {
        self.inst.close_editor();
    }
    fn state_reader(&mut self) -> Option<Box<dyn soundcraft_dsp::PluginStateReader>> {
        let link = self.inst.state_link();
        Some(Box::new(move || link.save_state()))
    }
}
