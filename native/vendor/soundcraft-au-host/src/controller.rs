//! Generic parameter conversion without calls into the Audio Unit.

use soundcraft_dsp::{PluginEditor, PluginInfo};

pub(crate) fn initial_v3_view_size(frame: (f64, f64), preferred: (f64, f64)) -> (f64, f64) {
    let valid = |(width, height): (f64, f64)| {
        width.is_finite()
            && height.is_finite()
            && (1.0..=16384.0).contains(&width)
            && (1.0..=16384.0).contains(&height)
    };
    if valid(preferred) {
        preferred
    } else if valid(frame) {
        frame
    } else {
        (640.0, 480.0)
    }
}

pub(crate) struct ParameterController {
    pub(crate) info: &'static PluginInfo,
}

impl PluginEditor for ParameterController {
    fn open(&mut self) -> Result<(), String> {
        Err("Native Audio Unit editors are not available".into())
    }
    fn close(&mut self) {}
    fn is_open(&self) -> bool {
        false
    }
    fn idle(&mut self) -> Vec<(String, f32)> {
        Vec::new()
    }
    fn normalize(&self, id: &str, value: f32) -> Option<f64> {
        let param = self.info.param(id)?;
        value
            .is_finite()
            .then(|| f64::from(param.to_normalized(value)))
    }
    fn value_text(&mut self, id: &str, value: f32) -> Option<String> {
        let param = self.info.param(id)?;
        value.is_finite().then(|| param.format(value))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use soundcraft_dsp::{Category, ParamInfo, Taper, Unit};

    static INFO: PluginInfo = PluginInfo {
        id: "au:aufx:test:host",
        name: "Filter",
        short_name: "Filter",
        category: Category::Eq,
        params: &[
            ParamInfo {
                id: "0",
                name: "Frequency",
                min: 20.0,
                max: 20_000.0,
                default: 200.0,
                unit: Unit::Hz,
                taper: Taper::Log,
                choices: &[],
            },
            ParamInfo {
                id: "1",
                name: "Enabled",
                min: 0.0,
                max: 1.0,
                default: 1.0,
                unit: Unit::Toggle,
                taper: Taper::Linear,
                choices: &[],
            },
            ParamInfo {
                id: "2",
                name: "Gain",
                min: -24.0,
                max: 24.0,
                default: 0.0,
                unit: Unit::Db,
                taper: Taper::Linear,
                choices: &[],
            },
        ],
        is_instrument: false,
        audiosuite: false,
    };

    #[test]
    fn remote_view_initial_size_uses_preference_then_frame_then_bounded_default() {
        assert_eq!(
            initial_v3_view_size((0.0, 0.0), (420.0, 300.0)),
            (420.0, 300.0)
        );
        assert_eq!(
            initial_v3_view_size((400.0, 200.0), (0.0, 0.0)),
            (400.0, 200.0)
        );
        assert_eq!(
            initial_v3_view_size((0.0, 0.0), (f64::NAN, 100.0)),
            (640.0, 480.0)
        );
        assert_eq!(
            initial_v3_view_size((f64::INFINITY, 100.0), (50_000.0, 300.0)),
            (640.0, 480.0)
        );
    }

    #[test]
    fn preserves_plain_units_and_logarithmic_travel() {
        let mut controller = ParameterController { info: &INFO };
        assert!((controller.normalize("0", 200.0).unwrap() - 1.0 / 3.0).abs() < 0.000_001);
        assert_eq!(controller.normalize("2", 0.0), Some(0.5));
        assert_eq!(controller.normalize("0", -1.0), Some(0.0));
        assert_eq!(controller.normalize("0", 30_000.0), Some(1.0));
        assert_eq!(controller.normalize("1", 0.6), Some(1.0));
        assert_eq!(
            controller.value_text("2", -12.0).as_deref(),
            Some("-12.0 dB")
        );
    }

    #[test]
    fn rejects_unknown_and_nonfinite_values() {
        let mut controller = ParameterController { info: &INFO };
        assert_eq!(controller.normalize("missing", 0.0), None);
        for value in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            assert_eq!(controller.normalize("0", value), None);
            assert_eq!(controller.value_text("0", value), None);
        }
    }

    #[test]
    fn controller_does_not_imply_a_native_window() {
        let mut controller = ParameterController { info: &INFO };
        assert!(controller.open().is_err());
        assert!(!controller.is_open());
        controller.set_param("0", 1_000.0);
        assert!(controller.idle().is_empty());
        controller.close();
    }
}
