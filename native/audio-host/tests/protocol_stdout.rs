//! Regression for third-party printf/OS/Rust stdout corrupting helper JSON. This loads only
//! the local native fixture, creates no editor window, and opens no audio output stream.
use serde_json::{Value, json};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

fn fixture_bundle(root: &Path) -> PathBuf {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../vendor/soundcraft-vst3-host/tests/fixture/Cargo.toml");
    let target = Path::new(env!("CARGO_TARGET_TMPDIR")).join("audio-host-stdout-fixture-target");
    assert!(
        Command::new(std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into()))
            .args(["build", "--locked", "--quiet", "--manifest-path"])
            .arg(manifest)
            .arg("--target-dir")
            .arg(&target)
            .env_remove("RUSTFLAGS")
            .status()
            .unwrap()
            .success()
    );
    let (library, folder, binary) = if cfg!(windows) {
        ("vst3_fixture.dll", "x86_64-win", "Fixture.vst3")
    } else if cfg!(target_os = "macos") {
        ("libvst3_fixture.dylib", "MacOS", "Fixture")
    } else {
        (
            "libvst3_fixture.so",
            if cfg!(target_arch = "aarch64") {
                "aarch64-linux"
            } else {
                "x86_64-linux"
            },
            "Fixture.so",
        )
    };
    let bundle = root.join("Fixture.vst3");
    let directory = bundle.join("Contents").join(folder);
    std::fs::create_dir_all(&directory).unwrap();
    std::fs::copy(target.join("debug").join(library), directory.join(binary)).unwrap();
    bundle
}

#[test]
fn plugin_stdout_never_enters_scan_or_session_responses() {
    let directory = tempfile::tempdir().unwrap();
    let bundle = fixture_bundle(directory.path());
    let executable = env!("CARGO_BIN_EXE_pymss-audio-host");
    let scan = Command::new(executable)
        .arg("--plugin-scan")
        .arg(&bundle)
        .env("PYMSS_FIXTURE_STDOUT_NOISE", "1")
        .output()
        .unwrap();
    assert!(
        scan.status.success(),
        "{}",
        String::from_utf8_lossy(&scan.stderr)
    );
    let result: Value =
        serde_json::from_slice(&scan.stdout).expect("scanner must emit exactly one JSON object");
    let plugin = result["plugins"]
        .as_array()
        .unwrap()
        .iter()
        .find(|plugin| plugin["name"] == "Fixture Gain")
        .unwrap();
    let mut child = Command::new(executable)
        .arg("--audio-host")
        .env("PYMSS_FIXTURE_STDOUT_NOISE", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let project = json!({"id":"stdout", "name":"Audio", "sources":[], "tracks":[], "pluginChain":[{
        "id":"slot", "name":"Gain", "format":"vst3", "pluginId":plugin["id"], "path":bundle, "bypassed":false
    }]});
    let mut input = child.stdin.take().unwrap();
    for request in [
        json!({"id":1,"op":"load","project":project,"ffmpeg":executable}),
        json!({"id":2,"op":"parameters","projectId":"stdout","slotId":"slot"}),
        json!({"id":3,"op":"snapshot","projectId":"stdout"}),
        json!({"id":4,"op":"shutdown"}),
    ] {
        serde_json::to_writer(&mut input, &request).unwrap();
        input.write_all(b"\n").unwrap();
    }
    drop(input);
    let deadline = Instant::now() + Duration::from_secs(30);
    // This fixture's four bounded responses fit in the pipe. Bound teardown independently
    // so a future helper shutdown regression fails the test instead of hanging the CI job.
    while child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("Audio helper did not shut down after its final request");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let responses: Vec<Value> = String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| {
            serde_json::from_str(line).expect("plugin diagnostics must never reach JSON parser")
        })
        .collect();
    assert_eq!(responses.len(), 4);
    for (index, response) in responses.iter().enumerate() {
        assert_eq!(response["id"], index + 1);
        assert_eq!(response["ok"], true, "{response}");
    }
    assert!(responses[2]["result"]["states"]["slot"].is_string());
}
