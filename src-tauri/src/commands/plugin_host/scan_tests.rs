use super::*;

#[cfg(feature = "native-plugins")]
#[test]
fn refresh_removes_uninstalled_packages_and_finds_new_plugins_without_reprobing_unchanged_cache() {
    let root = tempfile::tempdir().unwrap();
    let plugins = root.path().join("plugins");
    fs::create_dir(&plugins).unwrap();
    let removed = plugins.join("removed.vst3");
    let retained = plugins.join(if cfg!(windows) {
        "retained.dll"
    } else if cfg!(target_os = "macos") {
        "retained.vst"
    } else {
        "retained.so"
    });
    for path in [&removed, &retained] {
        fs::write(path, b"plugin").unwrap();
    }
    let (_, failures) = scan_paths(vec![plugins.to_string_lossy().into_owned()]).unwrap();
    assert!(failures.is_empty());
    scan_bundles(
        root.path(),
        vec![removed.clone(), retained.clone()],
        vec![],
        false,
        |path| {
            Ok(vec![
                json!({"id":path.file_stem().unwrap().to_string_lossy()}),
            ])
        },
    )
    .unwrap();
    fs::remove_file(&removed).unwrap();
    let added = plugins.join("added.vst3");
    fs::write(&added, b"new plugin").unwrap();
    let (paths, failures) = scan_paths(vec![plugins.to_string_lossy().into_owned()]).unwrap();
    let report = scan_bundles(
        root.path(),
        selected_scan_targets(paths),
        failures,
        false,
        |path| {
            assert_eq!(path.file_name().unwrap(), "added.vst3");
            Ok(vec![json!({"id":"added"})])
        },
    )
    .unwrap();
    assert_eq!(report.cached, 1);
    assert_eq!(report.scanned, 1);
    assert_eq!(report.plugins.len(), 2);
    assert!(report
        .plugins
        .iter()
        .any(|plugin| plugin["id"] == "retained"));
    assert!(report.plugins.iter().any(|plugin| plugin["id"] == "added"));
    assert!(report.failures.is_empty());
    let cache: Value =
        serde_json::from_slice(&fs::read(root.path().join("plugin-cache.json")).unwrap()).unwrap();
    assert_eq!(cache["entries"].as_array().unwrap().len(), 2);
    assert!(cache["entries"]
        .as_array()
        .unwrap()
        .iter()
        .all(|entry| entry["plugins"][0]["id"] != "removed"));
}

#[test]
fn cancelled_refresh_prunes_deleted_cache_entries_but_preserves_unvisited_existing_plugins() {
    let root = tempfile::tempdir().unwrap();
    let removed = root.path().join("removed.vst3");
    let retained = root.path().join("retained.vst3");
    for path in [&removed, &retained] {
        fs::write(path, b"plugin").unwrap();
    }
    scan_bundles(
        root.path(),
        vec![removed.clone(), retained.clone()],
        vec![],
        false,
        |path| {
            Ok(vec![
                json!({"id":path.file_stem().unwrap().to_string_lossy()}),
            ])
        },
    )
    .unwrap();
    fs::remove_file(&removed).unwrap();
    let state = PluginHostState::default();
    let run = state
        .scan_control
        .start("cancelled-refresh".into())
        .unwrap();
    state.scan_control.cancel("cancelled-refresh");
    let report = scan_pipeline::scan(
        root.path(),
        vec![retained],
        vec![],
        false,
        scan_pipeline::Context {
            run: Some(&run),
            storage: &state.scan_storage,
        },
        |_, _| panic!("cancelled scanning must not probe a plugin"),
    )
    .unwrap();
    assert!(report.cancelled);
    let cache: Value =
        serde_json::from_slice(&fs::read(root.path().join("plugin-cache.json")).unwrap()).unwrap();
    assert_eq!(cache["entries"].as_array().unwrap().len(), 1);
    assert_eq!(cache["entries"][0]["plugins"][0]["id"], "retained");
}

#[test]
fn plugins_removed_during_probing_are_absent_from_the_report_and_persistent_cache() {
    let root = tempfile::tempdir().unwrap();
    let removed = root.path().join("removed.vst3");
    let retained = root.path().join("retained.vst3");
    for path in [&removed, &retained] {
        fs::write(path, b"plugin").unwrap();
    }
    let report = scan_bundles(
        root.path(),
        vec![removed, retained],
        vec![],
        false,
        |path| {
            if path.file_name().unwrap() == "removed.vst3" {
                fs::remove_file(path).unwrap();
                Ok(vec![
                    json!({"id":"removed-one"}),
                    json!({"id":"removed-two"}),
                ])
            } else {
                Ok(vec![json!({"id":"retained"})])
            }
        },
    )
    .unwrap();
    assert_eq!(report.plugins.len(), 1);
    assert_eq!(report.plugins[0]["id"], "retained");
    assert!(report.failures.is_empty());
    let cache: Value =
        serde_json::from_slice(&fs::read(root.path().join("plugin-cache.json")).unwrap()).unwrap();
    assert_eq!(cache["entries"].as_array().unwrap().len(), 1);
    assert_eq!(cache["entries"][0]["plugins"][0]["id"], "retained");
}

#[test]
fn timeout_blacklists_only_its_package_continues_scanning_and_force_cannot_bypass_it() {
    let root = tempfile::tempdir().unwrap();
    let bad = root.path().join("bad.vst3");
    let good = root.path().join("good.vst3");
    fs::write(&bad, "bad").unwrap();
    fs::write(&good, "good").unwrap();
    let report = scan_bundles(
        root.path(),
        vec![bad.clone(), good.clone()],
        vec![],
        true,
        |path| {
            if path.file_name().unwrap() == "bad.vst3" {
                Err(ScanProbeError::Skipped(BlacklistReason::Timeout))
            } else {
                Ok(vec![json!({"id":"gain"})])
            }
        },
    )
    .unwrap();
    assert_eq!(report.plugins.len(), 1);
    assert_eq!(report.blacklisted.len(), 1);
    assert_eq!(report.blacklisted[0].reason, BlacklistReason::Timeout);
    // The UI returns the reported package identity, which can differ from the
    // temporary directory spelling (for example /var versus /private/var on macOS).
    let bad = PathBuf::from(&report.blacklisted[0].path);
    // A blocked path stays excluded even when missing: no fingerprint or native probe occurs.
    fs::remove_file(&bad).unwrap();
    let report = scan_bundles(root.path(), vec![bad.clone(), good], vec![], true, |path| {
        assert_eq!(path.file_name().unwrap(), "good.vst3");
        Ok(vec![json!({"id":"gain"})])
    })
    .unwrap();
    assert_eq!(report.scanned, 1);
    assert_eq!(report.blacklisted.len(), 1);
    assert!(report.failures.is_empty());
    let state = PluginHostState::default();
    restore_scan_entry(root.path(), &state, &bad.to_string_lossy()).unwrap();
    fs::write(&bad, "recovered").unwrap();
    let report = scan_bundles(root.path(), vec![bad], vec![], false, |_| {
        Ok(vec![json!({"id":"recovered"})])
    })
    .unwrap();
    assert_eq!(report.scanned, 1);
    assert_eq!(report.plugins[0]["id"], "recovered");
}

#[test]
fn manual_skip_overrides_a_successful_reply_and_persists_before_scan_cancellation() {
    let root = tempfile::tempdir().unwrap();
    let state = PluginHostState::default();
    let bad = root.path().join("bad.vst3");
    let next = root.path().join("next.vst3");
    fs::write(&bad, "bad").unwrap();
    fs::write(&next, "next").unwrap();
    let run = state.scan_control.start("scan-one".into()).unwrap();
    let report = scan_pipeline::scan(
        root.path(),
        vec![bad.clone(), next],
        vec![],
        true,
        scan_pipeline::Context {
            run: Some(&run),
            storage: &state.scan_storage,
        },
        |path, _| {
            assert_eq!(path.file_name().unwrap(), "bad.vst3");
            assert_eq!(
                skip_scan(root.path(), &state, "scan-one", &path.to_string_lossy()).unwrap()
                    ["skipped"],
                true
            );
            assert_eq!(blacklist::load(root.path()).unwrap().len(), 1);
            state.scan_control.cancel("scan-one");
            Ok(vec![json!({"id":"must-not-be-added"})])
        },
    )
    .unwrap();
    assert!(report.cancelled);
    assert!(report.plugins.is_empty());
    assert_eq!(
        blacklist::load(root.path()).unwrap()[0].reason,
        BlacklistReason::Manual
    );
    drop(run);
    assert!(state.scan_control.start("scan-two".into()).is_ok());
    assert_ne!(state.scan_process.id(), u32::MAX);
}

#[test]
fn blacklist_beats_a_success_cache_and_stale_skip_does_not_persist_an_entry() {
    let root = tempfile::tempdir().unwrap();
    let state = PluginHostState::default();
    let bad = root.path().join("cached.vst3");
    fs::write(&bad, "binary").unwrap();
    scan_bundles(root.path(), vec![bad.clone()], vec![], false, |_| {
        Ok(vec![json!({"id":"gain"})])
    })
    .unwrap();
    blacklist::add(root.path(), &bad.to_string_lossy(), BlacklistReason::Manual).unwrap();
    let report = scan_bundles(root.path(), vec![bad.clone()], vec![], false, |_| {
        panic!("an excluded cached package must not load")
    })
    .unwrap();
    assert!(report.plugins.is_empty());
    assert_eq!(report.cached, 0);
    assert_eq!(report.blacklisted.len(), 1);
    assert_eq!(
        skip_scan(root.path(), &state, "stale", "another.vst3").unwrap()["skipped"],
        false
    );
    assert_eq!(blacklist::load(root.path()).unwrap().len(), 1);
}

#[test]
fn restoring_a_manual_skip_before_the_worker_finishes_cannot_recreate_the_exclusion() {
    let root = tempfile::tempdir().unwrap();
    let state = PluginHostState::default();
    let bad = root.path().join("bad.vst3");
    fs::write(&bad, "bad").unwrap();
    let run = state.scan_control.start("scan-one".into()).unwrap();
    let report = scan_pipeline::scan(
        root.path(),
        vec![bad.clone()],
        vec![],
        true,
        scan_pipeline::Context {
            run: Some(&run),
            storage: &state.scan_storage,
        },
        |path, _| {
            assert_eq!(
                skip_scan(root.path(), &state, "scan-one", &path.to_string_lossy()).unwrap()
                    ["skipped"],
                true
            );
            restore_scan_entry(root.path(), &state, &path.to_string_lossy()).unwrap();
            assert!(blacklist::load(root.path()).unwrap().is_empty());
            Ok(vec![json!({"id":"discarded-success"})])
        },
    )
    .unwrap();
    assert!(report.plugins.is_empty());
    assert!(report.blacklisted.is_empty());
    drop(run);
    assert!(blacklist::load(root.path()).unwrap().is_empty());
    assert_eq!(
        skip_scan(root.path(), &state, "scan-one", &bad.to_string_lossy()).unwrap()["skipped"],
        false
    );
    let report = scan_bundles(root.path(), vec![bad], vec![], false, |_| {
        Ok(vec![json!({"id":"recovered"})])
    })
    .unwrap();
    assert_eq!(report.scanned, 1);
    assert_eq!(report.plugins[0]["id"], "recovered");
}

#[test]
fn restoring_during_a_scan_does_not_resurrect_its_previously_read_failure_cache() {
    let root = tempfile::tempdir().unwrap();
    let state = PluginHostState::default();
    let bad = root.path().join("bad.vst3");
    let trigger = root.path().join("trigger.vst3");
    fs::write(&bad, "bad").unwrap();
    fs::write(&trigger, "trigger").unwrap();
    let failed = scan_bundles(root.path(), vec![bad.clone()], vec![], false, |_| {
        Err("temporary failure".into())
    })
    .unwrap();
    // Use the scan's canonical identity when adding/restoring an exclusion, as
    // the UI does with entry.path, so invalidation addresses the cached package.
    let bad = PathBuf::from(&failed.failures[0].path);
    let run = state.scan_control.start("scan-one".into()).unwrap();
    let report = scan_pipeline::scan(
        root.path(),
        vec![bad.clone(), trigger],
        vec![],
        false,
        scan_pipeline::Context {
            run: Some(&run),
            storage: &state.scan_storage,
        },
        |_, _| {
            blacklist::add(root.path(), &bad.to_string_lossy(), BlacklistReason::Manual).unwrap();
            restore_scan_entry(root.path(), &state, &bad.to_string_lossy()).unwrap();
            Ok(vec![json!({"id":"trigger"})])
        },
    )
    .unwrap();
    assert_eq!(report.cached, 1);
    drop(run);
    let report = scan_bundles(root.path(), vec![bad], vec![], false, |_| {
        Ok(vec![json!({"id":"recovered"})])
    })
    .unwrap();
    assert_eq!(report.scanned, 1);
    assert_eq!(report.plugins[0]["id"], "recovered");
}

#[cfg(any(windows, unix))]
fn hanging_process() -> HostProcess {
    #[cfg(windows)]
    let mut command = {
        let mut command = Command::new("cmd.exe");
        command.args(["/D", "/C", "ping -n 30 127.0.0.1 >nul"]);
        command
    };
    #[cfg(unix)]
    let mut command = {
        let mut command = Command::new("sh");
        command.args(["-c", "exec sleep 30"]);
        command
    };
    command.env("PYMSS_STUDIO_DATA_ROOT", "unused-scan-fixture");
    HostProcess::spawn_command(command).unwrap()
}

#[cfg(any(windows, unix))]
#[test]
fn a_hung_scanner_has_a_bounded_deadline_and_is_reaped() {
    let control = Arc::new(ScanControl::default());
    let run = control.start("timeout".into()).unwrap();
    let ticket = run.begin_item("hung.vst3");
    let process = hanging_process();
    let child = process.control.clone();
    run.attach(ticket, child.clone());
    let result = receive_scan(&process, &run, ticket, Duration::from_millis(100));
    assert!(matches!(
        result,
        Err(ScanProbeError::Skipped(BlacklistReason::Timeout))
    ));
    drop(process);
    assert!(!*child.active.lock().unwrap());
    assert!(ticket.started.elapsed() < Duration::from_secs(5));
}

#[cfg(any(windows, unix))]
#[test]
fn manual_skip_preempts_a_queued_success_without_terminating_another_host() {
    let control = Arc::new(ScanControl::default());
    let run = control.start("manual".into()).unwrap();
    let ticket = run.begin_item("hung.vst3");
    let mut process = hanging_process();
    let mut other = hanging_process();
    let child = process.control.clone();
    run.attach(ticket, child.clone());
    let (reply, responses) = mpsc::sync_channel(1);
    reply.send(Ok(json!({"plugins":[{"id":"stale"}]}))).unwrap();
    process.responses = responses;
    assert!(control.skip("manual", "hung.vst3"));
    assert!(matches!(
        receive_scan(&process, &run, ticket, Duration::from_secs(10)),
        Err(ScanProbeError::Skipped(BlacklistReason::Manual))
    ));
    assert!(other.child.try_wait().unwrap().is_none());
    drop(process);
    assert!(!*child.active.lock().unwrap());
}

#[cfg(unix)]
#[test]
fn exited_leader_with_a_descendant_holding_stdout_is_killed_before_reaping() {
    let control = Arc::new(ScanControl::default());
    let run = control.start("descendant".into()).unwrap();
    let ticket = run.begin_item("descendant.vst3");
    let mut command = Command::new("sh");
    command.args(["-c", "sleep 30 & exit 0"]);
    let process = HostProcess::spawn_command(command).unwrap();
    let child = process.control.clone();
    run.attach(ticket, child.clone());
    assert!(matches!(
        receive_scan(&process, &run, ticket, Duration::from_millis(100)),
        Err(ScanProbeError::Skipped(BlacklistReason::Timeout))
    ));
    drop(process);
    assert!(!*child.active.lock().unwrap());
    assert!(ticket.started.elapsed() < Duration::from_secs(3));
}
