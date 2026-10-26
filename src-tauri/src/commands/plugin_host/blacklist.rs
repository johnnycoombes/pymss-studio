//! Persist scan exclusions without opening or resolving the excluded plugin paths.
//! Callers serialize load/mutate operations with the plugin-host state mutex.

use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::io::{ErrorKind, Read};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

const FILE_NAME: &str = "plugin-blacklist.json";
const VERSION: u32 = 1;
const MAX_BYTES: usize = 2 * 1024 * 1024;
const MAX_ENTRIES: usize = 4096;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub(super) struct BlacklistEntry {
    pub path: String,
    pub name: String,
    pub reason: BlacklistReason,
    pub added_at: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub(super) enum BlacklistReason {
    Manual,
    Timeout,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct BlacklistFile {
    version: u32,
    entries: Vec<BlacklistEntry>,
}

pub(super) fn path_key(path: &str) -> String {
    #[cfg(windows)]
    {
        let path = path.replace('\\', "/").to_lowercase();
        if let Some(unc) = path.strip_prefix("//?/unc/") {
            return format!("//{unc}");
        }
        if let Some(drive) = path.strip_prefix("//?/") {
            let bytes = drive.as_bytes();
            if bytes.len() >= 3
                && bytes[0].is_ascii_alphabetic()
                && bytes[1] == b':'
                && bytes[2] == b'/'
            {
                return drive.to_owned();
            }
        }
        path
    }
    #[cfg(not(windows))]
    {
        path.to_owned()
    }
}

pub(super) fn path_matches(a: &str, b: &str) -> bool {
    path_key(a) == path_key(b)
}

fn validate_path(path: &str) -> Result<(), String> {
    if path.trim().is_empty() || path.contains('\0') {
        return Err("The plugin blacklist path is invalid".into());
    }
    Ok(())
}

fn display_name(path: &str) -> Result<String, String> {
    validate_path(path)?;
    if path == "au:system" {
        return Ok("Audio Units".into());
    }
    let name = path
        .trim_end_matches(['/', '\\'])
        .rsplit(['/', '\\'])
        .next()
        .filter(|name| !name.is_empty())
        .ok_or("The plugin blacklist path has no file name")?;
    Ok(name.to_owned())
}

fn validate_entries(entries: &[BlacklistEntry]) -> Result<(), String> {
    if entries.len() > MAX_ENTRIES {
        return Err(format!(
            "The plugin blacklist exceeds {MAX_ENTRIES} entries"
        ));
    }
    let mut paths = HashSet::with_capacity(entries.len());
    for entry in entries {
        validate_path(&entry.path)?;
        if entry.name.trim().is_empty() || entry.name.contains('\0') {
            return Err("The plugin blacklist contains an invalid name".into());
        }
        if !paths.insert(path_key(&entry.path)) {
            return Err("The plugin blacklist contains duplicate paths".into());
        }
    }
    Ok(())
}

pub(super) fn load(root: &Path) -> Result<Vec<BlacklistEntry>, String> {
    let path = root.join(FILE_NAME);
    let file = match std::fs::File::open(&path) {
        Ok(file) => file,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(format!("Cannot open the plugin blacklist: {error}")),
    };
    // A bounded reader also handles a file growing after open; metadata alone is insufficient.
    let mut bytes = Vec::new();
    file.take(MAX_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("Cannot read the plugin blacklist: {error}"))?;
    if bytes.len() > MAX_BYTES {
        return Err("The plugin blacklist exceeds its 2 MiB size limit".into());
    }
    let blacklist: BlacklistFile = serde_json::from_slice(&bytes)
        .map_err(|error| format!("The plugin blacklist is invalid: {error}"))?;
    if blacklist.version != VERSION {
        return Err(format!(
            "Unsupported plugin blacklist version: {}",
            blacklist.version
        ));
    }
    validate_entries(&blacklist.entries)?;
    Ok(blacklist.entries)
}

fn save(root: &Path, entries: Vec<BlacklistEntry>) -> Result<(), String> {
    validate_entries(&entries)?;
    let value = serde_json::to_value(BlacklistFile {
        version: VERSION,
        entries,
    })
    .map_err(|error| format!("Cannot encode the plugin blacklist: {error}"))?;
    // Match the storage writer's pretty encoding so add cannot create a file load rejects.
    let size = serde_json::to_vec_pretty(&value)
        .map_err(|error| format!("Cannot encode the plugin blacklist: {error}"))?
        .len();
    if size > MAX_BYTES {
        return Err("The plugin blacklist exceeds its 2 MiB size limit".into());
    }
    crate::storage::write_json_file(&root.join(FILE_NAME), &value)
        .map_err(|error| format!("Cannot save the plugin blacklist: {error}"))
}

pub(super) fn add(
    root: &Path,
    path: &str,
    reason: BlacklistReason,
) -> Result<BlacklistEntry, String> {
    let name = display_name(path)?;
    let mut entries = load(root)?;
    if let Some(existing) = entries.iter().find(|entry| path_matches(&entry.path, path)) {
        return Ok(existing.clone());
    }
    if entries.len() >= MAX_ENTRIES {
        return Err(format!(
            "The plugin blacklist exceeds {MAX_ENTRIES} entries"
        ));
    }
    let added_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "The system clock predates the Unix epoch")?
        .as_millis()
        .try_into()
        .map_err(|_| "The plugin blacklist timestamp exceeds its supported range")?;
    let entry = BlacklistEntry {
        path: path.into(),
        name,
        reason,
        added_at,
    };
    entries.push(entry.clone());
    save(root, entries)?;
    Ok(entry)
}

pub(super) fn remove(root: &Path, path: &str) -> Result<(), String> {
    validate_path(path)?;
    let mut entries = load(root)?;
    let previous_len = entries.len();
    entries.retain(|entry| !path_matches(&entry.path, path));
    if previous_len != entries.len() {
        save(root, entries)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn write(root: &Path, value: serde_json::Value) {
        std::fs::write(root.join(FILE_NAME), serde_json::to_vec(&value).unwrap()).unwrap();
    }

    #[test]
    fn entries_survive_reload_and_duplicate_add_keeps_first_reason_time_and_file() {
        let root = tempfile::tempdir().unwrap();
        assert!(load(root.path()).unwrap().is_empty());
        let entry = add(
            root.path(),
            "C:/Plugins/Delay.vst3",
            BlacklistReason::Timeout,
        )
        .unwrap();
        assert_eq!(entry.name, "Delay.vst3");
        assert!(entry.added_at > 0);
        assert_eq!(load(root.path()).unwrap(), [entry.clone()]);
        let original = std::fs::read(root.path().join(FILE_NAME)).unwrap();
        assert_eq!(
            add(root.path(), &entry.path, BlacklistReason::Manual).unwrap(),
            entry
        );
        assert_eq!(
            std::fs::read(root.path().join(FILE_NAME)).unwrap(),
            original
        );
        let persisted: serde_json::Value = serde_json::from_slice(&original).unwrap();
        assert_eq!(persisted["version"], 1);
        assert_eq!(persisted["entries"][0]["reason"], "timeout");
        assert_eq!(persisted["entries"][0]["addedAt"], entry.added_at);
    }

    #[test]
    fn removal_persists_and_missing_paths_do_not_create_or_rewrite_a_file() {
        let root = tempfile::tempdir().unwrap();
        remove(root.path(), "/missing.vst3").unwrap();
        assert!(!root.path().join(FILE_NAME).exists());
        add(root.path(), "/Plugins/One.vst", BlacklistReason::Manual).unwrap();
        let keep = add(root.path(), "/Plugins/Two.vst3", BlacklistReason::Timeout).unwrap();
        remove(root.path(), "/Plugins/One.vst").unwrap();
        assert_eq!(load(root.path()).unwrap(), [keep]);
        let original = std::fs::read(root.path().join(FILE_NAME)).unwrap();
        remove(root.path(), "/missing.vst3").unwrap();
        assert_eq!(
            std::fs::read(root.path().join(FILE_NAME)).unwrap(),
            original
        );
        remove(root.path(), "/Plugins/Two.vst3").unwrap();
        assert!(load(root.path()).unwrap().is_empty());
    }

    #[test]
    fn damaged_unknown_or_invalid_files_fail_closed_without_overwriting_them() {
        let root = tempfile::tempdir().unwrap();
        for bytes in [
            b"{".as_slice(),
            b"[]".as_slice(),
            b"{\"version\":2,\"entries\":[]}".as_slice(),
            b"{\"version\":1}".as_slice(),
            b"{\"version\":1,\"entries\":[{\"path\":\"x.vst3\",\"name\":\"x\",\"reason\":\"crash\",\"addedAt\":1}]}".as_slice(),
        ] {
            std::fs::write(root.path().join(FILE_NAME), bytes).unwrap();
            assert!(load(root.path()).is_err());
            assert!(add(root.path(), "valid.vst3", BlacklistReason::Manual).is_err());
            assert!(remove(root.path(), "valid.vst3").is_err());
            assert_eq!(std::fs::read(root.path().join(FILE_NAME)).unwrap(), bytes);
        }
    }

    #[test]
    fn oversized_files_and_entry_lists_are_rejected() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join(FILE_NAME), vec![b' '; MAX_BYTES + 1]).unwrap();
        assert!(load(root.path()).unwrap_err().contains("size limit"));
        let entries: Vec<_> = (0..=MAX_ENTRIES)
            .map(|index| {
                json!({
                    "path":format!("Effect{index}.vst3"), "name":format!("Effect{index}"),
                    "reason":"manual", "addedAt":1
                })
            })
            .collect();
        write(root.path(), json!({"version":1,"entries":entries}));
        assert!(load(root.path()).unwrap_err().contains("entries"));
    }

    #[test]
    fn mutations_cannot_write_an_oversized_or_overfull_blacklist() {
        let root = tempfile::tempdir().unwrap();
        let entries: Vec<_> = (0..MAX_ENTRIES)
            .map(|index| {
                json!({
                    "path":format!("Effect{index}.vst3"), "name":format!("Effect{index}"),
                    "reason":"manual", "addedAt":1
                })
            })
            .collect();
        write(root.path(), json!({"version":1,"entries":entries}));
        let original = std::fs::read(root.path().join(FILE_NAME)).unwrap();
        assert!(add(root.path(), "OneMore.vst3", BlacklistReason::Manual).is_err());
        assert_eq!(
            std::fs::read(root.path().join(FILE_NAME)).unwrap(),
            original
        );
        assert_eq!(
            add(root.path(), "Effect0.vst3", BlacklistReason::Timeout)
                .unwrap()
                .reason,
            BlacklistReason::Manual
        );
        // A long new path/name is bounded by the actual pretty-encoded file size.
        let root = tempfile::tempdir().unwrap();
        let first = add(root.path(), "First.vst3", BlacklistReason::Manual).unwrap();
        let original = std::fs::read(root.path().join(FILE_NAME)).unwrap();
        let long_path = format!("{}.vst3", "x".repeat(MAX_BYTES / 2));
        assert!(add(root.path(), &long_path, BlacklistReason::Manual).is_err());
        assert_eq!(
            std::fs::read(root.path().join(FILE_NAME)).unwrap(),
            original
        );
        assert_eq!(load(root.path()).unwrap(), [first]);
    }

    #[test]
    fn invalid_or_duplicate_entries_fail_closed() {
        let root = tempfile::tempdir().unwrap();
        for (path, name) in [
            ("", "Effect"),
            (" ", "Effect"),
            ("a\0b", "Effect"),
            ("Effect.vst", ""),
        ] {
            write(
                root.path(),
                json!({"version":1,"entries":[{
                    "path":path,"name":name,"reason":"manual","addedAt":0
                }]}),
            );
            assert!(load(root.path()).is_err());
        }
        let entry = json!({"path":"effect.vst3","name":"effect","reason":"manual","addedAt":1});
        write(
            root.path(),
            json!({"version":1,"entries":[entry.clone(),entry]}),
        );
        assert!(load(root.path()).unwrap_err().contains("duplicate"));
    }

    #[test]
    fn file_names_and_virtual_paths_require_no_plugin_files() {
        let root = tempfile::tempdir().unwrap();
        for (path, expected) in [
            ("C:\\Plugins\\Delay.vst3", "Delay.vst3"),
            ("/Library/Audio/Plug-Ins/VST/Gain.vst/", "Gain.vst"),
            ("au:system", "Audio Units"),
            ("au3:aufx:dely:appl", "au3:aufx:dely:appl"),
        ] {
            let entry = add(root.path(), path, BlacklistReason::Manual).unwrap();
            assert_eq!(entry.name, expected);
            assert_eq!(entry.path, path);
        }
        for invalid in ["", " ", "a\0b", "/"] {
            assert!(add(root.path(), invalid, BlacklistReason::Manual).is_err());
        }
    }

    #[test]
    fn path_comparison_matches_platform_semantics_without_resolving_paths() {
        assert!(path_matches("au:system", "au:system"));
        assert!(path_matches(
            "/nonexistent/Effect.vst3",
            "/nonexistent/Effect.vst3"
        ));
        #[cfg(windows)]
        {
            assert!(path_matches(
                "C:\\Plugins\\Delay.vst3",
                "c:/plugins/DELAY.VST3"
            ));
            assert!(path_matches(
                r"\\?\C:\Plugins\Delay.vst3",
                "c:/plugins/DELAY.VST3"
            ));
            assert!(path_matches(
                r"\\?\UNC\Server\Share\Delay.vst3",
                r"\\server\share\DELAY.VST3"
            ));
            assert!(!path_matches(
                r"\\.\C:\Plugins\Delay.vst3",
                "c:/plugins/Delay.vst3"
            ));
            let root = tempfile::tempdir().unwrap();
            let original = add(
                root.path(),
                "C:\\Plugins\\Delay.vst3",
                BlacklistReason::Timeout,
            )
            .unwrap();
            assert_eq!(
                add(
                    root.path(),
                    "c:/plugins/DELAY.VST3",
                    BlacklistReason::Manual
                )
                .unwrap(),
                original
            );
            remove(root.path(), "c:/PLUGINS/delay.vst3").unwrap();
            assert!(load(root.path()).unwrap().is_empty());
        }
        #[cfg(not(windows))]
        {
            assert!(!path_matches("/Plugins/Delay.vst3", "/plugins/DELAY.VST3"));
            assert!(!path_matches("a\\b.vst3", "a/b.vst3"));
        }
    }
}
