//! Linux's VST3 run loop. Each exposed context owns its registrations; snapshots contain
//! retained entries so no host lock is held while a plug-in callback runs.

use sl::{IEventHandlerTrait, ITimerHandlerTrait};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock, PoisonError, Weak};
use std::time::{Duration, Instant};
use vst3::Steinberg::{self as sb, Linux as sl};
use vst3::{ComPtr, ComRef};

const MAX_REGISTRATIONS: usize = 4096;
const MAX_OWNER_REGISTRATIONS: usize = 256;
const MAX_CALLBACKS: usize = 256;
static REGISTRATIONS: AtomicUsize = AtomicUsize::new(0);
static NEXT_ID: AtomicU64 = AtomicU64::new(1);

enum Callback {
    Event {
        handler: ComPtr<sl::IEventHandler>,
        fd: i32,
    },
    Timer {
        handler: ComPtr<sl::ITimerHandler>,
        interval: Duration,
        next: Mutex<Instant>,
    },
}

struct Entry {
    id: u64,
    callback: Callback,
}

impl Drop for Entry {
    fn drop(&mut self) {
        REGISTRATIONS.fetch_sub(1, Ordering::Relaxed);
    }
}

#[derive(Default)]
struct State {
    entries: Vec<Arc<Entry>>,
    closed: bool,
}

fn registry() -> &'static Mutex<Vec<Weak<Mutex<State>>>> {
    static REGISTRY: OnceLock<Mutex<Vec<Weak<Mutex<State>>>>> = OnceLock::new();
    REGISTRY.get_or_init(Mutex::default)
}

pub(super) struct Owner {
    state: Arc<Mutex<State>>,
}

impl Default for Owner {
    fn default() -> Self {
        let state = Arc::new(Mutex::new(State::default()));
        let mut registry = registry().lock().unwrap_or_else(PoisonError::into_inner);
        registry.retain(|owner| owner.strong_count() != 0);
        registry.push(Arc::downgrade(&state));
        Self { state }
    }
}

impl Owner {
    fn add(&self, callback: Callback) -> sb::tresult {
        // Keep COM releases (including rejected registrations) outside the state lock.
        let result = {
            let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
            if state.closed {
                sb::kResultFalse
            } else if state.entries.len() >= MAX_OWNER_REGISTRATIONS {
                sb::kOutOfMemory
            } else if REGISTRATIONS
                .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |n| {
                    (n < MAX_REGISTRATIONS).then_some(n + 1)
                })
                .is_err()
            {
                sb::kOutOfMemory
            } else {
                state.entries.push(Arc::new(Entry {
                    id: NEXT_ID.fetch_add(1, Ordering::Relaxed),
                    callback,
                }));
                return sb::kResultOk;
            }
        };
        drop(callback);
        result
    }

    pub(super) unsafe fn register_event(
        &self,
        handler: *mut sl::IEventHandler,
        fd: i32,
    ) -> sb::tresult {
        if fd < 0 {
            return sb::kInvalidArgument;
        }
        // SAFETY: the caller supplies an SDK interface valid for this call. The new reference
        // keeps it alive until unregister/owner teardown (or a currently executing snapshot).
        let Some(handler) = (unsafe { ComRef::from_raw(handler) }) else {
            return sb::kInvalidArgument;
        };
        let handler = handler.to_com_ptr();
        {
            let state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
            if state.entries.iter().any(|entry| matches!(&entry.callback, Callback::Event { handler: existing, fd: existing_fd } if existing.as_ptr() == handler.as_ptr() && *existing_fd == fd)) {
                return sb::kResultFalse;
            }
        }
        self.add(Callback::Event { handler, fd })
    }

    pub(super) unsafe fn register_timer(
        &self,
        handler: *mut sl::ITimerHandler,
        milliseconds: u64,
    ) -> sb::tresult {
        // Zero timers would create an unbounded busy loop; reject overlarge intervals rather
        // than wrapping Instant arithmetic supplied by an untrusted plug-in.
        if milliseconds == 0 || milliseconds > 86_400_000 {
            return sb::kInvalidArgument;
        }
        // SAFETY: as in register_event.
        let Some(handler) = (unsafe { ComRef::from_raw(handler) }) else {
            return sb::kInvalidArgument;
        };
        let handler = handler.to_com_ptr();
        {
            let state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
            if state.entries.iter().any(|entry| matches!(&entry.callback, Callback::Timer { handler: existing, .. } if existing.as_ptr() == handler.as_ptr())) {
                return sb::kResultFalse;
            }
        }
        let interval = Duration::from_millis(milliseconds);
        self.add(Callback::Timer {
            handler,
            interval,
            next: Mutex::new(Instant::now() + interval),
        })
    }

    fn remove(&self, matches: impl Fn(&Callback) -> bool) -> sb::tresult {
        let removed = {
            let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
            let mut removed = Vec::new();
            let mut i = 0;
            while i < state.entries.len() {
                if matches(&state.entries[i].callback) {
                    removed.push(state.entries.remove(i));
                } else {
                    i += 1;
                }
            }
            removed
        };
        if removed.is_empty() {
            sb::kResultFalse
        } else {
            drop(removed);
            sb::kResultOk
        }
    }

    pub(super) fn unregister_event(&self, handler: *mut sl::IEventHandler) -> sb::tresult {
        if handler.is_null() {
            return sb::kInvalidArgument;
        }
        self.remove(|callback| matches!(callback, Callback::Event { handler: existing, .. } if existing.as_ptr() == handler))
    }

    pub(super) fn unregister_timer(&self, handler: *mut sl::ITimerHandler) -> sb::tresult {
        if handler.is_null() {
            return sb::kInvalidArgument;
        }
        self.remove(|callback| matches!(callback, Callback::Timer { handler: existing, .. } if existing.as_ptr() == handler))
    }

    pub(super) fn clear(&self) {
        clear(&self.state);
    }
}

fn clear(owner: &Mutex<State>) {
    let entries = {
        let mut state = owner.lock().unwrap_or_else(PoisonError::into_inner);
        state.closed = true;
        std::mem::take(&mut state.entries)
    };
    drop(entries);
}

impl Drop for Owner {
    fn drop(&mut self) {
        self.clear();
    }
}

#[cfg(target_os = "linux")]
fn owners() -> Vec<Arc<Mutex<State>>> {
    let mut registry = registry().lock().unwrap_or_else(PoisonError::into_inner);
    let owners: Vec<_> = registry.iter().filter_map(Weak::upgrade).collect();
    registry.retain(|owner| owner.strong_count() != 0);
    owners
}

#[cfg(target_os = "linux")]
pub(super) fn shutdown() {
    for owner in owners() {
        clear(&owner);
    }
}

struct Pending {
    owner: Arc<Mutex<State>>,
    entry: Arc<Entry>,
}

fn snapshot(owners: &[Arc<Mutex<State>>]) -> Vec<Pending> {
    let mut pending = Vec::new();
    for owner in owners {
        let state = owner.lock().unwrap_or_else(PoisonError::into_inner);
        for entry in &state.entries {
            pending.push(Pending {
                owner: owner.clone(),
                entry: entry.clone(),
            });
        }
    }
    pending
}

fn dispatch(pending: &[Pending], now: Instant, ready: impl Fn(i32) -> bool, start: usize) -> usize {
    if pending.is_empty() {
        return 0;
    }
    let start = start % pending.len();
    let mut called = 0;
    for offset in 0..pending.len() {
        if called >= MAX_CALLBACKS {
            break;
        }
        let item = &pending[(start + offset) % pending.len()];
        let active = {
            let owner = item.owner.lock().unwrap_or_else(PoisonError::into_inner);
            !owner.closed && owner.entries.iter().any(|entry| entry.id == item.entry.id)
        };
        if !active {
            continue;
        }
        match &item.entry.callback {
            Callback::Event { handler, fd } if ready(*fd) => {
                // SAFETY: the entry retains the handler; callbacks run on the UI thread and
                // outside all locks, permitting unregister/re-register from the callback.
                unsafe { handler.onFDIsSet(*fd) };
                called += 1;
            }
            Callback::Timer {
                handler,
                interval,
                next,
            } => {
                let due = {
                    let mut next = next.lock().unwrap_or_else(PoisonError::into_inner);
                    if now < *next {
                        false
                    } else {
                        // Coalesce missed deadlines: never burst callbacks after a slow plug-in.
                        *next = now + *interval;
                        true
                    }
                };
                if due {
                    // SAFETY: same lifetime/thread guarantee as event callbacks.
                    unsafe { handler.onTimer() };
                    called += 1;
                }
            }
            _ => {}
        }
    }
    called
}

#[cfg(target_os = "linux")]
pub(super) fn pump() {
    use std::cell::Cell;
    thread_local! { static PUMPING: Cell<bool> = const { Cell::new(false) }; }
    if !super::window::on_main_thread() || PUMPING.with(|pumping| pumping.replace(true)) {
        return;
    }
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            PUMPING.with(|pumping| pumping.set(false));
        }
    }
    let _reset = Reset;
    let pending = snapshot(&owners());
    let mut fds: Vec<libc::pollfd> = pending
        .iter()
        .filter_map(|item| match item.entry.callback {
            Callback::Event { fd, .. } => Some(libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            }),
            _ => None,
        })
        .collect();
    // SAFETY: `fds` is a writable contiguous array; timeout zero never blocks the UI thread.
    let result = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, 0) };
    let ready: std::collections::HashSet<i32> = if result > 0 {
        fds.iter()
            .filter_map(|item| {
                (item.revents & (libc::POLLIN | libc::POLLHUP) != 0).then_some(item.fd)
            })
            .collect()
    } else {
        std::collections::HashSet::new()
    };
    static CURSOR: AtomicUsize = AtomicUsize::new(0);
    let start = CURSOR.fetch_add(MAX_CALLBACKS, Ordering::Relaxed);
    dispatch(&pending, Instant::now(), |fd| ready.contains(&fd), start);
}

#[cfg(target_os = "linux")]
macro_rules! impl_run_loop {
    ($host:ty) => {
        impl vst3::Steinberg::Linux::IRunLoopTrait for $host {
            unsafe fn registerEventHandler(
                &self,
                handler: *mut vst3::Steinberg::Linux::IEventHandler,
                fd: i32,
            ) -> vst3::Steinberg::tresult {
                if !window::on_main_thread() {
                    return sb::kResultFalse;
                }
                unsafe { self.run_loop.register_event(handler, fd) }
            }
            unsafe fn unregisterEventHandler(
                &self,
                handler: *mut vst3::Steinberg::Linux::IEventHandler,
            ) -> vst3::Steinberg::tresult {
                if !window::on_main_thread() {
                    return sb::kResultFalse;
                }
                self.run_loop.unregister_event(handler)
            }
            unsafe fn registerTimer(
                &self,
                handler: *mut vst3::Steinberg::Linux::ITimerHandler,
                milliseconds: u64,
            ) -> vst3::Steinberg::tresult {
                if !window::on_main_thread() {
                    return sb::kResultFalse;
                }
                unsafe { self.run_loop.register_timer(handler, milliseconds) }
            }
            unsafe fn unregisterTimer(
                &self,
                handler: *mut vst3::Steinberg::Linux::ITimerHandler,
            ) -> vst3::Steinberg::tresult {
                if !window::on_main_thread() {
                    return sb::kResultFalse;
                }
                self.run_loop.unregister_timer(handler)
            }
        }
    };
}
#[cfg(target_os = "linux")]
pub(super) use impl_run_loop;

#[cfg(test)]
mod tests {
    use super::*;
    use vst3::{Class, ComWrapper};
    struct Timer {
        hits: Arc<AtomicUsize>,
    }
    impl Class for Timer {
        type Interfaces = (sl::ITimerHandler,);
    }
    impl sl::ITimerHandlerTrait for Timer {
        unsafe fn onTimer(&self) {
            self.hits.fetch_add(1, Ordering::Relaxed);
        }
    }
    struct Event {
        owner: Arc<Owner>,
        hits: Arc<AtomicUsize>,
    }
    impl Class for Event {
        type Interfaces = (sl::IEventHandler,);
    }
    impl sl::IEventHandlerTrait for Event {
        unsafe fn onFDIsSet(&self, _: i32) {
            self.hits.fetch_add(1, Ordering::Relaxed);
            self.owner.clear();
        }
    }

    struct Counter {
        hits: Arc<AtomicUsize>,
    }
    impl Class for Counter {
        type Interfaces = (sl::IEventHandler,);
    }
    impl sl::IEventHandlerTrait for Counter {
        unsafe fn onFDIsSet(&self, _: i32) {
            self.hits.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[test]
    fn timers_coalesce_missed_deadlines_and_unregister_cancels_snapshots() {
        let owner = Owner::default();
        let hits = Arc::new(AtomicUsize::new(0));
        let timer = ComWrapper::new(Timer { hits: hits.clone() });
        let handler = timer.as_com_ref::<sl::ITimerHandler>().unwrap();
        assert_eq!(
            unsafe { owner.register_timer(handler.as_ptr(), 0) },
            sb::kInvalidArgument
        );
        assert_eq!(
            unsafe { owner.register_timer(handler.as_ptr(), 10) },
            sb::kResultOk
        );
        assert_eq!(
            unsafe { owner.register_timer(handler.as_ptr(), 10) },
            sb::kResultFalse
        );
        let pending = snapshot(&[owner.state.clone()]);
        let later = Instant::now() + Duration::from_secs(60);
        assert_eq!(dispatch(&pending, later, |_| false, 0), 1);
        assert_eq!(dispatch(&pending, later, |_| false, 0), 0);
        assert_eq!(owner.unregister_timer(handler.as_ptr()), sb::kResultOk);
        assert_eq!(
            dispatch(&pending, later + Duration::from_secs(60), |_| false, 0),
            0
        );
        assert_eq!(hits.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn callbacks_can_clear_their_owner_and_cancel_remaining_callbacks() {
        let owner = Arc::new(Owner::default());
        let hits = Arc::new(AtomicUsize::new(0));
        let event = ComWrapper::new(Event {
            owner: owner.clone(),
            hits: hits.clone(),
        });
        let handler = event.as_com_ref::<sl::IEventHandler>().unwrap();
        assert_eq!(
            unsafe { owner.register_event(handler.as_ptr(), -1) },
            sb::kInvalidArgument
        );
        assert_eq!(
            unsafe { owner.register_event(handler.as_ptr(), 4) },
            sb::kResultOk
        );
        assert_eq!(
            unsafe { owner.register_event(handler.as_ptr(), 5) },
            sb::kResultOk
        );
        let pending = snapshot(&[owner.state.clone()]);
        assert_eq!(dispatch(&pending, Instant::now(), |_| true, 0), 1);
        assert_eq!(hits.load(Ordering::Relaxed), 1);
        assert_eq!(
            unsafe { owner.register_event(handler.as_ptr(), 6) },
            sb::kResultFalse
        );
    }

    #[test]
    fn registrations_are_bounded_and_unregister_removes_all_handler_descriptors() {
        let owner = Owner::default();
        let hits = Arc::new(AtomicUsize::new(0));
        let event = ComWrapper::new(Counter { hits: hits.clone() });
        let handler = event.as_com_ref::<sl::IEventHandler>().unwrap();
        for fd in 0..MAX_OWNER_REGISTRATIONS as i32 {
            assert_eq!(
                unsafe { owner.register_event(handler.as_ptr(), fd) },
                sb::kResultOk
            );
        }
        assert_eq!(
            unsafe { owner.register_event(handler.as_ptr(), 0) },
            sb::kResultFalse
        );
        assert_eq!(
            unsafe { owner.register_event(handler.as_ptr(), 999) },
            sb::kOutOfMemory
        );
        let pending = snapshot(&[owner.state.clone()]);
        assert_eq!(
            dispatch(&pending, Instant::now(), |_| true, usize::MAX),
            MAX_CALLBACKS
        );
        assert_eq!(owner.unregister_event(handler.as_ptr()), sb::kResultOk);
        assert_eq!(dispatch(&pending, Instant::now(), |_| true, 0), 0);
        assert_eq!(hits.load(Ordering::Relaxed), MAX_CALLBACKS);
    }
}
