//! Documented asynchronous AUv3 C bridge, with UI-thread ownership of late results.
use super::*;
use crate::completion::{Completion, PendingLease};
use block2::{DynBlock, RcBlock};
use objc2::rc::Retained;
use objc2_app_kit::NSViewController;
use std::sync::atomic::AtomicUsize;
use std::time::{Duration, Instant};

const TIMEOUT: Duration = Duration::from_secs(10);
const PROP_REQUEST_VIEW_CONTROLLER: u32 = 56;

#[link(name = "AudioToolbox", kind = "framework")]
unsafe extern "C" {
    fn AudioComponentInstantiate(
        component: AudioComponent,
        options: u32,
        completion: &DynBlock<dyn Fn(*mut cocoa::OpaqueAudioComponentInstance, OSStatus)>,
    );
}
fn load_core_audio_kit() -> Result<(), String> {
    // The C bridge's view-controller support is supplied by CoreAudioKit. Pin its executable
    // explicitly; an otherwise unused framework link can be removed by dead-strip linkers.
    static FRAMEWORK: OnceLock<Result<libloading::Library, String>> = OnceLock::new();
    FRAMEWORK
        .get_or_init(|| unsafe {
            libloading::Library::new(
                "/System/Library/Frameworks/CoreAudioKit.framework/CoreAudioKit",
            )
            .map_err(|error| format!("Cannot load CoreAudioKit: {error}"))
        })
        .as_ref()
        .map(|_| ())
        .map_err(Clone::clone)
}

enum LateResult {
    Unit(usize),
    View {
        pointer: usize,
        lease: Arc<PendingLease>,
    },
}
fn late_results() -> &'static Mutex<Vec<LateResult>> {
    static QUEUE: OnceLock<Mutex<Vec<LateResult>>> = OnceLock::new();
    QUEUE.get_or_init(|| Mutex::new(Vec::new()))
}
fn retire(result: LateResult) {
    late_results()
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .push(result);
}
pub(super) fn retire_uninitialized_unit(unit: AudioUnit) {
    if !unit.is_null() {
        retire(LateResult::Unit(unit as usize));
    }
}
pub(super) fn pump_cleanup() {
    if !soundcraft_vst3_host::on_ui_thread() {
        return;
    }
    let results = std::mem::take(
        &mut *late_results()
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner),
    );
    for result in results {
        match result {
            LateResult::Unit(pointer) if pointer != 0 => {
                let status = unsafe { AudioComponentInstanceDispose(pointer as AudioUnit) };
                if status != 0 {
                    log::error!("Late Audio Unit disposal failed ({status})");
                }
            }
            LateResult::View { pointer, lease } => {
                if pointer != 0 {
                    drop(unsafe {
                        Retained::<NSViewController>::from_raw(pointer as *mut NSViewController)
                    });
                }
                // A timed-out view request must keep its unit alive until the view controller
                // is released on the main thread, including a controller returned very late.
                lease.release();
            }
            _ => {}
        }
    }
}

fn wait<T>(request: &Completion<T>, operation: &str) -> Result<T, String> {
    let deadline = Instant::now() + TIMEOUT;
    loop {
        if let Some(value) = request.take() {
            return Ok(value);
        }
        if Instant::now() >= deadline {
            return request
                .cancel()
                .ok_or_else(|| format!("{operation} timed out"));
        }
        // Do not park the main thread: AU extension instantiation can require main-queue and
        // runloop work. AppKit's bounded pump services both while the helper protocol waits.
        soundcraft_vst3_host::with_ui_context(soundcraft_vst3_host::pump_ui);
        pump_cleanup();
        std::thread::sleep(Duration::from_millis(1));
    }
}

pub(super) fn instantiate(component: AudioComponent, options: u32) -> Result<AudioUnit, String> {
    if !soundcraft_vst3_host::on_ui_thread() {
        return Err("Asynchronous Audio Units require the main thread".into());
    }
    soundcraft_vst3_host::initialize_ui()?;
    let request = Arc::new(Completion::new());
    let callback_request = Arc::clone(&request);
    let callback = RcBlock::new(
        move |unit: *mut cocoa::OpaqueAudioComponentInstance, status: OSStatus| {
            if let Err((pointer, _status)) = callback_request.complete((unit as usize, status)) {
                if pointer != 0 {
                    retire(LateResult::Unit(pointer));
                }
            }
        },
    );
    // Follow the component's instantiation flags rather than its display identity.
    unsafe {
        AudioComponentInstantiate(component, options, &callback);
    }
    let (pointer, status) = wait(&request, "Audio Unit instantiation")?;
    if status != 0 || pointer == 0 {
        if pointer != 0 {
            retire(LateResult::Unit(pointer));
            pump_cleanup();
        }
        return Err(format!("AudioComponentInstantiate failed ({status})"));
    }
    Ok(pointer as AudioUnit)
}

pub(super) fn request_view(
    unit: AudioUnit,
    pending: Arc<AtomicUsize>,
) -> Result<Retained<NSViewController>, String> {
    soundcraft_vst3_host::initialize_ui()?;
    load_core_audio_kit()?;
    let request = Arc::new(Completion::new());
    let callback_request = Arc::clone(&request);
    let lease = Arc::new(PendingLease::new(pending));
    let callback_lease = Arc::clone(&lease);
    let callback = RcBlock::new(move |controller: *mut NSViewController| {
        // The completion can run on any queue. Retain only; AppKit methods and the balancing
        // release are deferred to the main thread.
        let pointer = unsafe { Retained::retain(controller) }
            .map_or(0, |controller| Retained::into_raw(controller) as usize);
        if let Err(pointer) = callback_request.complete(pointer) {
            retire(LateResult::View {
                pointer,
                lease: Arc::clone(&callback_lease),
            });
        }
    });
    let block: *const DynBlock<dyn Fn(*mut NSViewController)> = &*callback;
    let status = unsafe {
        AudioUnitSetProperty(
            unit,
            PROP_REQUEST_VIEW_CONTROLLER,
            SCOPE_GLOBAL,
            0,
            ptr::from_ref(&block).cast(),
            std::mem::size_of_val(&block) as u32,
        )
    };
    if status != 0 {
        // A rejected property did not accept the completion block. If a provider nevertheless
        // calls it synchronously, reclaim that result; otherwise balance this request here.
        if let Some(pointer) = request.cancel() {
            retire(LateResult::View {
                pointer,
                lease: Arc::clone(&lease),
            });
            pump_cleanup();
        }
        return Err(format!(
            "Audio Unit view-controller request failed ({status})"
        ));
    }
    let pointer = wait(&request, "Audio Unit view-controller request")?;
    let controller =
        unsafe { Retained::<NSViewController>::from_raw(pointer as *mut NSViewController) };
    lease.release();
    controller.ok_or_else(|| "This Audio Unit does not provide a custom view controller".into())
}
