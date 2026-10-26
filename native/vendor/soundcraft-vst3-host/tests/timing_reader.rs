//! Isolated headless timing-reader contracts. No editor windows or audio device are used.
use soundcraft_dsp::Plugin;

mod common;

fn main() {
    if std::env::args().nth(1).as_deref() == Some("--child") {
        check(&std::env::args().nth(2).expect("timing scenario"));
        soundcraft_vst3_host::shutdown();
        return;
    }
    for mode in ["strict-timing", "strict-timing,no-controller", "strict-timing,getter-notification", "strict-timing,deferred-latency", "strict-timing,deferred-latency,reject-reactivate", "strict-timing,deferred-latency,reject-stop"] {
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--child", mode]).env("PYMSS_VST_FIXTURE_BEHAVIOR", mode).spawn().unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(60);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(status.success(), "timing scenario failed: {mode}");
                break;
            }
            if std::time::Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                panic!("timing scenario timed out: {mode}");
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    }
}

fn check(mode: &str) {
    let descriptor = soundcraft_vst3_host::scan_paths(&[common::fixture_dir().to_path_buf()])
        .into_iter().find(|plugin| plugin.name == "Fixture Gain").unwrap();
    let mut plugin = soundcraft_vst3_host::instantiate_descriptor(&descriptor).unwrap();
    let mut timing = plugin.timing_reader();
    assert!(timing.refresh().is_err(), "unprepared readers must not query SDK timing");
    plugin.try_prepare(48_000.0, 64, 2).unwrap();
    assert_eq!(plugin.latency(), 3);
    if mode.contains("deferred-latency") { assert!(plugin.set_param("7", 1.5)); }
    let wrong_thread = std::thread::spawn(move || {
        assert!(timing.refresh().unwrap_err().contains("creating control thread"));
        timing
    });
    let mut timing = wrong_thread.join().unwrap();
    // Process and parameter flushes on the worker must use only the cache, including dirty
    // timing notifications. The fixture aborts if either getter is called on this thread.
    let mut plugin = std::thread::spawn(move || {
        let mut io = vec![vec![0.25; 64]; 2];
        plugin.try_process(&mut io, 64).unwrap();
        plugin.flush_pending().unwrap();
        assert_eq!(plugin.refresh_latency_control(), 3);
        if mode_is_deferred() { assert_ne!(plugin.refresh_latency(), 3); }
        plugin
    }).join().unwrap();
    if mode.contains("reject-") {
        let error = timing.refresh().unwrap_err();
        assert!(error.contains(if mode.contains("reject-stop") { "stop plugin processing" } else { "reactivate" }));
        assert!(timing.refresh().unwrap_err().contains("reload"));
        let mut io = vec![vec![0.25; 64]; 2];
        assert!(plugin.try_process(&mut io, 64).is_err());
        assert!(plugin.flush_pending().is_err());
        assert!(plugin.set_offline(false).is_err());
        assert!(plugin.set_offline(true).is_err());
        assert!(plugin.try_prepare(48_000.0, 64, 2).is_err());
        drop(plugin);
        assert!(timing.refresh().is_err());
        return;
    }
    timing.refresh().unwrap();
    if mode.contains("getter-notification") {
        assert_ne!(plugin.refresh_latency(), 3, "a getter's notification must remain pending");
        timing.refresh().unwrap();
        assert_eq!(plugin.refresh_latency(), 3);
    }
    if mode.contains("deferred-latency") {
        assert_eq!(plugin.refresh_latency_control(), 11, "reactivation must publish deferred latency");
        assert_eq!(plugin.refresh_latency(), 11);
        plugin = std::thread::spawn(move || {
            let mut io = vec![vec![0.25; 64]; 2];
            plugin.try_process(&mut io, 64).unwrap();
            assert_eq!(plugin.refresh_latency(), 11);
            plugin
        }).join().unwrap();
    }
    drop(plugin);
    assert!(timing.refresh().unwrap_err().contains("released"));
}

fn mode_is_deferred() -> bool {
    std::env::var("PYMSS_VST_FIXTURE_BEHAVIOR").unwrap_or_default().contains("deferred-latency")
}
