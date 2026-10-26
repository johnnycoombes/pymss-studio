//! Windows plugin dialogs can hold the helper UI thread inside a modal message loop.
//! Only the live session retains such a request; scanners and temporary exporters stay bounded.
use std::time::{Duration, Instant};

pub(super) const PENDING_PREFIX: &str = "PLUGIN_HOST_DIALOG_PENDING:";

pub(super) struct Pending {
    pub id: u64,
    pub operation: String,
    timeout: Duration,
    deadline: Option<Instant>,
}

impl Pending {
    pub fn new(id: u64, operation: String, started: Instant, timeout: Duration) -> Self {
        Self {
            id,
            operation,
            timeout,
            deadline: Some(started + timeout),
        }
    }

    pub fn suspend(&mut self) {
        self.deadline = None;
    }

    pub fn is_suspended(&self) -> bool {
        self.deadline.is_none()
    }

    pub fn remaining(&mut self) -> Duration {
        // Once the user has dismissed the dialog, SDK completion gets the same bounded budget
        // as the original request. Further retries cannot reset this deadline without a dialog.
        let now = Instant::now();
        self.deadline
            .get_or_insert(now + self.timeout)
            .saturating_duration_since(now)
    }
}

pub(super) fn message(title: &str) -> String {
    let dialog = if title.is_empty() {
        "a plugin dialog".into()
    } else {
        format!("the plugin dialog \"{title}\"")
    };
    format!(
        "{PENDING_PREFIX} Complete or cancel {dialog}, then retry. The audio host remains open."
    )
}

#[cfg(not(windows))]
pub(super) fn owned_dialog(_pid: u32) -> Option<String> {
    None
}

#[cfg(windows)]
pub(super) fn owned_dialog(pid: u32) -> Option<String> {
    use windows_sys::Win32::Foundation::{HWND, LPARAM};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetAncestor, GetClassNameW, GetWindowTextW, GetWindowThreadProcessId,
        IsIconic, IsWindowVisible, GA_ROOTOWNER,
    };

    struct Search {
        pid: u32,
        title: Option<String>,
    }
    unsafe extern "system" fn visit(window: HWND, data: LPARAM) -> i32 {
        // SAFETY: EnumWindows invokes this callback synchronously with our live stack context.
        let search = unsafe { &mut *(data as *mut Search) };
        let mut pid = 0;
        unsafe {
            GetWindowThreadProcessId(window, &mut pid);
        }
        if pid != search.pid {
            return 1;
        }
        // Windows hides the complete owned-popup chain while the app editor is minimized.
        // A hidden standard modal can still block the helper UI thread until restoration.
        if unsafe {
            IsWindowVisible(window) == 0 && IsIconic(GetAncestor(window, GA_ROOTOWNER)) == 0
        } {
            return 1;
        }
        let mut class = [0u16; 64];
        let count = unsafe { GetClassNameW(window, class.as_mut_ptr(), class.len() as i32) };
        if count <= 0 || String::from_utf16_lossy(&class[..count as usize]) != "#32770" {
            return 1;
        }
        let mut title = [0u16; 512];
        let count = unsafe { GetWindowTextW(window, title.as_mut_ptr(), title.len() as i32) }.max(0)
            as usize;
        search.title = Some(String::from_utf16_lossy(&title[..count]));
        0
    }
    let mut search = Search { pid, title: None };
    // The caller still owns this unreaped child, so its PID cannot have been reused.
    unsafe {
        EnumWindows(Some(visit), (&mut search as *mut Search) as LPARAM);
    }
    search.title
}
