#![cfg(any(
    all(windows, target_arch = "x86_64"),
    all(
        target_os = "macos",
        any(target_arch = "x86_64", target_arch = "aarch64")
    )
))]

use pymss_aax_probe::probe;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

// Local ABI fixtures only. These have no AAX processor or host acceptance claim.
const FIXTURE: &str = include_str!("fixture/module.rs");

fn build(root: &Path, name: &str, configuration: Option<&str>) -> PathBuf {
    let source = root.join(format!("{name}.rs"));
    let binary = root.join(format!("{name}.aaxplugin"));
    fs::write(&source, FIXTURE).unwrap();
    let mut command = Command::new("rustc");
    command.args([
        "--edition=2021",
        "--crate-type=cdylib",
        "--crate-name",
        name,
    ]);
    if let Some(configuration) = configuration {
        command.args(["--cfg", configuration]);
    }
    let output = command
        .arg(&source)
        .arg("-o")
        .arg(&binary)
        .output()
        .expect("rustc must be available for ABI fixtures");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    binary
}

#[test]
fn module_exports_and_version_probe_never_advertise_audio_processing() {
    let root = tempfile::tempdir().unwrap();
    for (name, configuration, version, status) in [
        ("complete", None, true, Some("0x00000000")),
        (
            "positive",
            Some("positive_success"),
            true,
            Some("0x00000001"),
        ),
        ("failure", Some("failure"), false, Some("0x80004005")),
        ("empty", Some("zero_version"), false, Some("0x00000000")),
        ("missing", Some("missing_sdk"), false, None),
    ] {
        let binary = build(root.path(), name, configuration);
        let report = probe(&binary).unwrap();
        assert!(report.module_loaded);
        assert!(!report.audio_processing_available);
        assert_eq!(report.sdk_version_valid, version);
        assert_eq!(report.sdk_version_result.as_deref(), status);
        if version {
            assert_eq!(report.sdk_version.as_deref(), Some("0x0208000100000002"));
        } else {
            assert!(report.sdk_version.is_none());
        }
        assert_eq!(
            report.missing_exports.contains(&"ACFGetSDKVersion".into()),
            configuration == Some("missing_sdk")
        );
        assert_eq!(report.found_exports.len() + report.missing_exports.len(), 7);
    }
}

#[test]
fn non_loadable_binaries_report_the_loader_error() {
    let root = tempfile::tempdir().unwrap();
    let binary = root.path().join("Invalid.aaxplugin");
    fs::write(&binary, b"not a native module").unwrap();
    assert!(
        probe(&binary)
            .unwrap_err()
            .contains("Cannot load AAX module")
    );
}
