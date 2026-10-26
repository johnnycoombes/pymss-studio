//! Focused child-window key messages use the same container for VST2 and VST3.
#[cfg(windows)]
#[allow(unsafe_code)]
fn verify() {
    use soundcraft_vst3_host::{
        NativeEditorWindow, editor_transport_revision, pump_ui, set_editor_transport_owner,
    };
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        GetFocus, GetKeyboardState, SetActiveWindow, SetFocus, SetKeyboardState, VK_CONTROL,
        VK_SPACE,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, GetWindowTextW, PostMessageW, SW_SHOWNOACTIVATE, SWP_NOACTIVATE,
        SWP_NOSIZE, SWP_NOZORDER, SetWindowPos, ShowWindow, WM_KEYDOWN, WM_KEYUP, WS_CHILD,
        WS_VISIBLE,
    };
    fn wide(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(Some(0)).collect()
    }
    set_editor_transport_owner("editor-a").unwrap();
    let window = NativeEditorWindow::new("Keyboard fixture", 320.0, 240.0).unwrap();
    unsafe {
        SetWindowPos(
            window.raw(),
            std::ptr::null_mut(),
            -30000,
            -30000,
            0,
            0,
            SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOZORDER,
        );
        ShowWindow(window.raw(), SW_SHOWNOACTIVATE);
        SetActiveWindow(window.raw());
        let canvas = CreateWindowExW(
            0,
            wide("STATIC").as_ptr(),
            wide("Canvas").as_ptr(),
            WS_CHILD | WS_VISIBLE,
            0,
            0,
            100,
            40,
            window.parent(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null(),
        );
        assert!(!canvas.is_null());
        SetFocus(canvas);
        assert_eq!(GetFocus(), canvas);
        PostMessageW(canvas, WM_KEYDOWN, VK_SPACE as usize, 1);
        PostMessageW(canvas, WM_KEYDOWN, VK_SPACE as usize, 1 | (1 << 30));
        PostMessageW(
            canvas,
            WM_KEYUP,
            VK_SPACE as usize,
            1 | (1 << 30) | (1 << 31),
        );
        pump_ui();
        assert_eq!(editor_transport_revision("editor-a"), 1);
        assert_eq!(editor_transport_revision("editor-b"), 0);
        let edit = CreateWindowExW(
            0,
            wide("EDIT").as_ptr(),
            wide("").as_ptr(),
            WS_CHILD | WS_VISIBLE,
            0,
            50,
            120,
            30,
            window.parent(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null(),
        );
        assert!(!edit.is_null());
        SetFocus(edit);
        PostMessageW(edit, WM_KEYDOWN, VK_SPACE as usize, 1);
        PostMessageW(edit, WM_KEYUP, VK_SPACE as usize, 1 | (1 << 30) | (1 << 31));
        pump_ui();
        assert_eq!(editor_transport_revision("editor-a"), 1);
        let mut text = [0u16; 8];
        assert_eq!(GetWindowTextW(edit, text.as_mut_ptr(), 8), 1);
        assert_eq!(text[0], ' ' as u16);
        SetFocus(canvas);
        let mut original = [0u8; 256];
        assert_ne!(GetKeyboardState(original.as_mut_ptr()), 0);
        let mut modified = original;
        modified[VK_CONTROL as usize] = 0x80;
        assert_ne!(SetKeyboardState(modified.as_ptr()), 0);
        PostMessageW(canvas, WM_KEYDOWN, VK_SPACE as usize, 1);
        PostMessageW(
            canvas,
            WM_KEYUP,
            VK_SPACE as usize,
            1 | (1 << 30) | (1 << 31),
        );
        pump_ui();
        SetKeyboardState(original.as_ptr());
        assert_eq!(editor_transport_revision("editor-a"), 1);
        set_editor_transport_owner("editor-b").unwrap();
        window.update_owner().unwrap();
        PostMessageW(canvas, WM_KEYDOWN, VK_SPACE as usize, 1);
        PostMessageW(
            canvas,
            WM_KEYUP,
            VK_SPACE as usize,
            1 | (1 << 30) | (1 << 31),
        );
        pump_ui();
        assert_eq!(editor_transport_revision("editor-a"), 1);
        assert_eq!(editor_transport_revision("editor-b"), 1);
    }
    window.close();
    pump_ui();
}

fn main() {
    #[cfg(windows)]
    verify();
}
