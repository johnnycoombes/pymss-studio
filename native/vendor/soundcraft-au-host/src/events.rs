//! One coalescing slot per writable parameter. Neither DSP updates nor event callbacks allocate.
use soundcraft_dsp::{ParamInfo, PluginInfo};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

pub(crate) struct ParameterEvent {
    pub(crate) id: u32,
    pub(crate) info: &'static ParamInfo,
    native_value: AtomicU32,
    native_dirty: AtomicBool,
    host_dirty: AtomicBool,
}

pub(crate) struct ParameterEvents {
    entries: Vec<ParameterEvent>,
}

impl ParameterEvents {
    pub(crate) fn new(info: &'static PluginInfo, values: &[f32]) -> Self {
        let entries = info
            .params
            .iter()
            .take(4096)
            .enumerate()
            .filter_map(|(index, param)| {
                Some(ParameterEvent {
                    id: param.id.parse().ok()?,
                    info: param,
                    native_value: AtomicU32::new(
                        param
                            .clamp(values.get(index).copied().unwrap_or(param.default))
                            .to_bits(),
                    ),
                    native_dirty: AtomicBool::new(false),
                    host_dirty: AtomicBool::new(false),
                })
            })
            .collect();
        Self { entries }
    }
    pub(crate) fn entries(&self) -> &[ParameterEvent] {
        &self.entries
    }
    pub(crate) fn native_change(&self, id: u32, value: f32) {
        if !value.is_finite() {
            return;
        }
        if let Some(entry) = self.entries.iter().find(|entry| entry.id == id) {
            let value = entry.info.clamp(value).to_bits();
            entry.native_value.store(value, Ordering::Release);
            // The host may have changed the value since the last native callback. Let the
            // session deduplicate against its authoritative cache instead of a stale UI baseline.
            entry.native_dirty.store(true, Ordering::Release);
        }
    }
    pub(crate) fn applied_change(&self, id: u32) {
        if let Some(entry) = self.entries.iter().find(|entry| entry.id == id) {
            entry.host_dirty.store(true, Ordering::Release);
        }
    }
    pub(crate) fn drain_native(&self) -> Vec<(String, f32)> {
        self.entries
            .iter()
            .filter_map(|entry| {
                entry.native_dirty.swap(false, Ordering::AcqRel).then(|| {
                    (
                        entry.info.id.to_owned(),
                        f32::from_bits(entry.native_value.load(Ordering::Acquire)),
                    )
                })
            })
            .collect()
    }
}

impl ParameterEvent {
    pub(crate) fn take_host_change(&self) -> bool {
        self.host_dirty.swap(false, Ordering::AcqRel)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use soundcraft_dsp::{Category, Taper, Unit};
    static INFO: PluginInfo = PluginInfo {
        id: "au:aufx:test:host",
        name: "Gain",
        short_name: "Gain",
        category: Category::Other,
        params: &[ParamInfo {
            id: "7",
            name: "Gain",
            min: -24.0,
            max: 24.0,
            default: 0.0,
            unit: Unit::Db,
            taper: Taper::Linear,
            choices: &[],
        }],
        is_instrument: false,
        audiosuite: false,
    };
    #[test]
    fn coalesces_edits_without_losing_the_last_value() {
        let events = ParameterEvents::new(&INFO, &[0.0]);
        for index in 0..100_000 {
            events.native_change(7, index as f32 / 100_000.0);
        }
        let values = events.drain_native();
        assert_eq!(values.len(), 1);
        assert_eq!(values[0], ("7".into(), 0.99999));
        assert!(events.drain_native().is_empty());
    }
    #[test]
    fn filters_unknown_and_nonfinite_notifications() {
        let events = ParameterEvents::new(&INFO, &[0.0]);
        events.native_change(999, 2.0);
        events.native_change(7, f32::NAN);
        assert!(events.drain_native().is_empty());
        events.native_change(7, 99.0);
        assert_eq!(events.drain_native(), vec![("7".into(), 24.0)]);
    }
    #[test]
    fn dsp_notifications_remain_separate_from_native_edits() {
        let events = ParameterEvents::new(&INFO, &[0.0]);
        events.applied_change(7);
        assert!(events.entries()[0].take_host_change());
        assert!(!events.entries()[0].take_host_change());
        assert!(events.drain_native().is_empty());
    }

    #[test]
    fn native_return_to_original_value_after_a_generic_edit_is_preserved() {
        let events = ParameterEvents::new(&INFO, &[0.0]);
        // The generic host changes the AU to 3.0, then its native view returns to 0.0.
        events.applied_change(7);
        events.native_change(7, 0.0);
        assert_eq!(events.drain_native(), vec![("7".into(), 0.0)]);
        assert!(events.entries()[0].take_host_change());
    }
}
