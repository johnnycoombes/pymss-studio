//! Read a helper's natural exit status without reaping it or releasing its process identity.
use std::io;
use std::process::Child;
use std::time::{Duration, Instant};

pub(super) fn observe(child: &Child, timeout: Duration) -> io::Result<Option<String>> {
    let started = Instant::now();
    loop {
        if let Some(status) = peek(child)? {
            return Ok(Some(status));
        }
        let remaining = timeout.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            return Ok(None);
        }
        std::thread::sleep(remaining.min(Duration::from_millis(5)));
    }
}

#[cfg(unix)]
fn peek(child: &Child) -> io::Result<Option<String>> {
    // SAFETY: writable siginfo for this owned child. WNOWAIT leaves the child waitable and
    // keeps its PID reserved until ProcessControl signals descendants and reaps the leader.
    let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
    let result = unsafe {
        libc::waitid(
            libc::P_PID,
            child.id() as libc::id_t,
            &mut info,
            libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
        )
    };
    if result != 0 {
        let error = io::Error::last_os_error();
        return if error.kind() == io::ErrorKind::Interrupted {
            Ok(None)
        } else {
            Err(error)
        };
    }
    // SAFETY: waitid initialized the child-status fields; zero means no status was ready.
    if unsafe { info.si_pid() } == 0 {
        return Ok(None);
    }
    let status = unsafe { info.si_status() };
    Ok(match info.si_code {
        libc::CLD_EXITED => Some(format!("exit status: {status}")),
        libc::CLD_KILLED => Some(format!("signal: {status}")),
        libc::CLD_DUMPED => Some(format!("signal: {status} (core dumped)")),
        _ => None,
    })
}

#[cfg(windows)]
fn peek(child: &Child) -> io::Result<Option<String>> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::{WAIT_FAILED, WAIT_OBJECT_0, WAIT_TIMEOUT};
    use windows_sys::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject};
    let handle = child.as_raw_handle().cast();
    // SAFETY: Child owns the live process handle. A zero timeout never blocks or closes it.
    match unsafe { WaitForSingleObject(handle, 0) } {
        WAIT_OBJECT_0 => {
            let mut code = 0;
            if unsafe { GetExitCodeProcess(handle, &mut code) } == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(Some(format!("exit status: {code} (0x{code:08X})")))
        }
        WAIT_TIMEOUT => Ok(None),
        WAIT_FAILED => Err(io::Error::last_os_error()),
        _ => Err(io::Error::other("Unexpected process wait result")),
    }
}

#[cfg(not(any(unix, windows)))]
fn peek(_child: &Child) -> io::Result<Option<String>> {
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, Read, Write};
    use std::process::{Command, Stdio};

    #[test]
    #[ignore = "owned subprocess fixture; launched by the exit-observation tests"]
    fn owned_eof_fixture() {
        if std::env::var_os("PYMSS_OWNED_EXIT_FIXTURE").as_deref()
            != Some(std::ffi::OsStr::new("1"))
        {
            return;
        }
        // Close only this fixture's stdout, then hold its exit behind the parent's stdin gate.
        // A failing parent also closes stdin, so the fixture cannot remain stuck after failure.
        #[cfg(unix)]
        unsafe {
            libc::close(libc::STDOUT_FILENO);
        }
        #[cfg(windows)]
        unsafe {
            unsafe extern "C" {
                fn _close(fd: i32) -> i32;
            }
            _close(1);
        }
        let mut line = String::new();
        let _ = io::stdin().lock().read_line(&mut line);
        std::process::exit(23);
    }

    fn fixture() -> Child {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--ignored",
                "--exact",
                "commands::plugin_host::process_exit::tests::owned_eof_fixture",
                "--nocapture",
            ])
            .env("PYMSS_OWNED_EXIT_FIXTURE", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command.spawn().unwrap();
        let mut output = child.stdout.take().unwrap();
        let (sender, ready) = std::sync::mpsc::channel();
        let reader = std::thread::spawn(move || {
            let _ = sender.send(output.read_to_end(&mut Vec::new()));
        });
        if !matches!(ready.recv_timeout(Duration::from_secs(5)), Ok(Ok(_))) {
            let _ = child.kill();
            let _ = child.wait();
            reader.join().unwrap();
            panic!("The owned fixture did not close its stdout");
        }
        reader.join().unwrap();
        child
    }

    #[test]
    fn eof_before_exit_retains_the_natural_status_until_the_owner_reaps() {
        let mut child = fixture();
        assert_eq!(observe(&child, Duration::ZERO).unwrap(), None);
        let mut gate = child.stdin.take().unwrap();
        let release = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(50));
            let _ = gate.write_all(b"exit\n");
        });
        let status = observe(&child, Duration::from_secs(2)).unwrap().unwrap();
        release.join().unwrap();
        assert!(status.starts_with("exit status: 23"));
        assert!(
            observe(&child, Duration::ZERO).unwrap().is_some(),
            "observation must not reap the child"
        );
        assert_eq!(child.wait().unwrap().code(), Some(23));
    }

    #[test]
    fn eof_from_a_live_child_has_a_bounded_observation_window() {
        let mut child = fixture();
        let observed = observe(&child, Duration::from_millis(10)).unwrap();
        assert_eq!(observed, None);
        // The stdin gate is still held: return promptly rather than waiting for a natural exit.
        child.kill().unwrap();
        child.wait().unwrap();
    }
}
