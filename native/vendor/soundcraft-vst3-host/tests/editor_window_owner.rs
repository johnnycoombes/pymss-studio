//! Real, hidden Win32 containers exercise ownership without opening a third-party plugin.
#[cfg(windows)]
#[allow(unsafe_code)]
fn assert_owner(window: &soundcraft_vst3_host::NativeEditorWindow, owner: usize) {
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GW_OWNER, GWL_EXSTYLE, GetWindow, GetWindowLongPtrW, WS_EX_TOPMOST,
    };
    // SAFETY: the test retains each live hidden window while querying its attributes.
    unsafe {
        assert_eq!(GetWindow(window.raw(), GW_OWNER) as usize, owner);
        assert_eq!(
            GetWindowLongPtrW(window.raw(), GWL_EXSTYLE) as u32 & WS_EX_TOPMOST,
            0
        );
    }
}

#[cfg(windows)]
#[allow(unsafe_code)]
fn verify_minimization(app: &soundcraft_vst3_host::NativeEditorWindow) {
    use soundcraft_vst3_host::{NativeEditorWindow, pump_ui};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        IsIconic, IsWindowVisible, SW_HIDE, SW_SHOWMINNOACTIVE, SW_SHOWNOACTIVATE, SWP_NOACTIVATE,
        SWP_NOSIZE, SWP_NOZORDER, SendMessageW, SetWindowPos, ShowWindow, WM_CLOSE,
    };
    let popup = NativeEditorWindow::new("Minimize insert fixture", 320.0, 240.0).expect("insert");
    // SAFETY: these are live windows owned by this test. Keep them offscreen and never
    // activate them, so the regression does not disturb the user's desktop or focus.
    unsafe {
        for hwnd in [app.raw(), popup.raw()] {
            assert_ne!(
                SetWindowPos(
                    hwnd,
                    std::ptr::null_mut(),
                    -30000,
                    -30000,
                    0,
                    0,
                    SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOZORDER
                ),
                0
            );
            ShowWindow(hwnd, SW_SHOWNOACTIVATE);
        }
        pump_ui();
        assert!(popup.is_visible());
        ShowWindow(app.raw(), SW_SHOWMINNOACTIVE);
        pump_ui();
        assert_ne!(IsIconic(app.raw()), 0);
        assert_eq!(
            IsWindowVisible(popup.raw()),
            0,
            "Windows hides owned popups on minimize"
        );
        assert!(
            popup.is_visible(),
            "temporary hiding must not tear down the plugin view"
        );
        ShowWindow(app.raw(), SW_SHOWNOACTIVATE);
        pump_ui();
        assert!(popup.is_visible());
        assert_ne!(IsWindowVisible(popup.raw()), 0);
        SendMessageW(popup.raw(), WM_CLOSE, 0, 0);
        assert!(
            !popup.is_visible(),
            "the close button must still close the view"
        );
        ShowWindow(app.raw(), SW_HIDE);
    }
    popup.close();
}

#[cfg(windows)]
fn main() {
    use soundcraft_vst3_host::{
        NativeEditorWindow, initialize_ui, pump_ui, set_editor_owner, shutdown_ui,
    };
    use std::process::Command;
    use std::time::{Duration, Instant};
    initialize_ui().expect("initialize UI");
    if let Some(owner) = std::env::args().nth(1) {
        let owner: usize = owner.parse().expect("owner HWND");
        set_editor_owner(owner).expect("foreign app owner");
        let first =
            NativeEditorWindow::new("Cross-process insert 1", 320.0, 240.0).expect("first insert");
        let second =
            NativeEditorWindow::new("Cross-process insert 2", 320.0, 240.0).expect("second insert");
        assert_owner(&first, owner);
        assert_owner(&second, owner);
        first.close();
        second.close();
        set_editor_owner(0).expect("clear owner");
        shutdown_ui();
        return;
    }
    set_editor_owner(0).expect("standalone owner");
    let app = NativeEditorWindow::new("Editor owner fixture", 640.0, 480.0).expect("app window");
    let owner = app.raw() as usize;
    assert_owner(&app, 0);
    set_editor_owner(owner).expect("app owner");
    let first = NativeEditorWindow::new("Insert 1", 320.0, 240.0).expect("first insert");
    let second = NativeEditorWindow::new("Insert 2", 320.0, 240.0).expect("second insert");
    assert_owner(&first, owner);
    assert_owner(&second, owner);
    set_editor_owner(0).expect("standalone second app");
    let other_app =
        NativeEditorWindow::new("Other editor fixture", 640.0, 480.0).expect("second app");
    set_editor_owner(other_app.raw() as usize).expect("second app owner");
    second.update_owner().expect("reassign existing insert");
    assert_owner(&first, owner);
    assert_owner(&second, other_app.raw() as usize);
    set_editor_owner(owner).expect("restore app owner");
    second.update_owner().expect("restore insert owner");
    other_app.close();
    first.close();
    assert_owner(&second, owner);
    second.close();
    let reopened = NativeEditorWindow::new("Reopened insert", 320.0, 240.0).expect("reopen insert");
    assert_owner(&reopened, owner);
    reopened.close();
    verify_minimization(&app);

    // The application and isolated audio helper own windows in different processes.
    let mut child = Command::new(std::env::current_exe().expect("test executable"))
        .arg(owner.to_string())
        .spawn()
        .expect("isolated window helper");
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        pump_ui();
        if let Some(status) = child.try_wait().expect("helper status") {
            assert!(status.success(), "cross-process ownership failed: {status}");
            break;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("cross-process ownership timed out");
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    set_editor_owner(0).expect("clear owner");
    app.close();
    assert!(
        set_editor_owner(owner).is_err(),
        "a closed app cannot own new plugin windows"
    );
    shutdown_ui();
    println!("editor_window_owner: same-process, cross-process and reopen checks passed");
}

#[cfg(not(windows))]
fn main() {
    println!("editor_window_owner: Windows-only check");
}
