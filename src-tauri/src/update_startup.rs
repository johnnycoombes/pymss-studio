use crate::error::{AppError, AppResult};
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::time::{Duration, Instant};

fn failure(message: impl Into<String>) -> AppError {
    AppError::Worker(message.into())
}

#[derive(Clone, Deserialize, Serialize)]
pub(super) struct Request {
    pub token: String,
    pub target: PathBuf,
    pub version: String,
    pub privileged: bool,
}

#[derive(Deserialize, Serialize)]
struct Acknowledgement {
    token: String,
    target: PathBuf,
    version: String,
    pid: u32,
}

fn startup_process_matches(launcher: u32, pid: u32, token: &str) -> bool {
    if pid == launcher {
        return true;
    }
    #[cfg(target_os = "linux")]
    if token.starts_with("linux-update-startup-") {
        // Extract-and-run keeps a launcher process alive while its AppRun child
        // runs. Accept that child only while its ancestry still leads to ours.
        let mut current = pid;
        for _ in 0..64 {
            let Ok(stat) = fs::read_to_string(format!("/proc/{current}/stat")) else {
                return false;
            };
            let Some((_, fields)) = stat.rsplit_once(')') else {
                return false;
            };
            let mut fields = fields.split_whitespace();
            if matches!(fields.next(), None | Some("Z") | Some("X")) {
                return false;
            }
            let Some(parent) = fields.next().and_then(|value| value.parse::<u32>().ok()) else {
                return false;
            };
            if parent == launcher {
                return true;
            }
            if parent < 2 || parent == current {
                return false;
            }
            current = parent;
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = token;
    false
}

pub(super) fn create_directory(parent: &Path, prefix: &str) -> std::io::Result<tempfile::TempDir> {
    let mut builder = tempfile::Builder::new();
    builder.prefix(prefix);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        builder.permissions(fs::Permissions::from_mode(0o700));
    }
    builder.tempdir_in(parent)
}

#[cfg(unix)]
pub(super) fn secure_directory(directory: &Path) -> AppResult<()> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let metadata = fs::symlink_metadata(directory)?;
    // Earlier updaters used tempfile's default directory mode (usually 0755).
    // Accept owner-controlled legacy directories, then make the acknowledgement
    // private. A symlink or a directory writable by another user is never trusted.
    // SAFETY: geteuid takes no arguments and has no preconditions.
    let user_id = unsafe { libc::geteuid() };
    if !metadata.is_dir() || metadata.uid() != user_id || metadata.mode() & 0o022 != 0 {
        return Err(failure(
            "The application startup transaction directory is invalid",
        ));
    }
    fs::set_permissions(directory, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

pub(super) fn valid_token(token: &str) -> bool {
    token
        .strip_prefix("macos-update-startup-")
        .or_else(|| token.strip_prefix("linux-update-startup-"))
        .is_some_and(|suffix| {
            (6..=64).contains(&suffix.len())
                && suffix.bytes().all(|byte| byte.is_ascii_alphanumeric())
        })
}

pub(super) fn argument_token(args: impl Iterator<Item = String>) -> AppResult<Option<String>> {
    let mut args = args;
    let mut token = None;
    while let Some(argument) = args.next() {
        if argument == "--pymss-update-startup" {
            let value = args
                .next()
                .ok_or_else(|| failure("Missing application startup confirmation token"))?;
            if token.is_some() || !valid_token(&value) {
                return Err(failure("Invalid application startup confirmation token"));
            }
            token = Some(value);
        }
    }
    Ok(token)
}

pub(super) fn save_request(directory: &Path, request: &Request) -> AppResult<()> {
    if !valid_token(&request.token) || directory.file_name() != Some(request.token.as_ref()) {
        return Err(failure("Invalid application startup transaction directory"));
    }
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join("request.json"))?;
    file.write_all(&serde_json::to_vec(request)?)?;
    file.sync_all()?;
    Ok(())
}

pub(super) fn read_request(directory: &Path) -> AppResult<Request> {
    let mut bytes = Vec::new();
    fs::File::open(directory.join("request.json"))?
        .take(16 * 1024)
        .read_to_end(&mut bytes)?;
    let request: Request = serde_json::from_slice(&bytes)?;
    if !valid_token(&request.token) || directory.file_name() != Some(request.token.as_ref()) {
        return Err(failure("Invalid application startup transaction"));
    }
    Ok(request)
}

pub(super) fn acknowledge(directory: &Path, request: &Request, pid: u32) -> AppResult<()> {
    let acknowledgement = Acknowledgement {
        token: request.token.clone(),
        target: request.target.clone(),
        version: request.version.clone(),
        pid,
    };
    let pending = directory.join("ack.pending");
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&pending)?;
    file.write_all(&serde_json::to_vec(&acknowledgement)?)?;
    file.sync_all()?;
    drop(file);
    fs::rename(pending, directory.join("ack.json"))?;
    Ok(())
}

pub(super) fn wait_for_acknowledgement(
    child: &mut Child,
    directory: &Path,
    request: &Request,
    timeout: Duration,
) -> AppResult<()> {
    let started = Instant::now();
    let mut confirmed = None;
    loop {
        if let Some(status) = child.try_wait()? {
            return Err(failure(format!(
                "The updated application exited before startup completed: {status}"
            )));
        }
        if confirmed.is_none() {
            match fs::File::open(directory.join("ack.json")) {
                Ok(file) => {
                    let mut bytes = Vec::new();
                    file.take(16 * 1024).read_to_end(&mut bytes)?;
                    let ack: Acknowledgement = serde_json::from_slice(&bytes)?;
                    if !startup_process_matches(child.id(), ack.pid, &request.token)
                        || ack.token != request.token
                        || ack.version != request.version
                        || ack.target != request.target
                    {
                        return Err(failure(
                            "The application startup acknowledgement does not match this update",
                        ));
                    }
                    confirmed = Some((Instant::now(), ack.pid));
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
                Err(error) => return Err(error.into()),
            }
        }
        if confirmed
            .is_some_and(|(_, pid)| !startup_process_matches(child.id(), pid, &request.token))
        {
            return Err(failure(
                "The updated application exited before startup completed",
            ));
        }
        if confirmed.is_some_and(|(at, _)| at.elapsed() >= Duration::from_millis(250)) {
            return Ok(());
        }
        if started.elapsed() >= timeout {
            return Err(failure(
                "The updated application did not confirm startup in time",
            ));
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}

pub(super) fn stop_child(child: &mut Child) -> AppResult<()> {
    if child.try_wait()?.is_none() {
        child.kill()?;
    }
    child.wait()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};

    #[cfg(unix)]
    #[test]
    fn startup_directory_is_private_and_seals_legacy_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let parent = tempfile::tempdir().unwrap();
        let directory = create_directory(parent.path(), "macos-update-startup-").unwrap();
        assert_eq!(
            fs::metadata(directory.path()).unwrap().permissions().mode() & 0o777,
            0o700
        );
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o755)).unwrap();
        secure_directory(directory.path()).unwrap();
        assert_eq!(
            fs::metadata(directory.path()).unwrap().permissions().mode() & 0o777,
            0o700
        );
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o775)).unwrap();
        assert!(secure_directory(directory.path()).is_err());
        fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let alias = parent.path().join("alias");
        std::os::unix::fs::symlink(directory.path(), &alias).unwrap();
        assert!(secure_directory(&alias).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn startup_directories_keep_platform_tokens_and_private_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let parent = tempfile::tempdir().unwrap();
        for prefix in ["macos-update-startup-", "linux-update-startup-"] {
            let directory = create_directory(parent.path(), prefix).unwrap();
            let token = directory.path().file_name().unwrap().to_str().unwrap();
            assert!(token.starts_with(prefix));
            assert!(valid_token(token));
            assert_eq!(
                fs::metadata(directory.path()).unwrap().permissions().mode() & 0o777,
                0o700
            );
        }
    }
    #[test]
    fn token_arguments_cannot_select_arbitrary_paths() {
        for invalid in [
            "../settings",
            "macos-update-startup-../settings",
            "macos-update-startup-short",
            "",
        ] {
            assert!(!valid_token(invalid));
        }
        assert!(valid_token("macos-update-startup-aBc123"));
        assert!(valid_token("linux-update-startup-aBc123"));
        assert!(argument_token(["--pymss-update-startup".into()].into_iter()).is_err());
    }

    #[test]
    fn startup_child() {
        let Ok(mode) = std::env::var("PYMSS_STARTUP_TEST_MODE") else {
            return;
        };
        let directory = PathBuf::from(std::env::var_os("PYMSS_STARTUP_TEST_DIRECTORY").unwrap());
        #[cfg(target_os = "linux")]
        if mode.starts_with("wrapper") {
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "update_startup::tests::startup_child",
                    "--nocapture",
                ])
                .env(
                    "PYMSS_STARTUP_TEST_MODE",
                    if mode == "wrapper" {
                        "ready"
                    } else {
                        "exit_after_ack"
                    },
                )
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            let status = child.wait().unwrap();
            std::thread::sleep(Duration::from_secs(5));
            std::process::exit(status.code().unwrap_or(1));
        }
        if mode == "exit" {
            std::process::exit(42);
        }
        if mode != "timeout" {
            let mut request = read_request(&directory).unwrap();
            if mode == "forged" {
                request.version = "99.0.0".into();
            }
            acknowledge(&directory, &request, std::process::id()).unwrap();
            if mode == "exit_after_ack" {
                std::process::exit(42);
            }
        }
        std::thread::sleep(Duration::from_secs(5));
    }

    fn run_child(mode: &str) -> (tempfile::TempDir, Request, Child) {
        let directory = tempfile::Builder::new()
            .prefix(if mode.starts_with("wrapper") {
                "linux-update-startup-"
            } else {
                "macos-update-startup-"
            })
            .tempdir()
            .unwrap();
        let request = Request {
            token: directory
                .path()
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
                .into(),
            target: directory.path().join("Installed.app"),
            version: "1.2.3".into(),
            privileged: false,
        };
        save_request(directory.path(), &request).unwrap();
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "update_startup::tests::startup_child",
                "--nocapture",
            ])
            .env("PYMSS_STARTUP_TEST_MODE", mode)
            .env("PYMSS_STARTUP_TEST_DIRECTORY", directory.path())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(target_os = "linux")]
        if mode.starts_with("wrapper") {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let child = command.spawn().unwrap();
        (directory, request, child)
    }

    #[test]
    fn startup_requires_a_live_process_and_matching_acknowledgement() {
        let (directory, request, mut child) = run_child("ready");
        let result = wait_for_acknowledgement(
            &mut child,
            directory.path(),
            &request,
            Duration::from_secs(3),
        );
        stop_child(&mut child).unwrap();
        result.unwrap();
        for mode in ["exit", "exit_after_ack", "forged", "timeout"] {
            let (directory, request, mut child) = run_child(mode);
            let result = wait_for_acknowledgement(
                &mut child,
                directory.path(),
                &request,
                Duration::from_millis(500),
            );
            stop_child(&mut child).unwrap();
            assert!(result.is_err(), "{mode}");
            assert!(child.try_wait().unwrap().is_some());
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_update_confirms_wrapped_appimage_children_and_rejects_exited_ui() {
        for mode in ["wrapper", "wrapper_exited_ui"] {
            let (directory, request, mut child) = run_child(mode);
            assert!(!startup_process_matches(
                child.id(),
                std::process::id(),
                &request.token
            ));
            let result = wait_for_acknowledgement(
                &mut child,
                directory.path(),
                &request,
                Duration::from_secs(3),
            );
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            child.wait().unwrap();
            assert_eq!(result.is_ok(), mode == "wrapper", "{mode}");
        }
    }
}
