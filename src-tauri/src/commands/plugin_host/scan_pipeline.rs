//! Bounded package scans, persistent exclusions and cache updates on the blocking worker.
use super::*;
use std::collections::HashSet;

pub(super) struct Context<'a> {
    pub run: Option<&'a ScanRun>,
    pub storage: &'a Mutex<()>,
}

/// Restore descriptor metadata only. Never discover directories or load plugin binaries here.
pub(super) fn cached_report(root: &Path, paths: &[String]) -> Result<Value, String> {
    if paths.len() > 4096 {
        return Err("The plugin library exceeds the source limit".into());
    }
    let mut selected = Vec::new();
    for path in paths {
        let source = Path::new(path);
        if !is_virtual_scan_target(source) && !source.is_absolute() {
            return Err("Plugin library directories must be absolute".into());
        }
        // Cached aliases retain the selected spelling. Keep it alongside the
        // resolved root so a symlinked parent does not hide those entries.
        selected.push(blacklist::path_key(path));
        let canonical = if is_virtual_scan_target(source) {
            source.to_path_buf()
        } else {
            source
                .canonicalize()
                .unwrap_or_else(|_| source.to_path_buf())
        };
        selected.push(blacklist::path_key(&canonical.to_string_lossy()));
    }
    selected.sort_unstable();
    selected.dedup();
    let contains = |path: &str| {
        let key = blacklist::path_key(path);
        selected.iter().any(|source| {
            key == *source || key.starts_with(&format!("{}/", source.trim_end_matches('/')))
        })
    };
    let blocked = blacklist::load(root)?;
    let mut report = ScanReport {
        blacklisted: blocked
            .iter()
            .filter(|entry| contains(&entry.path))
            .cloned()
            .collect(),
        ..ScanReport::default()
    };
    let blocked: HashSet<_> = blocked
        .iter()
        .map(|entry| blacklist::path_key(&entry.path))
        .collect();
    let cache = match fs::File::open(root.join("plugin-cache.json")) {
        Ok(file) => {
            let mut bytes = Vec::new();
            file.take(MAX_CACHE_BYTES + 1)
                .read_to_end(&mut bytes)
                .map_err(|error| error.to_string())?;
            if bytes.len() as u64 > MAX_CACHE_BYTES {
                return Err("The plugin scan cache exceeded the size limit".into());
            }
            serde_json::from_slice::<Value>(&bytes).map_err(|error| error.to_string())?
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Value::Null,
        Err(error) => return Err(error.to_string()),
    };
    let entries = (cache["version"].as_u64() == Some(u64::from(SCAN_CACHE_VERSION)))
        .then(|| cache["entries"].as_array())
        .flatten();
    let mut has_cache = false;
    let mut seen = HashSet::new();
    for entry in entries.into_iter().flatten() {
        let Some(path) = entry["path"].as_str().filter(|path| {
            contains(path)
                || entry["sourcePath"].as_str().is_some_and(&contains)
                || entry["sourcePaths"]
                    .as_array()
                    .is_some_and(|paths| paths.iter().filter_map(Value::as_str).any(&contains))
        }) else {
            continue;
        };
        let key = blacklist::path_key(path);
        if blocked.contains(&key) || !seen.insert(key) {
            continue;
        }
        has_cache = true;
        if let Some(message) = entry["error"].as_str() {
            report.failures.push(ScanFailure {
                path: path.into(),
                message: message.into(),
            });
            report.cached += 1;
        } else if let Some(plugins) = entry["plugins"].as_array() {
            let mut plugins = plugins.clone();
            if plugins
                .iter_mut()
                .all(|plugin| normalize_descriptor(plugin, path, path == "au:system").is_ok())
            {
                report.plugins.extend(plugins);
                report.cached += 1;
            }
        }
    }
    let mut value = serde_json::to_value(report).map_err(|error| error.to_string())?;
    value["hasCache"] = json!(has_cache);
    Ok(value)
}

#[cfg(test)]
pub(super) fn scan_bundles(
    root: &Path,
    bundles: Vec<PathBuf>,
    failures: Vec<ScanFailure>,
    force: bool,
    mut probe: impl FnMut(&Path) -> Result<Vec<Value>, ScanProbeError>,
) -> Result<ScanReport, String> {
    scan(
        root,
        bundles,
        failures,
        force,
        Context {
            run: None,
            storage: &Mutex::new(()),
        },
        |path, _| probe(path),
    )
}

pub(super) fn scan(
    root: &Path,
    bundles: Vec<PathBuf>,
    failures: Vec<ScanFailure>,
    force: bool,
    context: Context<'_>,
    mut probe: impl FnMut(&Path, Option<ScanTicket>) -> Result<Vec<Value>, ScanProbeError>,
) -> Result<ScanReport, String> {
    if bundles.len() > 4096 {
        return Err("The plugin scan exceeded the bundle limit".into());
    }
    let run = context.run;
    let cache_path = root.join("plugin-cache.json");
    let guard = context
        .storage
        .lock()
        .map_err(|_| "Plugin scan storage lock failed")?;
    let mut blocked: HashMap<_, _> = blacklist::load(root)?
        .into_iter()
        .map(|entry| (blacklist::path_key(&entry.path), entry))
        .collect();
    let old_cache: Value = fs::File::open(&cache_path)
        .ok()
        .and_then(|file| serde_json::from_reader(file.take(MAX_CACHE_BYTES)).ok())
        .unwrap_or(Value::Null);
    drop(guard);
    let old_entries = (old_cache.get("version").and_then(Value::as_u64)
        == Some(u64::from(SCAN_CACHE_VERSION)))
    .then(|| old_cache.get("entries").and_then(Value::as_array))
    .flatten();
    let old_by_path: HashMap<_, _> = old_entries
        .into_iter()
        .flatten()
        .filter_map(|entry| {
            entry["path"]
                .as_str()
                .map(|path| (blacklist::path_key(path), entry))
        })
        .collect();
    let mut entries: Vec<Value> = Vec::new();
    let mut visited = HashSet::new();
    let mut report = ScanReport {
        failures,
        ..ScanReport::default()
    };
    if let Some(run) = run {
        run.set_total(bundles.len());
    }
    for bundle in bundles {
        if run.is_some_and(|run| run.decision(None) == ScanDecision::Cancel) {
            report.cancelled = true;
            break;
        }
        let source_path = bundle.to_string_lossy().to_string();
        let virtual_target = is_virtual_scan_target(&bundle);
        // The original spelling can match an exclusion without touching the package filesystem.
        let bundle = if virtual_target
            || blocked.contains_key(&blacklist::path_key(&bundle.to_string_lossy()))
        {
            bundle
        } else {
            bundle.canonicalize().unwrap_or(bundle)
        };
        let path = bundle.to_string_lossy().to_string();
        let key = blacklist::path_key(&path);
        if !visited.insert(key.clone()) {
            if let Some(entry) = entries.iter_mut().find(|entry| {
                entry["path"]
                    .as_str()
                    .is_some_and(|path| blacklist::path_key(path) == key)
            }) {
                let aliases = entry
                    .as_object_mut()
                    .expect("cache entry")
                    .entry("sourcePaths")
                    .or_insert_with(|| json!([]));
                if let Some(aliases) = aliases.as_array_mut() {
                    aliases.push(json!(source_path));
                }
            }
            continue;
        }
        let ticket = run.map(|run| run.begin_item(&path));
        if let Some(entry) = blocked.get(&key) {
            if run
                .zip(ticket)
                .is_some_and(|(run, ticket)| run.complete_item(ticket) == ScanDecision::Cancel)
            {
                report.cancelled = true;
                break;
            }
            report.blacklisted.push(entry.clone());
            continue;
        }
        if let Some(reason) = unsupported_scan_reason(&bundle) {
            report.failures.push(ScanFailure {
                path,
                message: reason.into(),
            });
            if let Some((run, ticket)) = run.zip(ticket) {
                run.complete_item(ticket);
            }
            continue;
        }
        let mut fingerprint = None;
        let mut used_cache = false;
        let result = (|| -> Result<Vec<Value>, ScanProbeError> {
            fingerprint = Some(if virtual_target {
                String::new()
            } else {
                bundle_fingerprint(&bundle)?
            });
            if !force && !virtual_target && !run.is_some_and(|run| run.is_invalidated(&path)) {
                let previous = old_by_path.get(&key).copied();
                let matches = previous.is_some_and(|entry| {
                    entry["fingerprint"].as_str() == fingerprint.as_deref()
                        || entry["fingerprint"].as_str().is_some_and(|old| {
                            !old.starts_with("metadata-v1:")
                                && legacy_bundle_fingerprint(&bundle)
                                    .is_ok_and(|value| value == old)
                        })
                });
                if let Some(entry) = previous.filter(|_| matches) {
                    if let Some(error) = entry["error"].as_str() {
                        used_cache = true;
                        return Err(ScanProbeError::Failure(error.to_owned()));
                    }
                    if let Some(found) = entry["plugins"]
                        .as_array()
                        .filter(|found| !found.is_empty() && found.iter().all(Value::is_object))
                    {
                        let mut found = found.clone();
                        for descriptor in &mut found {
                            normalize_descriptor(descriptor, &path, false)?;
                        }
                        used_cache = true;
                        return Ok(found);
                    }
                }
            }
            if let Some((run, ticket)) = run.zip(ticket) {
                match run.decision(Some(ticket)) {
                    ScanDecision::Skip => {
                        return Err(ScanProbeError::Skipped(BlacklistReason::Manual))
                    }
                    ScanDecision::Cancel => return Err(ScanProbeError::Cancelled),
                    ScanDecision::Continue => {}
                }
                if ticket.started.elapsed() >= SCAN_TIMEOUT {
                    return Err(ScanProbeError::Skipped(BlacklistReason::Timeout));
                }
            }
            report.scanned += 1;
            let mut found = probe(&bundle, ticket)?;
            if found.is_empty() && !virtual_target {
                return Err(ScanProbeError::Failure(
                    "No compatible effects were found in this plugin".into(),
                ));
            }
            if found.iter().any(|descriptor| !descriptor.is_object()) {
                return Err(ScanProbeError::Failure(
                    "The plugin scanner returned an invalid result".into(),
                ));
            }
            for descriptor in &mut found {
                normalize_descriptor(descriptor, &path, virtual_target)?;
            }
            Ok(found)
        })();
        // Commit under the control lock: late skip cannot remove a later package or accept stale success.
        let result = match run
            .zip(ticket)
            .map(|(run, ticket)| run.complete_item(ticket))
        {
            Some(ScanDecision::Skip) => Err(ScanProbeError::Skipped(BlacklistReason::Manual)),
            Some(ScanDecision::Cancel) => Err(ScanProbeError::Cancelled),
            _ => result,
        };
        match result {
            Ok(found) => {
                report.cached += usize::from(used_cache);
                entries.push(
                    json!({"path":path,"sourcePath":source_path,"fingerprint":fingerprint,"plugins":found,"error":null}),
                );
                report.plugins.extend(found);
            }
            Err(ScanProbeError::Skipped(reason)) => {
                let _guard = context
                    .storage
                    .lock()
                    .map_err(|_| "Plugin scan storage lock failed")?;
                // Manual skip has one persistence owner (the control command). It may already
                // have been restored while the worker was stopping; never re-add it here.
                let entry = match reason {
                    BlacklistReason::Manual => blacklist::load(root)?
                        .into_iter()
                        .find(|entry| blacklist::path_matches(&entry.path, &path)),
                    BlacklistReason::Timeout => {
                        Some(blacklist::add(root, &path, BlacklistReason::Timeout)?)
                    }
                };
                if let Some(entry) = entry {
                    blocked.insert(key, entry.clone());
                    report.blacklisted.push(entry);
                }
            }
            Err(ScanProbeError::Cancelled) => {
                // Legacy callers retain the previous cache on cancellation.
                if run.is_none() {
                    return Err("Plugin scanning was cancelled".into());
                }
                report.cancelled = true;
                break;
            }
            Err(ScanProbeError::Failure(message)) => {
                report.cached += usize::from(used_cache);
                crate::session_log::early(
                    "WARN",
                    "audio.plugin_scan",
                    &format!("Plugin scan failed for {}: {message}", bundle.display()),
                );
                entries.push(
                    json!({"path":path,"sourcePath":source_path,"fingerprint":fingerprint,"plugins":[],"error":message}),
                );
                report.failures.push(ScanFailure { path, message });
            }
        }
    }
    // The library is global: scanning one source must retain the cache of other sources.
    if let Some(previous) = old_entries {
        entries.extend(
            previous
                .iter()
                .filter(|entry| {
                    entry["path"]
                        .as_str()
                        .is_some_and(|path| !visited.contains(&blacklist::path_key(path)))
                })
                .cloned(),
        );
    }
    // Keep valid directory aliases across partial refreshes, and drop deleted or retargeted
    // aliases here so restoring the catalog never has to touch plugin files.
    for entry in &mut entries {
        let Some(path) = entry["path"].as_str() else {
            continue;
        };
        if is_virtual_scan_target(Path::new(path)) {
            continue;
        }
        let key = blacklist::path_key(path);
        let previous = old_by_path.get(&key).copied();
        let mut candidates = Vec::new();
        for value in [Some(&*entry), previous].into_iter().flatten() {
            if let Some(source) = value["sourcePath"].as_str() {
                candidates.push(source.to_owned());
            }
            candidates.extend(
                value["sourcePaths"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .take(4096)
                    .filter_map(Value::as_str)
                    .map(str::to_owned),
            );
        }
        let mut seen = HashSet::new();
        let aliases: Vec<_> = candidates
            .into_iter()
            .filter(|alias| {
                let alias_key = blacklist::path_key(alias);
                if alias_key == key || !Path::new(alias).is_absolute() || !seen.insert(alias_key) {
                    return false;
                }
                match Path::new(alias).canonicalize() {
                    Ok(target) => blacklist::path_key(&target.to_string_lossy()) == key,
                    Err(error) => error.kind() != std::io::ErrorKind::NotFound,
                }
            })
            .collect();
        if let Some(object) = entry.as_object_mut() {
            object.remove("sourcePath");
            object.remove("sourcePaths");
            if !aliases.is_empty() {
                object.insert("sourcePaths".into(), json!(aliases));
            }
        }
    }
    // Reconcile after probing and after restoring unvisited cache entries on cancellation.
    // Only a confirmed missing package is removed; access or device errors are not uninstalls.
    let removed_paths: HashSet<_> = entries
        .iter()
        .filter_map(|entry| entry["path"].as_str())
        .filter(|path| Path::new(path).is_absolute())
        .collect::<HashSet<_>>()
        .into_iter()
        .filter(|path| matches!(fs::metadata(path), Err(error) if error.kind() == std::io::ErrorKind::NotFound))
        .map(blacklist::path_key)
        .collect();
    report.plugins.retain(|plugin| {
        plugin["path"]
            .as_str()
            .is_none_or(|path| !removed_paths.contains(&blacklist::path_key(path)))
    });
    let _guard = context
        .storage
        .lock()
        .map_err(|_| "Plugin scan storage lock failed")?;
    let invalidations = run.map(ScanRun::invalidations).unwrap_or_default();
    let latest_blocked: HashSet<_> = blacklist::load(root)?
        .into_iter()
        .map(|entry| blacklist::path_key(&entry.path))
        .collect();
    report.blacklisted.retain(|entry| {
        let key = blacklist::path_key(&entry.path);
        latest_blocked.contains(&key) && !invalidations.contains(&key)
    });
    // Removing an exclusion while a scan runs must not resurrect its old cached failure at commit.
    entries.retain(|entry| {
        entry["path"].as_str().is_some_and(|path| {
            let key = blacklist::path_key(path);
            !latest_blocked.contains(&key)
                && !invalidations.contains(&key)
                && !removed_paths.contains(&key)
        })
    });
    let cache = json!({"version":SCAN_CACHE_VERSION,"entries":entries});
    if serde_json::to_vec(&cache)
        .map_err(|error| error.to_string())?
        .len() as u64
        > MAX_CACHE_BYTES
    {
        return Err("The plugin scan cache exceeded the size limit".into());
    }
    crate::storage::write_json_file(&cache_path, &cache).map_err(|error| error.to_string())?;
    Ok(report)
}
