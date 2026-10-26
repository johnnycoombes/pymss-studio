//! Headless processing-mode contracts, each in an isolated plugin process.
use soundcraft_dsp::Plugin;

mod common;

fn main() {
    if std::env::args().nth(1).as_deref() == Some("--child") {
        let mode = std::env::args().nth(2).expect("child processing mode");
        check(&mode);
        soundcraft_vst3_host::shutdown();
        return;
    }
    for mode in ["", "mode-quality", "reject-offline", "reject-rollback", "only-ara", "only-offline", "only-rt", "only-rt,reject-reactivate", "no-offline"] {
        let result = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("--child")
            .arg(mode)
            .env("PYMSS_VST_FIXTURE_BEHAVIOR", mode)
            .status()
            .unwrap();
        assert!(result.success(), "processing mode scenario failed: {mode}");
    }
}

fn check(mode: &str) {
    let descriptor = soundcraft_vst3_host::scan_paths(&[common::fixture_dir().to_path_buf()])
        .into_iter()
        .find(|plugin| plugin.name == "Fixture Gain")
        .unwrap();
    let id = descriptor.id.clone();
    if mode == "only-ara" || mode == "only-offline" {
        let token = if mode == "only-ara" { "OnlyARA" } else { "OnlyOfflineProcess" };
        assert!(descriptor.sub_categories.iter().any(|category| category == token));
        assert!(soundcraft_vst3_host::instantiate_plugin(&id).err().unwrap().to_string().contains(token));
        assert!(soundcraft_vst3_host::instantiate_descriptor(&descriptor).err().unwrap().to_string().contains(token));
        // A stale or altered caller descriptor must not bypass the live factory's restriction.
        let mut stale = descriptor;
        stale.sub_categories.clear();
        assert!(soundcraft_vst3_host::instantiate_descriptor(&stale).err().unwrap().to_string().contains(token));
        assert!(soundcraft_vst3_host::create(&id).is_none());
        assert!(soundcraft_vst3_host::plugin_info(&id).is_none());
        return;
    }
    if mode.split(',').any(|token| token == "only-rt") {
        check_realtime_only(&descriptor);
        return;
    }
    if mode == "no-offline" {
        assert!(descriptor.sub_categories.iter().any(|category| category == "NoOfflineProcess"));
    }
    let mut plugin = soundcraft_vst3_host::instantiate_plugin(&id).unwrap();
    assert!(!plugin.is_offline());
    if mode.starts_with("reject-") {
        plugin.try_prepare(48_000.0, 64, 2).unwrap();
        assert!(plugin.set_param("7", 1.5));
        let error = plugin.set_offline(true).unwrap_err();
        assert!(!plugin.is_offline());
        let mut io = vec![vec![0.25; 64]; 2];
        if mode == "reject-rollback" {
            assert!(error.contains("unable to restore processing mode"));
            assert!(plugin.try_process(&mut io, 64).is_err());
            assert!(plugin.try_process(&mut io, 64).is_err());
        } else {
            plugin.try_process(&mut io, 64).unwrap();
            assert!(
                io.iter()
                    .flatten()
                    .all(|value| (*value - 0.375).abs() < 1e-6)
            );
        }
        return;
    }

    // Staging the initial mode must not prepare/process on a different thread.
    plugin.set_offline(true).unwrap();
    plugin.try_prepare(48_000.0, 64, 2).unwrap();
    assert!(plugin.is_offline());
    if mode == "mode-quality" {
        assert_eq!(plugin.latency(), 11);
        assert_eq!(plugin.tail_samples(), 64);
    }
    assert!(plugin.set_param("7", 1.5));
    plugin.flush_pending().unwrap();
    let state = plugin.save_state().unwrap();
    assert!(plugin.set_param("7", 1.25));
    for offline in [false, true, false] {
        plugin.set_offline(offline).unwrap();
        assert_eq!(plugin.is_offline(), offline);
        assert_eq!(plugin.param("7"), Some(1.25));
        // Mode setup must retain the queued processor value and controller state.
        plugin.flush_pending().unwrap();
        let mut io = vec![vec![0.25; 64]; 2];
        plugin.try_process(&mut io, 64).unwrap();
        let expected = if offline && mode == "mode-quality" {
            0.625
        } else {
            0.3125
        };
        assert!(
            io.iter()
                .flatten()
                .all(|value| (*value - expected).abs() < 1e-6)
        );
        if mode == "mode-quality" {
            assert_eq!(plugin.latency(), if offline { 11 } else { 3 });
            assert_eq!(plugin.tail_samples(), if offline { 64 } else { 0 });
        }
    }
    assert!(plugin.load_state(&state));
    assert_eq!(plugin.param("7"), Some(1.5));
}


fn check_realtime_only(descriptor: &soundcraft_vst3_host::Vst3Descriptor) {
    assert!(descriptor.sub_categories.iter().any(|category| category == "OnlyRT"));
    let mut unprepared = soundcraft_vst3_host::instantiate_descriptor(descriptor).unwrap();
    assert!(unprepared.set_offline(true).unwrap_err().contains("OnlyRT"));
    assert!(!unprepared.is_offline());
    unprepared.try_prepare(48_000.0, 64, 2).unwrap();
    let mut initial = vec![vec![0.25; 64]; 2];
    unprepared.try_process(&mut initial, 64).unwrap();
    assert!(initial.iter().flatten().all(|value| (*value - 0.125).abs() < 1e-6));
    drop(unprepared);

    let mut plugin = soundcraft_vst3_host::instantiate_plugin(&descriptor.id).unwrap();
    plugin.try_prepare(48_000.0, 64, 2).unwrap();
    assert!(plugin.set_param("7", 1.5));
    let mut first = vec![vec![0.25; 64]; 2];
    plugin.try_process(&mut first, 64).unwrap();
    assert!(first.iter().flatten().all(|value| (*value - 0.375).abs() < 1e-6));
    let timing = (plugin.latency(), plugin.tail_samples());
    assert!(plugin.set_param("7", 1.25));
    let state = plugin.save_state().unwrap();
    assert!(plugin.set_offline(true).unwrap_err().contains("OnlyRT"));
    assert!(!plugin.is_offline());
    assert_eq!(plugin.param("7"), Some(1.25));
    assert_eq!((plugin.latency(), plugin.tail_samples()), timing);
    assert_eq!(plugin.save_state().unwrap(), state);
    // reject-reactivate makes an accidental reconfiguration poison the instance after this point.
    let mut next = vec![vec![0.25; 64]; 2];
    plugin.try_process(&mut next, 64).unwrap();
    assert!(next.iter().flatten().all(|value| (*value - 0.3125).abs() < 1e-6));
    plugin.set_offline(false).unwrap();
}
