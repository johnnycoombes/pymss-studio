use std::collections::HashSet;
use std::path::{Path, PathBuf};

pub const AAX_HOST_UNAVAILABLE: &str = "AAX plugin hosting is not supported in this build";

/// Known native packages that must not be opened through another format's loader.
pub fn unsupported_plugin_reason(path: &Path) -> Option<&'static str> {
    path.ancestors()
        .any(|path| {
            extension(path).is_some_and(|extension| extension.eq_ignore_ascii_case("aaxplugin"))
        })
        .then_some(AAX_HOST_UNAVAILABLE)
}

pub fn supported_formats() -> Vec<&'static str> {
    let mut formats = vec!["vst3", "vst2"];
    if cfg!(target_os = "macos") {
        formats.push("au");
    }
    formats
}

pub fn native_editor_formats() -> Vec<&'static str> {
    let mut formats = vec!["vst3", "vst2"];
    if soundcraft_au_host::NATIVE_EDITOR_SUPPORTED {
        formats.push("au");
    }
    formats
}

pub(crate) fn supports_format(format: &str) -> bool {
    matches!(format, "vst3" | "vst2") || (format == "au" && cfg!(target_os = "macos"))
}

fn extension(path: &Path) -> Option<&str> {
    path.extension()?.to_str()
}

pub fn is_plugin_path(path: &Path) -> bool {
    let Some(extension) = extension(path) else {
        return false;
    };
    extension.eq_ignore_ascii_case("vst3")
        || extension.eq_ignore_ascii_case("aaxplugin")
        || extension.eq_ignore_ascii_case(if cfg!(windows) {
            "dll"
        } else if cfg!(target_os = "macos") {
            "vst"
        } else {
            "so"
        })
}

pub(crate) fn is_vst3_path(path: &Path) -> bool {
    extension(path).is_some_and(|extension| extension.eq_ignore_ascii_case("vst3"))
}

/// Standard plugin directories and optional VST_PATH/VST3_PATH entries. Scanning never
/// loads a binary; the application probes each discovered candidate in a separate process.
pub fn default_search_paths() -> Vec<PathBuf> {
    let mut paths = soundcraft_vst3_host::scan::default_search_paths();
    #[cfg(windows)]
    {
        if let Some(root) = std::env::var_os("CommonProgramFiles") {
            paths.push(PathBuf::from(root).join("VST2"));
        }
        if let Some(root) = std::env::var_os("ProgramFiles") {
            paths.push(PathBuf::from(&root).join("VstPlugins"));
            paths.push(PathBuf::from(root).join("Steinberg/VstPlugins"));
        }
    }
    #[cfg(target_os = "macos")]
    {
        if let Some(home) = std::env::var_os("HOME") {
            paths.push(PathBuf::from(home).join("Library/Audio/Plug-Ins/VST"));
        }
        paths.push(PathBuf::from("/Library/Audio/Plug-Ins/VST"));
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        if let Some(home) = std::env::var_os("HOME") {
            paths.push(PathBuf::from(home).join(".vst"));
        }
        paths.extend(
            [
                "/usr/lib/vst",
                "/usr/lib64/vst",
                "/usr/local/lib/vst",
                "/usr/local/lib64/vst",
            ]
            .map(PathBuf::from),
        );
    }
    if let Some(extra) = std::env::var_os("VST_PATH") {
        paths.extend(std::env::split_paths(&extra).filter(|path| !path.as_os_str().is_empty()));
    }
    let mut seen = HashSet::new();
    paths.retain(|path| seen.insert(path.clone()));
    paths
}

pub fn find_bundles(paths: &[PathBuf]) -> Vec<PathBuf> {
    let defaults;
    let paths = if paths.is_empty() {
        defaults = default_search_paths();
        &defaults
    } else {
        paths
    };
    let mut found = Vec::new();
    let mut entries = 100_000;
    for path in paths {
        visit(path, 0, &mut entries, &mut found);
        if entries == 0 || found.len() >= 4096 {
            break;
        }
    }
    found.sort();
    found.dedup();
    found
}

fn visit(path: &Path, depth: usize, budget: &mut usize, found: &mut Vec<PathBuf>) {
    if depth > 8 || *budget == 0 || found.len() >= 4096 {
        return;
    }
    *budget -= 1;
    let Ok(metadata) = std::fs::metadata(path) else {
        return;
    };
    if is_plugin_path(path) {
        // VST3/VST2 macOS packages are terminals; do not discover their embedded libraries twice.
        if metadata.is_file()
            || (metadata.is_dir()
                && extension(path).is_some_and(|extension| {
                    extension.eq_ignore_ascii_case("vst3")
                        || extension.eq_ignore_ascii_case("vst")
                        || extension.eq_ignore_ascii_case("aaxplugin")
                }))
        {
            found.push(path.to_path_buf());
        }
    } else if metadata.is_dir() {
        let Ok(children) = std::fs::read_dir(path) else {
            return;
        };
        for child in children.flatten() {
            visit(&child.path(), depth + 1, budget, found);
            if *budget == 0 || found.len() >= 4096 {
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn editor_capabilities_include_vst2_and_only_expose_audio_units_on_macos() {
        assert!(native_editor_formats().contains(&"vst2"));
        assert_eq!(
            native_editor_formats().contains(&"au"),
            cfg!(target_os = "macos")
        );
        assert!(!supported_formats().contains(&"aax"));
    }
    #[test]
    fn discovers_vst2_and_vst3_without_descending_into_packages() {
        let root = tempfile::tempdir().unwrap();
        let package = root.path().join("Effect.vst3");
        std::fs::create_dir_all(package.join("Contents")).unwrap();
        std::fs::write(package.join("Contents/Nested.vst3"), b"module").unwrap();
        let binary = root.path().join(if cfg!(windows) {
            "Legacy.DLL"
        } else if cfg!(target_os = "macos") {
            "Legacy.vst"
        } else {
            "Legacy.so"
        });
        std::fs::write(&binary, b"module").unwrap();
        std::fs::write(root.path().join("Readme.txt"), b"text").unwrap();
        let found = find_bundles(&[root.path().to_path_buf(), binary.clone()]);
        assert_eq!(found.len(), 2);
        assert!(found.contains(&package));
        assert!(found.contains(&binary));
    }
    #[test]
    fn candidate_budget_is_shared_across_roots() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("Effect.vst3");
        std::fs::write(&path, b"module").unwrap();
        let mut found = Vec::new();
        let mut budget = 1;
        visit(&path, 0, &mut budget, &mut found);
        visit(&path, 0, &mut budget, &mut found);
        assert_eq!(found, vec![path]);
    }

    #[test]
    fn aax_packages_are_terminal_and_never_discovered_as_vst_libraries() {
        let directory = tempfile::tempdir().unwrap();
        let bundle = directory.path().join("Effect.aaxplugin");
        std::fs::create_dir_all(bundle.join("Contents")).unwrap();
        std::fs::write(bundle.join("Contents/Effect.dll"), b"module").unwrap();
        std::fs::write(bundle.join("Contents/Effect.so"), b"module").unwrap();
        std::fs::write(bundle.join("Contents/Effect.vst3"), b"module").unwrap();
        assert_eq!(
            find_bundles(&[directory.path().to_owned()]),
            vec![bundle.clone()]
        );
        assert_eq!(
            unsupported_plugin_reason(&bundle),
            Some(AAX_HOST_UNAVAILABLE)
        );
        assert_eq!(
            unsupported_plugin_reason(&bundle.join("Contents/Effect.dll")),
            Some(AAX_HOST_UNAVAILABLE)
        );
        assert!(unsupported_plugin_reason(Path::new("Effect.vst3")).is_none());
    }
}
