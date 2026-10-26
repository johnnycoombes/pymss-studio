//! Keep the JSON transport separate from plugin-visible stdout. Native plugins may write
//! diagnostics, even JSON-looking text, to either the OS handle or the C runtime descriptor.
//! This is initialized once in helper mode before loading any third-party code.
use serde_json::Value;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::sync::{Mutex, OnceLock, PoisonError};

struct Output {
    protocol: File,
    // On Windows SetStdHandle borrows this handle; it must outlive all plugin calls.
    _sink: File,
}

static OUTPUT: OnceLock<Mutex<Output>> = OnceLock::new();

pub(crate) fn initialize() -> io::Result<()> {
    if OUTPUT.get().is_some() {
        return Ok(());
    }
    let output = isolate_stdout()?;
    OUTPUT
        .set(Mutex::new(output))
        .map_err(|_| io::Error::other("Audio protocol output was already initialized"))
}

fn write_line(out: &mut impl Write, value: &Value) -> io::Result<()> {
    serde_json::to_writer(&mut *out, value)?;
    out.write_all(b"\n")?;
    out.flush()
}

pub(crate) fn write(value: &Value) -> io::Result<()> {
    if let Some(output) = OUTPUT.get() {
        let mut output = output.lock().unwrap_or_else(PoisonError::into_inner);
        write_line(&mut output.protocol, value)
    } else {
        // Unit tests may exercise the request dispatcher without entering helper mode.
        write_line(&mut io::stdout().lock(), value)
    }
}

#[cfg(unix)]
fn isolate_stdout() -> io::Result<Output> {
    use std::os::fd::{AsRawFd, FromRawFd};
    // SAFETY: duplicate an existing descriptor, preventing subprocesses from inheriting the
    // private protocol pipe. No plugin or background thread has been started at this point.
    let fd = unsafe { libc::fcntl(libc::STDOUT_FILENO, libc::F_DUPFD_CLOEXEC, 3) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: fcntl returned a new, owned descriptor.
    let protocol = unsafe { File::from_raw_fd(fd) };
    let sink = OpenOptions::new().write(true).open("/dev/null")?;
    // Use a null sink rather than stderr, which callers can intentionally merge into stdout.
    // Plugin stderr remains available for diagnostics without exposing the protocol handle.
    // SAFETY: both descriptors are live; dup2 atomically replaces fd 1, including C stdio.
    if unsafe { libc::dup2(sink.as_raw_fd(), libc::STDOUT_FILENO) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(Output {
        protocol,
        _sink: sink,
    })
}

#[cfg(windows)]
fn crt_error(operation: &str) -> io::Error {
    unsafe extern "C" {
        fn _get_errno(value: *mut i32) -> i32;
    }
    let mut errno = 0;
    // SAFETY: the CRT writes one integer into a valid local out pointer.
    unsafe {
        _get_errno(&mut errno);
    }
    io::Error::other(format!("{operation} (CRT errno {errno})"))
}

#[cfg(windows)]
fn isolate_stdout() -> io::Result<Output> {
    use std::os::windows::io::{AsRawHandle, BorrowedHandle, FromRawHandle, IntoRawHandle};
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::System::Console::{GetStdHandle, STD_OUTPUT_HANDLE, SetStdHandle};
    // SAFETY: these query/duplicate process-owned standard handles before plugin initialization.
    let handle = unsafe { GetStdHandle(STD_OUTPUT_HANDLE) };
    if handle.is_null() || handle == INVALID_HANDLE_VALUE {
        return Err(io::Error::new(
            io::ErrorKind::NotConnected,
            "Audio protocol output handle is unavailable",
        ));
    }
    // The duplicated OwnedHandle is non-inheritable and independent of CRT fd 1.
    // SAFETY: handle was checked above and remains live while it is duplicated.
    let protocol = File::from(unsafe { BorrowedHandle::borrow_raw(handle) }.try_clone_to_owned()?);
    let sink = OpenOptions::new().write(true).open("NUL")?;
    let raw = sink.try_clone()?.into_raw_handle();
    // SAFETY: transfer this cloned handle to the CRT. Binary/no-inherit flags keep the standard
    // descriptor usable without translating data or leaking it to decoder subprocesses.
    let fd = unsafe {
        libc::open_osfhandle(
            raw as isize,
            libc::O_WRONLY | libc::O_BINARY | libc::O_NOINHERIT,
        )
    };
    if fd < 0 {
        let error = crt_error("Cannot allocate the C runtime output descriptor");
        // SAFETY: ownership was not transferred when _open_osfhandle failed.
        drop(unsafe { File::from_raw_handle(raw) });
        return Err(error);
    }
    // SAFETY: fd owns a live sink handle. Replacing fd 1 closes only the original standard
    // descriptor; the private protocol duplicate remains open. Always release the temporary fd.
    let result = unsafe { libc::dup2(fd, 1) };
    let error = (result != 0).then(|| crt_error("Cannot redirect the C runtime standard output"));
    if fd != 1 {
        unsafe {
            libc::close(fd);
        }
    }
    if let Some(error) = error {
        return Err(error);
    }
    // CRT reassignment can also update the Windows standard-handle table, so set the borrowed
    // OS handle last. The sink is retained alongside the private protocol handle until exit.
    // SAFETY: sink is an owned live handle retained in Output.
    if unsafe { SetStdHandle(STD_OUTPUT_HANDLE, sink.as_raw_handle()) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(Output {
        protocol,
        _sink: sink,
    })
}

#[cfg(not(any(unix, windows)))]
fn isolate_stdout() -> io::Result<Output> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "Native audio protocol is unavailable on this platform",
    ))
}
