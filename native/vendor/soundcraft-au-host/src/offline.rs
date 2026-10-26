//! OfflineRender is optional for ordinary AU effects. Mode changes run only while DSP is stopped.

pub(crate) trait OfflineUnit {
    type Configuration: Copy;
    fn configuration(&self) -> Option<Self::Configuration>;
    fn stop(&mut self) -> Result<(), String>;
    fn supports_offline(&self) -> Result<bool, String>;
    fn write_offline(&mut self, offline: bool) -> Result<(), String>;
    fn restart(&mut self, configuration: Self::Configuration) -> Result<(), String>;
    fn invalidate(&mut self);
}

#[derive(Default)]
pub(crate) struct OfflineState {
    requested: bool,
    supported: bool,
    failure: Option<String>,
}

impl OfflineState {
    pub(crate) fn requested(&self) -> bool {
        self.requested
    }

    pub(crate) fn check(&self) -> Result<(), String> {
        self.failure.clone().map_or(Ok(()), Err)
    }

    pub(crate) fn set(
        &mut self,
        unit: &mut impl OfflineUnit,
        requested: bool,
    ) -> Result<(), String> {
        self.check()?;
        if self.requested == requested {
            return Ok(());
        }
        let configuration = unit.configuration();
        if configuration.is_some() {
            unit.stop()?;
        }
        let mut wrote = false;
        let result = (|| {
            let supported = unit.supports_offline()?;
            if !supported && self.supported {
                return Err("Audio Unit OfflineRender support changed unexpectedly".into());
            }
            self.supported |= supported;
            if supported {
                // Even a failed setter can have modified the unit. Roll back that property
                // before recovering its previous configuration instead of assuming no change.
                wrote = true;
                unit.write_offline(requested)?;
            }
            if let Some(configuration) = configuration {
                unit.restart(configuration)?;
            }
            Ok(())
        })();
        if let Err(error) = result {
            let recovered = (|| {
                unit.stop()?;
                if wrote {
                    unit.write_offline(self.requested)?;
                }
                if let Some(configuration) = configuration {
                    unit.restart(configuration)?;
                }
                Ok::<_, String>(())
            })();
            if let Err(recovery) = recovered {
                unit.invalidate();
                let message = format!(
                    "{error}; restoring the previous Audio Unit mode failed: {recovery}; reload the plugin"
                );
                self.failure = Some(message.clone());
                return Err(message);
            }
            return Err(error);
        }
        // This is the host's requested context. An AU without the optional property still
        // processes through the existing effect path, without pretending the property exists.
        self.requested = requested;
        Ok(())
    }
}

/// Only an explicit unknown property or a declared read-only property is optional absence.
/// Unexpected status codes, ABI sizes and contradictory setters remain real errors.
pub(crate) fn optional_property(status: i32, size: u32, writable: bool) -> Result<bool, String> {
    const INVALID_PROPERTY: i32 = -10879;
    if status == INVALID_PROPERTY {
        return Ok(false);
    }
    if status != 0 {
        return Err(format!("Cannot query Audio Unit OfflineRender ({status})"));
    }
    if size != std::mem::size_of::<u32>() as u32 {
        return Err("Audio Unit OfflineRender has an invalid property size".into());
    }
    Ok(writable)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::channels::ChannelLayout;
    use std::cell::RefCell;

    struct Unit {
        prepared: bool,
        supported: bool,
        mode: bool,
        fail_query: bool,
        fail_set: bool,
        fail_restart: usize,
        invalid: bool,
        calls: RefCell<Vec<&'static str>>,
        parameters: [f32; 2],
        class_info: Vec<u8>,
        editor: usize,
        layout: ChannelLayout,
    }
    impl Unit {
        fn new(prepared: bool, supported: bool) -> Self {
            Self {
                prepared,
                supported,
                mode: false,
                fail_query: false,
                fail_set: false,
                fail_restart: 0,
                invalid: false,
                calls: RefCell::new(Vec::new()),
                parameters: [0.25, 0.75],
                class_info: vec![1, 2, 3],
                editor: 42,
                layout: ChannelLayout {
                    inputs: 2,
                    outputs: 2,
                },
            }
        }
    }
    impl OfflineUnit for Unit {
        type Configuration = (u32, usize, ChannelLayout);
        fn configuration(&self) -> Option<Self::Configuration> {
            self.prepared.then_some((48_000, 256, self.layout))
        }
        fn stop(&mut self) -> Result<(), String> {
            if self.prepared {
                self.calls.borrow_mut().push("uninitialize");
                self.prepared = false;
            }
            Ok(())
        }
        fn supports_offline(&self) -> Result<bool, String> {
            assert!(!self.prepared);
            self.calls.borrow_mut().push("query");
            if self.fail_query {
                Err("property query failed".into())
            } else {
                Ok(self.supported)
            }
        }
        fn write_offline(&mut self, offline: bool) -> Result<(), String> {
            assert!(!self.prepared);
            self.calls
                .borrow_mut()
                .push(if offline { "offline" } else { "realtime" });
            self.mode = offline;
            // Some units rebuild their bus defaults while changing render mode. Restart must
            // restore the saved pair instead of reading this replacement configuration.
            self.layout = ChannelLayout {
                inputs: 2,
                outputs: 2,
            };
            if self.fail_set && offline {
                Err("property setter failed".into())
            } else {
                Ok(())
            }
        }
        fn restart(&mut self, configuration: Self::Configuration) -> Result<(), String> {
            assert!(!self.prepared);
            assert_eq!(configuration.0, 48_000);
            assert_eq!(configuration.1, 256);
            self.calls.borrow_mut().push("configure/initialize");
            if self.fail_restart > 0 {
                self.fail_restart -= 1;
                return Err("initialize failed".into());
            }
            self.prepared = true;
            self.layout = configuration.2;
            Ok(())
        }
        fn invalidate(&mut self) {
            self.prepared = false;
            self.invalid = true;
        }
    }

    #[test]
    fn initial_context_is_realtime_and_offline_can_be_set_before_prepare() {
        let mut state = OfflineState::default();
        let mut unit = Unit::new(false, true);
        assert!(!state.requested());
        state.set(&mut unit, false).unwrap();
        assert!(unit.calls.borrow().is_empty());
        state.set(&mut unit, true).unwrap();
        assert_eq!(&*unit.calls.borrow(), &["query", "offline"]);
        assert!(state.requested());
        assert!(unit.mode);
        assert!(!unit.prepared);
        unit.restart((48_000, 256, unit.layout)).unwrap();
        assert!(
            unit.mode,
            "Preparing the same unit must retain the previously set context"
        );
    }

    #[test]
    fn prepared_switch_reuses_configuration_and_preserves_parameters_state_and_editor() {
        let mut state = OfflineState::default();
        let mut unit = Unit::new(true, true);
        state.set(&mut unit, true).unwrap();
        state.set(&mut unit, false).unwrap();
        assert_eq!(
            &*unit.calls.borrow(),
            &[
                "uninitialize",
                "query",
                "offline",
                "configure/initialize",
                "uninitialize",
                "query",
                "realtime",
                "configure/initialize"
            ]
        );
        assert_eq!(unit.parameters, [0.25, 0.75]);
        assert_eq!(unit.class_info, [1, 2, 3]);
        assert_eq!(unit.editor, 42);
        assert!(unit.prepared);
        assert!(!state.requested());
    }

    #[test]
    fn missing_optional_property_keeps_the_existing_effect_path() {
        let mut state = OfflineState::default();
        let mut unit = Unit::new(true, false);
        state.set(&mut unit, true).unwrap();
        assert!(state.requested());
        assert!(!unit.mode);
        assert!(unit.prepared);
        assert_eq!(
            &*unit.calls.borrow(),
            &["uninitialize", "query", "configure/initialize"]
        );
    }

    #[test]
    fn asymmetric_layout_is_preserved_on_mode_switch_and_failed_switch_rollback() {
        for layout in [
            ChannelLayout {
                inputs: 1,
                outputs: 2,
            },
            ChannelLayout {
                inputs: 2,
                outputs: 1,
            },
        ] {
            let mut state = OfflineState::default();
            let mut unit = Unit::new(true, true);
            unit.layout = layout;
            state.set(&mut unit, true).unwrap();
            assert_eq!(unit.layout, layout);
            state.set(&mut unit, false).unwrap();
            assert_eq!(unit.layout, layout);
            unit.fail_restart = 1;
            assert!(state.set(&mut unit, true).is_err());
            assert!(!state.requested());
            assert!(unit.prepared);
            assert_eq!(unit.layout, layout);
            assert_eq!(unit.parameters, [0.25, 0.75]);
            assert_eq!(unit.class_info, [1, 2, 3]);
            assert_eq!(unit.editor, 42);
        }
    }

    #[test]
    fn setter_or_restart_errors_restore_the_previous_prepared_mode_without_committing() {
        for fail_set in [true, false] {
            let mut state = OfflineState::default();
            let mut unit = Unit::new(true, true);
            unit.fail_set = fail_set;
            unit.fail_restart = usize::from(!fail_set);
            assert!(state.set(&mut unit, true).is_err());
            assert!(!state.requested());
            assert!(!unit.mode);
            assert!(unit.prepared);
            assert!(!unit.invalid);
            assert_eq!(unit.parameters, [0.25, 0.75]);
        }
    }

    #[test]
    fn query_errors_restore_initialization_but_never_attempt_a_property_write() {
        let mut state = OfflineState::default();
        let mut unit = Unit::new(true, true);
        unit.fail_query = true;
        assert!(state.set(&mut unit, true).is_err());
        assert_eq!(
            &*unit.calls.borrow(),
            &["uninitialize", "query", "configure/initialize"]
        );
        assert!(unit.prepared);
        assert!(!state.requested());
    }

    #[test]
    fn failed_rollback_blocks_further_processing_and_keeps_the_last_committed_context() {
        let mut state = OfflineState::default();
        let mut unit = Unit::new(true, true);
        unit.fail_restart = 2;
        assert!(
            state
                .set(&mut unit, true)
                .unwrap_err()
                .contains("restoring")
        );
        assert!(!state.requested());
        assert!(unit.invalid);
        assert!(!unit.prepared);
        let calls = unit.calls.borrow().len();
        let error = state.check().unwrap_err();
        assert!(error.contains("restoring"));
        for mode in [false, true, false] {
            assert_eq!(state.set(&mut unit, mode).unwrap_err(), error);
        }
        assert_eq!(unit.calls.borrow().len(), calls);
    }

    #[test]
    fn a_previously_supported_property_cannot_silently_disappear_on_restore() {
        let mut state = OfflineState::default();
        let mut unit = Unit::new(true, true);
        state.set(&mut unit, true).unwrap();
        unit.supported = false;
        assert!(
            state
                .set(&mut unit, false)
                .unwrap_err()
                .contains("support changed")
        );
        assert!(state.requested());
        assert!(unit.mode);
        assert!(unit.prepared);
    }

    #[test]
    fn support_policy_separates_optional_absence_from_abi_and_runtime_errors() {
        assert_eq!(optional_property(-10879, 0, false).unwrap(), false);
        assert_eq!(optional_property(0, 4, false).unwrap(), false);
        assert_eq!(optional_property(0, 4, true).unwrap(), true);
        assert!(optional_property(-10867, 4, true).is_err());
        assert!(optional_property(-50, 4, true).is_err());
        assert!(optional_property(0, 8, true).is_err());
    }
}
