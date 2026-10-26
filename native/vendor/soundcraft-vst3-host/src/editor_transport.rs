//! UI-thread transport intents, scoped to the webview that opened each native editor.
use std::cell::RefCell;
use std::collections::HashMap;

#[derive(Default)]
struct State {
    owner: String,
    windows: HashMap<usize, String>,
    revisions: HashMap<String, u64>,
    space_held: bool,
}

thread_local! { static STATE: RefCell<State> = RefCell::new(State::default()); }

pub(crate) fn set_owner(owner: &str) -> Result<(), String> {
    if owner.len() > 256 || owner.chars().any(char::is_control) {
        return Err("The plugin keyboard owner is invalid".into());
    }
    STATE.with(|state| state.borrow_mut().owner = owner.into());
    Ok(())
}

pub(crate) fn register(window: usize) {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        let owner = state.owner.clone();
        state.windows.insert(window, owner);
    });
}

pub(crate) fn unregister(window: usize) {
    STATE.with(|state| {
        state.borrow_mut().windows.remove(&window);
    });
}

pub(crate) fn request(window: usize) -> bool {
    STATE.with(|state| {
        let mut state = state.borrow_mut();
        let Some(owner) = state
            .windows
            .get(&window)
            .filter(|owner| !owner.is_empty())
            .cloned()
        else {
            return false;
        };
        let revision = state.revisions.entry(owner).or_default();
        *revision = revision.saturating_add(1);
        state.space_held = true;
        true
    })
}

pub(crate) fn held() -> bool {
    STATE.with(|state| state.borrow().space_held)
}
pub(crate) fn release() -> bool {
    STATE.with(|state| std::mem::take(&mut state.borrow_mut().space_held))
}
pub(crate) fn revision(owner: &str) -> u64 {
    STATE.with(|state| state.borrow().revisions.get(owner).copied().unwrap_or(0))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn counters_are_owned_by_each_window_and_survive_other_editors_closing() {
        set_owner("editor-a").unwrap();
        register(1);
        register(2);
        set_owner("editor-b").unwrap();
        register(3);
        assert!(request(1));
        assert!(request(2));
        assert!(request(3));
        assert_eq!(revision("editor-a"), 2);
        assert_eq!(revision("editor-b"), 1);
        assert!(release());
        assert!(!release());
        unregister(1);
        assert!(!request(1));
        assert_eq!(revision("editor-a"), 2);
        set_owner("editor-b").unwrap();
        register(2);
        assert!(request(2));
        assert_eq!(revision("editor-b"), 2);
        assert!(!request(999));
    }
}
