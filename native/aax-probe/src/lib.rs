//! AAX module inspection, not an AAX audio-processing host.
//!
//! This executes module initialization and ACFGetSDKVersion. Call it only in a
//! disposable helper process. No ACF host services or processing contexts exist here.

use serde::Serialize;
use std::fs;
use std::io::{Cursor, Read};
use std::path::{Component, Path, PathBuf};

const MAX_INFO_BYTES: u64 = 1024 * 1024;
const MAX_BINARY_ENTRIES: usize = 64;
pub const ACF_EXPORTS: [&str; 7] = [
    "ACFRegisterPlugin",
    "ACFRegisterComponent",
    "ACFGetClassFactory",
    "ACFCanUnloadNow",
    "ACFStartup",
    "ACFShutdown",
    "ACFGetSDKVersion",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PackageLayout {
    WindowsX64,
    MacOs,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProbeReport {
    pub binary_path: PathBuf,
    pub module_loaded: bool,
    pub found_exports: Vec<String>,
    pub missing_exports: Vec<String>,
    pub sdk_version: Option<String>,
    pub sdk_version_result: Option<String>,
    pub sdk_version_valid: bool,
    pub audio_processing_available: bool,
}

/// Resolve an explicitly selected AAX file/package without searching other roots.
pub fn resolve_binary(path: &Path, layout: PackageLayout) -> Result<PathBuf, String> {
    if !path.is_absolute() {
        return Err("AAX package path must be absolute".into());
    }
    if !path
        .extension()
        .is_some_and(|extension| extension.eq_ignore_ascii_case("aaxplugin"))
    {
        return Err("The selected path is not an AAX package".into());
    }
    let metadata =
        fs::metadata(path).map_err(|error| format!("Cannot read AAX package: {error}"))?;
    if metadata.is_file() {
        return Ok(path.to_path_buf());
    }
    if !metadata.is_dir() {
        return Err("The selected AAX package is not a file or directory".into());
    }
    let folder = path.join(match layout {
        PackageLayout::WindowsX64 => "Contents/x64",
        PackageLayout::MacOs => "Contents/MacOS",
    });
    if layout == PackageLayout::MacOs {
        let info_path = path.join("Contents/Info.plist");
        if info_path.exists() {
            let mut bytes = Vec::new();
            fs::File::open(&info_path)
                .map_err(|error| format!("Cannot read AAX Info.plist: {error}"))?
                .take(MAX_INFO_BYTES + 1)
                .read_to_end(&mut bytes)
                .map_err(|error| error.to_string())?;
            if bytes.len() as u64 > MAX_INFO_BYTES {
                return Err("AAX Info.plist exceeds the size limit".into());
            }
            let info = plist::Value::from_reader(Cursor::new(bytes))
                .map_err(|error| format!("Invalid AAX Info.plist: {error}"))?;
            let name = info
                .as_dictionary()
                .and_then(|dictionary| dictionary.get("CFBundleExecutable"))
                .and_then(plist::Value::as_string)
                .ok_or("AAX package has no CFBundleExecutable")?;
            let mut components = Path::new(name).components();
            if name.is_empty()
                || name.contains(['/', '\\'])
                || !matches!(components.next(), Some(Component::Normal(_)))
                || components.next().is_some()
            {
                return Err("AAX CFBundleExecutable must be a file name".into());
            }
            let binary = folder.join(name);
            return binary
                .is_file()
                .then_some(binary)
                .ok_or_else(|| "The AAX package executable is missing".into());
        }
    }
    let expected_name = match layout {
        PackageLayout::WindowsX64 => path.file_name(),
        PackageLayout::MacOs => path.file_stem(),
    };
    if let Some(name) = expected_name {
        let named = folder.join(name);
        if named.is_file() {
            return Ok(named);
        }
    }
    let mut found = Vec::new();
    let entries = fs::read_dir(&folder)
        .map_err(|error| format!("Cannot read AAX binary directory: {error}"))?;
    for (index, entry) in entries.enumerate() {
        if index >= MAX_BINARY_ENTRIES {
            return Err("AAX binary directory exceeds the entry limit".into());
        }
        let entry = entry.map_err(|error| error.to_string())?;
        let binary = entry.path();
        let candidate = match layout {
            PackageLayout::WindowsX64 => binary
                .extension()
                .is_some_and(|extension| extension.eq_ignore_ascii_case("aaxplugin")),
            PackageLayout::MacOs => binary.extension().is_none(),
        };
        if candidate && binary.is_file() {
            found.push(binary);
        }
    }
    match found.len() {
        1 => Ok(found.remove(0)),
        0 => Err("No AAX binary was found for this platform".into()),
        _ => Err("The AAX package contains ambiguous executable candidates".into()),
    }
}

/// Inspect exports/version only. A successful probe never enables audio processing.
pub fn probe(path: &Path) -> Result<ProbeReport, String> {
    #[cfg(all(windows, target_arch = "x86_64"))]
    let layout = PackageLayout::WindowsX64;
    #[cfg(all(
        target_os = "macos",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    let layout = PackageLayout::MacOs;
    #[cfg(any(
        all(windows, target_arch = "x86_64"),
        all(
            target_os = "macos",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    ))]
    {
        module::inspect(resolve_binary(path, layout)?)
    }
    #[cfg(not(any(
        all(windows, target_arch = "x86_64"),
        all(
            target_os = "macos",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    )))]
    {
        let _ = path;
        Err("AAX module probing requires Windows x64 or macOS 64-bit".into())
    }
}

#[cfg(any(
    all(windows, target_arch = "x86_64"),
    all(
        target_os = "macos",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )
))]
#[allow(unsafe_code)]
mod module {
    use super::*;
    use libloading::Library;

    // AAX_Exports.cpp / acfbasetypes.h: 32-bit ACFRESULT, 64-bit output;
    // __stdcall on Windows, C ABI on macOS. See ABI-SOURCES.txt.
    type GetSdkVersion = unsafe extern "system" fn(*mut u64) -> i32;

    pub(super) fn inspect(binary_path: PathBuf) -> Result<ProbeReport, String> {
        // SAFETY: the helper's explicitly selected module is trusted native code.
        // All borrowed symbols are consumed before this library is dropped.
        let library = unsafe { Library::new(&binary_path) }
            .map_err(|error| format!("Cannot load AAX module: {error}"))?;
        let mut found_exports = Vec::new();
        let mut missing_exports = Vec::new();
        for name in ACF_EXPORTS {
            // SAFETY: this address is inspected for presence only and is never invoked.
            if unsafe { library.get::<unsafe extern "system" fn()>(name.as_bytes()) }.is_ok() {
                found_exports.push(name.into());
            } else {
                missing_exports.push(name.into());
            }
        }
        let mut sdk_version = None;
        let mut sdk_version_result = None;
        // SAFETY: the official export accepts one valid writable u64 pointer and
        // does not require ACFStartup/host services to return its static SDK version.
        if let Ok(query) = unsafe { library.get::<GetSdkVersion>(b"ACFGetSDKVersion") } {
            let mut version = 0u64;
            let result = unsafe { query(&mut version) };
            sdk_version_result = Some(format!("0x{:08X}", result as u32));
            // ACFSUCCEEDED uses >= 0, not equality with 1 or 0.
            if result >= 0 && version != 0 {
                sdk_version = Some(format!("0x{version:016X}"));
            }
        }
        Ok(ProbeReport {
            binary_path,
            module_loaded: true,
            found_exports,
            missing_exports,
            sdk_version_valid: sdk_version.is_some(),
            sdk_version,
            sdk_version_result,
            audio_processing_available: false,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn package(name: &str) -> (tempfile::TempDir, PathBuf) {
        let root = tempfile::tempdir().unwrap();
        let package = root.path().join(name);
        fs::create_dir_all(package.join("Contents/x64")).unwrap();
        fs::create_dir_all(package.join("Contents/MacOS")).unwrap();
        (root, package)
    }

    #[test]
    fn windows_package_selects_x64_binary_without_searching_other_architectures() {
        let (_root, package) = package("音频 Effect.aaxplugin");
        let binary = package.join("Contents/x64/音频 Effect.aaxplugin");
        fs::write(&binary, b"module").unwrap();
        fs::create_dir_all(package.join("Contents/Win32")).unwrap();
        fs::write(
            package.join("Contents/Win32/音频 Effect.aaxplugin"),
            b"other",
        )
        .unwrap();
        assert_eq!(
            resolve_binary(&package, PackageLayout::WindowsX64).unwrap(),
            binary
        );
    }

    #[test]
    fn windows_package_names_with_dots_select_the_exact_matching_module() {
        let (_root, package) = package("Compressor.v2.aaxplugin");
        let binary = package.join("Contents/x64/Compressor.v2.aaxplugin");
        fs::write(&binary, b"selected").unwrap();
        fs::write(package.join("Contents/x64/Compressor.aaxplugin"), b"other").unwrap();
        assert_eq!(
            resolve_binary(&package, PackageLayout::WindowsX64).unwrap(),
            binary
        );
    }

    #[test]
    fn macos_package_honors_the_declared_executable() {
        let (_root, package) = package("Effect.aaxplugin");
        let binary = package.join("Contents/MacOS/ActualProcessor");
        fs::write(&binary, b"module").unwrap();
        fs::write(package.join("Contents/Info.plist"), br#"<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleExecutable</key><string>ActualProcessor</string></dict></plist>"#).unwrap();
        assert_eq!(
            resolve_binary(&package, PackageLayout::MacOs).unwrap(),
            binary
        );
    }

    #[test]
    fn unsafe_missing_or_oversized_bundle_metadata_is_rejected() {
        let (_root, package) = package("Effect.aaxplugin");
        for name in [
            "../outside",
            "nested/processor",
            "nested\\processor",
            "",
            "..",
        ] {
            let mut info = plist::Dictionary::new();
            info.insert("CFBundleExecutable".into(), name.into());
            plist::Value::Dictionary(info)
                .to_file_xml(package.join("Contents/Info.plist"))
                .unwrap();
            assert!(resolve_binary(&package, PackageLayout::MacOs).is_err());
        }
        fs::write(
            package.join("Contents/Info.plist"),
            vec![b' '; MAX_INFO_BYTES as usize + 1],
        )
        .unwrap();
        assert!(
            resolve_binary(&package, PackageLayout::MacOs)
                .unwrap_err()
                .contains("size limit")
        );
    }

    #[test]
    fn fallback_candidates_must_be_unique_and_bounded() {
        let (_root, package) = package("Effect.aaxplugin");
        let binary = package.join("Contents/x64/Actual.aaxplugin");
        fs::write(&binary, b"module").unwrap();
        assert_eq!(
            resolve_binary(&package, PackageLayout::WindowsX64).unwrap(),
            binary
        );
        fs::write(package.join("Contents/x64/Other.aaxplugin"), b"module").unwrap();
        assert!(
            resolve_binary(&package, PackageLayout::WindowsX64)
                .unwrap_err()
                .contains("ambiguous")
        );
        for index in 0..MAX_BINARY_ENTRIES {
            fs::write(package.join(format!("Contents/x64/{index}.txt")), b"text").unwrap();
        }
        assert!(
            resolve_binary(&package, PackageLayout::WindowsX64)
                .unwrap_err()
                .contains("entry limit")
        );
    }

    #[test]
    fn relative_missing_and_non_aax_paths_are_rejected() {
        assert!(resolve_binary(Path::new("Effect.aaxplugin"), PackageLayout::WindowsX64).is_err());
        let root = tempfile::tempdir().unwrap();
        assert!(
            resolve_binary(
                &root.path().join("Missing.aaxplugin"),
                PackageLayout::WindowsX64
            )
            .is_err()
        );
        let not_aax = root.path().join("Other.vst3");
        fs::write(&not_aax, b"module").unwrap();
        assert!(resolve_binary(&not_aax, PackageLayout::WindowsX64).is_err());
    }

    #[cfg(not(any(
        all(windows, target_arch = "x86_64"),
        all(
            target_os = "macos",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )
    )))]
    #[test]
    fn unsupported_platforms_never_load_a_module() {
        assert!(
            probe(Path::new("unused.aaxplugin"))
                .unwrap_err()
                .contains("requires Windows")
        );
    }
}
