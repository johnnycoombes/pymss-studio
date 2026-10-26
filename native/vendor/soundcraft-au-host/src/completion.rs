//! A bounded asynchronous request has one owner, even when completion races its timeout.
use std::sync::Mutex;
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};

/// Keeps an AU alive while its asynchronous view request or late result still owns a callback.
pub(crate) struct PendingLease {
    pending: Arc<AtomicUsize>,
    released: AtomicBool,
}
impl PendingLease {
    pub(crate) fn new(pending: Arc<AtomicUsize>) -> Self {
        pending.fetch_add(1, Ordering::AcqRel);
        Self {
            pending,
            released: AtomicBool::new(false),
        }
    }
    pub(crate) fn release(&self) {
        if !self.released.swap(true, Ordering::AcqRel) {
            self.pending.fetch_sub(1, Ordering::AcqRel);
        }
    }
}
impl Drop for PendingLease {
    fn drop(&mut self) {
        self.release();
    }
}

enum State<T> {
    Pending,
    Ready(T),
    Cancelled,
}
pub(crate) struct Completion<T>(Mutex<State<T>>);
impl<T> Completion<T> {
    pub(crate) fn new() -> Self {
        Self(Mutex::new(State::Pending))
    }
    pub(crate) fn complete(&self, value: T) -> Result<(), T> {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if matches!(*state, State::Pending) {
            *state = State::Ready(value);
            Ok(())
        } else {
            Err(value)
        }
    }
    pub(crate) fn take(&self) -> Option<T> {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if matches!(*state, State::Ready(_)) {
            match std::mem::replace(&mut *state, State::Cancelled) {
                State::Ready(value) => Some(value),
                _ => None,
            }
        } else {
            None
        }
    }
    pub(crate) fn cancel(&self) -> Option<T> {
        let mut state = self
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        match std::mem::replace(&mut *state, State::Cancelled) {
            State::Ready(value) => Some(value),
            _ => None,
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn timeout_returns_late_resources_to_the_callback_for_cleanup() {
        let request = Completion::new();
        assert_eq!(request.cancel(), None);
        assert_eq!(request.complete(42), Err(42));
        assert_eq!(request.take(), None);
    }
    #[test]
    fn completion_wins_without_losing_an_owned_resource_at_the_deadline() {
        let request = Completion::new();
        assert_eq!(request.complete(42), Ok(()));
        assert_eq!(request.cancel(), Some(42));
        assert_eq!(request.cancel(), None);
        assert_eq!(request.complete(43), Err(43));
    }
    #[test]
    fn concurrent_timeout_has_exactly_one_resource_owner() {
        for _ in 0..100 {
            let request = Arc::new(Completion::new());
            let callback = Arc::clone(&request);
            let worker = std::thread::spawn(move || callback.complete(42).err());
            let waiter = request.cancel();
            let late = worker.join().unwrap();
            assert_eq!(waiter.into_iter().chain(late).collect::<Vec<_>>(), vec![42]);
        }
    }
    #[test]
    fn cancelled_view_retains_its_unit_until_the_last_callback_owner_releases() {
        let pending = Arc::new(AtomicUsize::new(0));
        let request = Arc::new(PendingLease::new(Arc::clone(&pending)));
        let callback = Arc::clone(&request);
        drop(request);
        assert_eq!(pending.load(Ordering::Acquire), 1);
        let late_ui_cleanup = Arc::clone(&callback);
        drop(callback);
        assert_eq!(pending.load(Ordering::Acquire), 1);
        late_ui_cleanup.release();
        late_ui_cleanup.release();
        assert_eq!(pending.load(Ordering::Acquire), 0);
    }
    #[test]
    fn rejected_callback_block_releases_a_request_without_counter_underflow() {
        let pending = Arc::new(AtomicUsize::new(0));
        let request = PendingLease::new(Arc::clone(&pending));
        request.release();
        drop(request);
        assert_eq!(pending.load(Ordering::Acquire), 0);
    }
}
