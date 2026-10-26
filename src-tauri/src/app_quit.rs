//! Route the macOS menu shortcut through each window's asynchronous close guards.

const QUIT_MENU_ID: &str = "pymss-guarded-quit";

fn is_quit_event(id: &str) -> bool {
    id == QUIT_MENU_ID
}

fn request_window_closes<W, E>(
    mut windows: Vec<(String, W)>,
    mut close: impl FnMut(W) -> Result<(), E>,
) -> Vec<(String, E)> {
    // Request child windows first. Closing a WebView is asynchronous: a guard
    // may still keep any window open, so this must never force application exit.
    windows.sort_by(|left, right| (left.0 == "main", &left.0).cmp(&(right.0 == "main", &right.0)));
    windows
        .into_iter()
        .filter_map(|(label, window)| close(window).err().map(|error| (label, error)))
        .collect()
}

#[cfg(any(target_os = "macos", test))]
pub fn menu(app: &tauri::AppHandle) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    use tauri::menu::{Menu, MenuItem, MenuItemKind, PredefinedMenuItem, Submenu};

    fn find_quit(
        submenu: &Submenu<tauri::Wry>,
        label: &str,
        matches: &mut Vec<(Submenu<tauri::Wry>, usize)>,
    ) -> tauri::Result<()> {
        for (index, item) in submenu.items()?.into_iter().enumerate() {
            match item {
                MenuItemKind::Predefined(item) if item.text()? == label => {
                    matches.push((submenu.clone(), index));
                }
                MenuItemKind::Submenu(child) => find_quit(&child, label, matches)?,
                _ => {}
            }
        }
        Ok(())
    }

    // Keep Tauri's standard Services, Edit, Window and Help menus. Predefined
    // Quit invokes NSApplication.terminate: directly and bypasses window guards.
    // Read its platform label rather than depending on menu order or English.
    let native_quit_label = PredefinedMenuItem::quit(app, None)?.text()?;
    let menu = Menu::default(app)?;
    let mut matches = Vec::new();
    for item in menu.items()? {
        if let MenuItemKind::Submenu(submenu) = item {
            find_quit(&submenu, &native_quit_label, &mut matches)?;
        }
    }
    if matches.len() != 1 {
        return Err(std::io::Error::other("Unable to replace the native Quit menu item").into());
    }
    let (submenu, index) = matches.pop().expect("one Quit menu item was checked");
    let quit = MenuItem::with_id(app, QUIT_MENU_ID, native_quit_label, true, Some("Cmd+Q"))?;
    submenu.remove_at(index)?;
    submenu.insert(&quit, index)?;
    Ok(menu)
}

#[cfg(any(target_os = "macos", test))]
pub fn on_menu_event(app: &tauri::AppHandle, event: tauri::menu::MenuEvent) {
    use tauri::Manager;

    if !is_quit_event(event.id().as_ref()) {
        return;
    }
    let failures = request_window_closes(app.webview_windows().into_iter().collect(), |window| {
        window.close()
    });
    for (label, error) in failures {
        crate::session_log::append(
            app,
            "WARN",
            "app.quit",
            vec![("windowLabel", label), ("detail", error.to_string())],
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quit_requests_each_child_before_the_main_window() {
        let windows = vec![
            ("main".into(), 0),
            ("workflow-node-editor-one".into(), 1),
            ("editor-project".into(), 2),
        ];
        let mut requested = Vec::new();
        let failures = request_window_closes(windows, |window| {
            requested.push(window);
            Ok::<_, ()>(())
        });
        assert_eq!(requested, vec![2, 1, 0]);
        assert!(failures.is_empty());
    }

    #[test]
    fn a_close_failure_does_not_skip_other_windows_or_discard_its_context() {
        let windows = vec![("main".into(), 0), ("editor-project".into(), 1)];
        let mut requested = Vec::new();
        let failures = request_window_closes(windows, |window| {
            requested.push(window);
            if window == 1 {
                Err("close request rejected")
            } else {
                Ok(())
            }
        });
        assert_eq!(requested, vec![1, 0]);
        assert_eq!(
            failures,
            vec![("editor-project".into(), "close request rejected")]
        );
    }

    #[test]
    fn no_windows_means_no_close_request_or_forced_exit() {
        let failures = request_window_closes(Vec::<(String, ())>::new(), |_| -> Result<(), ()> {
            panic!("there are no windows to close")
        });
        assert!(failures.is_empty());
    }

    #[test]
    fn only_the_guarded_menu_id_requests_quit() {
        assert!(is_quit_event("pymss-guarded-quit"));
        for id in ["", "quit", "close", "pymss-guarded-quit-other"] {
            assert!(!is_quit_event(id));
        }
    }

    #[test]
    fn menu_handlers_compile_against_tauri_without_creating_native_windows() {
        let _menu: fn(&tauri::AppHandle) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> = menu;
        let _event: fn(&tauri::AppHandle, tauri::menu::MenuEvent) = on_menu_event;
    }
}
