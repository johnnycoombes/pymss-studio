use super::*;

fn sources(path: &Path) -> Vec<String> {
    vec![path.to_string_lossy().into_owned()]
}

#[test]
fn four_hundred_cached_packages_need_no_probes_and_only_changed_packages_are_identified() {
    let root = tempfile::tempdir().unwrap();
    let directory = root.path().join("plugins");
    fs::create_dir(&directory).unwrap();
    let mut packages = Vec::new();
    for index in 0..400 {
        let path = directory.join(format!("effect-{index}.vst3"));
        fs::write(&path, b"binary").unwrap();
        packages.push(path);
    }
    let first = scan_bundles(root.path(), packages.clone(), vec![], false, |path| {
        Ok(vec![
            json!({"id":path.file_stem().unwrap().to_string_lossy(),"name":"Effect"}),
        ])
    })
    .unwrap();
    assert_eq!(first.scanned, 400);
    let before = fs::read(root.path().join("plugin-cache.json")).unwrap();
    let cached = scan_pipeline::cached_report(root.path(), &sources(&directory)).unwrap();
    assert_eq!(cached["plugins"].as_array().unwrap().len(), 400);
    assert_eq!(cached["scanned"], 0);
    assert_eq!(
        before,
        fs::read(root.path().join("plugin-cache.json")).unwrap()
    );
    let started = std::time::Instant::now();
    let unchanged = scan_bundles(root.path(), packages.clone(), vec![], false, |_| {
        panic!("unchanged packages must not start native probes")
    })
    .unwrap();
    eprintln!("400-package metadata refresh: {:?}", started.elapsed());
    assert_eq!(unchanged.cached, 400);
    assert_eq!(unchanged.scanned, 0);
    fs::write(&packages[23], b"updated binary version").unwrap();
    fs::remove_file(packages.remove(100)).unwrap();
    let extra = directory.join("new.vst3");
    fs::write(&extra, b"new").unwrap();
    packages.push(extra);
    let mut probed = Vec::new();
    let changed = scan_bundles(root.path(), packages, vec![], false, |path| {
        probed.push(path.file_name().unwrap().to_string_lossy().into_owned());
        Ok(vec![
            json!({"id":path.file_stem().unwrap().to_string_lossy(),"name":"Updated"}),
        ])
    })
    .unwrap();
    probed.sort();
    assert_eq!(probed, ["effect-23.vst3", "new.vst3"]);
    assert_eq!(changed.scanned, 2);
    assert_eq!(changed.cached, 398);
    let cached = scan_pipeline::cached_report(root.path(), &sources(&directory)).unwrap();
    let plugins = cached["plugins"].as_array().unwrap();
    assert_eq!(plugins.len(), 400);
    assert!(!plugins.iter().any(|plugin| plugin["id"] == "effect-100"));
}

#[test]
fn partial_refresh_preserves_other_sources_and_cache_reads_respect_path_boundaries_and_blacklist() {
    let root = tempfile::tempdir().unwrap();
    let a = root.path().join("VST");
    let b = root.path().join("VST2");
    fs::create_dir(&a).unwrap();
    fs::create_dir(&b).unwrap();
    let one = a.join("one.vst3");
    let two = b.join("two.vst3");
    fs::write(&one, b"one").unwrap();
    fs::write(&two, b"two").unwrap();
    for path in [&one, &two] {
        scan_bundles(root.path(), vec![path.clone()], vec![], false, |_| {
            Ok(vec![
                json!({"id":"effect","vendorMetadata":{"retain":true}}),
            ])
        })
        .unwrap();
    }
    let cached = scan_pipeline::cached_report(root.path(), &sources(&a)).unwrap();
    assert_eq!(cached["plugins"].as_array().unwrap().len(), 1);
    assert_eq!(
        cached["plugins"][0]["path"],
        one.canonicalize().unwrap().to_string_lossy().as_ref()
    );
    assert_eq!(cached["plugins"][0]["vendorMetadata"]["retain"], true);
    let cached = scan_pipeline::cached_report(root.path(), &sources(&b)).unwrap();
    assert_eq!(cached["plugins"].as_array().unwrap().len(), 1);
    blacklist::add(
        root.path(),
        &two.canonicalize().unwrap().to_string_lossy(),
        BlacklistReason::Manual,
    )
    .unwrap();
    let cached = scan_pipeline::cached_report(root.path(), &sources(&b)).unwrap();
    assert!(cached["plugins"].as_array().unwrap().is_empty());
    assert_eq!(cached["blacklisted"].as_array().unwrap().len(), 1);
    assert!(scan_pipeline::cached_report(root.path(), &["relative".into()]).is_err());
}

#[test]
fn old_fingerprints_upgrade_without_native_probes_and_resource_edits_do_not_force_identification() {
    let root = tempfile::tempdir().unwrap();
    let bundle = root.path().join("effect.vst3");
    let executable = bundle.join("Contents/x86_64-win/Effect.vst3");
    let resources = bundle.join("Contents/Resources/Presets");
    fs::create_dir_all(executable.parent().unwrap()).unwrap();
    fs::create_dir_all(&resources).unwrap();
    fs::write(&executable, b"binary").unwrap();
    let preset = resources.join("preset.xml");
    fs::write(&preset, b"preset").unwrap();
    let bundle = bundle.canonicalize().unwrap();
    fs::write(root.path().join("plugin-cache.json"), serde_json::to_vec(&json!({
        "version":SCAN_CACHE_VERSION,"entries":[{"path":bundle,"fingerprint":legacy_bundle_fingerprint(&bundle).unwrap(),
            "plugins":[{"id":"retained","name":"Effect"}],"error":null}]
    })).unwrap()).unwrap();
    let migrated = scan_bundles(root.path(), vec![bundle.clone()], vec![], false, |_| {
        panic!("upgrading the metadata fingerprint must reuse the old descriptor")
    })
    .unwrap();
    assert_eq!(migrated.cached, 1);
    let before = bundle_fingerprint(&bundle).unwrap();
    fs::write(&preset, b"updated user preset data").unwrap();
    assert_eq!(bundle_fingerprint(&bundle).unwrap(), before);
    fs::write(&executable, b"updated executable").unwrap();
    assert_ne!(bundle_fingerprint(&bundle).unwrap(), before);
    let changed = scan_bundles(root.path(), vec![bundle], vec![], false, |_| {
        Ok(vec![json!({"id":"updated"})])
    })
    .unwrap();
    assert_eq!(changed.scanned, 1);
}

#[test]
fn duplicate_package_aliases_are_scanned_and_restored_once() {
    let root = tempfile::tempdir().unwrap();
    let package = root.path().join("effect.vst3");
    fs::write(&package, b"binary").unwrap();
    let alias = root.path().join(".").join("effect.vst3");
    let report = scan_bundles(root.path(), vec![package, alias], vec![], false, |_| {
        Ok(vec![json!({"id":"one"})])
    })
    .unwrap();
    assert_eq!(report.scanned, 1);
    assert_eq!(report.plugins.len(), 1);
    let cached = scan_pipeline::cached_report(root.path(), &sources(root.path())).unwrap();
    assert_eq!(cached["plugins"].as_array().unwrap().len(), 1);
}

#[test]
fn deleted_directory_aliases_stay_removed_after_partial_refresh_and_catalog_restore() {
    let root = tempfile::tempdir().unwrap();
    let package = root.path().join("installed.vst3");
    let a = root.path().join("A");
    let b = root.path().join("B");
    for directory in [&package, &a, &b] {
        fs::create_dir(directory).unwrap();
    }
    let alias_a = a.join("Effect.vst3");
    let alias_b = b.join("Effect.vst3");
    for alias in [&alias_a, &alias_b] {
        #[cfg(unix)]
        std::os::unix::fs::symlink(&package, alias).unwrap();
        #[cfg(windows)]
        {
            let output = Command::new("cmd")
                .args(["/C", "mklink", "/J"])
                .arg(alias)
                .arg(&package)
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "junction creation failed: {output:?}"
            );
        }
    }
    scan_bundles(
        root.path(),
        vec![alias_a.clone(), alias_b.clone()],
        vec![],
        false,
        |_| Ok(vec![json!({"id":"effect"})]),
    )
    .unwrap();
    scan_bundles(root.path(), vec![alias_a], vec![], false, |_| {
        panic!("a partial refresh must reuse the unchanged canonical package")
    })
    .unwrap();
    assert_eq!(
        scan_pipeline::cached_report(root.path(), &sources(&b)).unwrap()["plugins"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    #[cfg(windows)]
    fs::remove_dir(&alias_b).unwrap();
    #[cfg(unix)]
    fs::remove_file(&alias_b).unwrap();
    scan_bundles(root.path(), vec![], vec![], false, |_| unreachable!()).unwrap();
    assert!(
        scan_pipeline::cached_report(root.path(), &sources(&b)).unwrap()["plugins"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        scan_pipeline::cached_report(root.path(), &sources(&a)).unwrap()["plugins"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert!(package.is_dir());
}

#[test]
fn cached_catalog_matches_source_aliases_below_a_symlinked_parent_directory() {
    let root = tempfile::tempdir().unwrap();
    let physical = root.path().join("physical");
    let source = physical.join("plugins");
    let package = physical.join("installed.vst3");
    fs::create_dir_all(&source).unwrap();
    fs::create_dir(&package).unwrap();
    let alias_root = root.path().join("alias");
    let alias_package = source.join("Effect.vst3");
    for (target, alias) in [(&physical, &alias_root), (&package, &alias_package)] {
        #[cfg(unix)]
        std::os::unix::fs::symlink(target, alias).unwrap();
        #[cfg(windows)]
        {
            let output = Command::new("cmd")
                .args(["/C", "mklink", "/J"])
                .arg(alias)
                .arg(target)
                .output()
                .unwrap();
            assert!(output.status.success(), "junction creation failed: {output:?}");
        }
    }
    let selected = alias_root.join("plugins");
    scan_bundles(root.path(), vec![selected.join("Effect.vst3")], vec![], false, |_| {
        Ok(vec![json!({"id":"effect"})])
    }).unwrap();
    let before = fs::read(root.path().join("plugin-cache.json")).unwrap();
    // Match the saved source spelling even when its parent resolves elsewhere,
    // such as /var -> /private/var on macOS. Restoration must not rescan packages.
    let restored = scan_pipeline::cached_report(root.path(), &sources(&selected)).unwrap();
    assert_eq!(restored["plugins"].as_array().unwrap().len(), 1);
    assert_eq!(restored["scanned"], 0);
    assert_eq!(fs::read(root.path().join("plugin-cache.json")).unwrap(), before);
    assert!(scan_pipeline::cached_report(root.path(), &sources(&root.path().join("unrelated")))
        .unwrap()["plugins"].as_array().unwrap().is_empty());
}

#[test]
fn cached_descriptors_remain_available_during_a_blocked_probe_and_reading_does_not_touch_packages()
{
    let root = tempfile::tempdir().unwrap();
    let package = root.path().join("effect.vst3");
    fs::write(&package, b"binary").unwrap();
    scan_bundles(root.path(), vec![package.clone()], vec![], false, |_| {
        Ok(vec![json!({"id":"before"})])
    })
    .unwrap();
    let state = PluginHostState::default();
    let worker_state = state.clone();
    let worker_root = root.path().to_owned();
    let (started, waiting) = mpsc::channel();
    let (release, blocked) = mpsc::channel();
    let worker = std::thread::spawn(move || {
        let _scan = worker_state.scan_lock.lock().unwrap();
        scan_pipeline::scan(
            &worker_root,
            vec![package],
            vec![],
            true,
            scan_pipeline::Context {
                run: None,
                storage: &worker_state.scan_storage,
            },
            |_, _| {
                started.send(()).unwrap();
                blocked.recv_timeout(Duration::from_secs(10)).unwrap();
                Ok(vec![json!({"id":"after"})])
            },
        )
        .unwrap();
    });
    waiting.recv_timeout(Duration::from_secs(10)).unwrap();
    {
        let _snapshot = state
            .scan_storage
            .try_lock()
            .expect("probing must not lock catalog reads");
        let cached = scan_pipeline::cached_report(root.path(), &sources(root.path())).unwrap();
        assert_eq!(cached["plugins"][0]["id"], "before");
    }
    release.send(()).unwrap();
    worker.join().unwrap();
    fs::remove_file(root.path().join("effect.vst3")).unwrap();
    let cached = scan_pipeline::cached_report(root.path(), &sources(root.path())).unwrap();
    assert_eq!(
        cached["plugins"][0]["id"], "after",
        "cache restore must not enumerate or stat plugin files"
    );
    scan_bundles(root.path(), vec![], vec![], false, |_| unreachable!()).unwrap();
    assert!(
        scan_pipeline::cached_report(root.path(), &sources(root.path())).unwrap()["plugins"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}
