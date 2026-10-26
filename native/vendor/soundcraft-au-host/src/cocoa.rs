//! AUCocoaUIBase and AUEventListener live on the helper's AppKit thread.
use super::*;
use crate::events::ParameterEvents;
use objc2::encode::{Encoding, RefEncode};
use objc2::rc::Retained;
use objc2::runtime::{AnyObject, Bool};
use objc2::{ClassType, msg_send, sel};
use objc2_app_kit::{NSView, NSViewController, NSWindow};
use objc2_foundation::{NSBundle, NSPoint, NSSize, NSString, NSURL};
use soundcraft_vst3_host::NativeEditorWindow;
use std::sync::Arc;
use std::sync::atomic::AtomicUsize;

const PROP_COCOA_UI: u32 = 31;
const PARAMETER_VALUE_CHANGE: u32 = 0;
const MAX_VIEW_CLASSES: usize = 64;

#[repr(C)]
#[derive(Clone, Copy)]
struct AudioUnitParameter {
    unit: AudioUnit,
    id: u32,
    scope: u32,
    element: u32,
}
#[repr(C)]
struct AudioUnitEvent {
    kind: u32,
    parameter: AudioUnitParameter,
}

#[repr(C)]
pub(super) struct OpaqueAudioComponentInstance {
    _private: [u8; 0],
}
// Preserve the SDK's Objective-C argument encoding as well as the C pointer ABI.
unsafe impl RefEncode for OpaqueAudioComponentInstance {
    const ENCODING_REF: Encoding =
        Encoding::Pointer(&Encoding::Struct("OpaqueAudioComponentInstance", &[]));
}
type EventProc = unsafe extern "C" fn(*mut c_void, *mut c_void, *const AudioUnitEvent, u64, f32);

#[link(name = "AudioToolbox", kind = "framework")]
unsafe extern "C" {
    fn AUEventListenerCreate(
        callback: EventProc,
        user: *mut c_void,
        run_loop: CFTypeRef,
        mode: CFTypeRef,
        interval: f32,
        granularity: f32,
        listener: *mut *mut c_void,
    ) -> OSStatus;
    fn AUEventListenerAddEventType(
        listener: *mut c_void,
        object: *mut c_void,
        event: *const AudioUnitEvent,
    ) -> OSStatus;
    fn AUListenerDispose(listener: *mut c_void) -> OSStatus;
    fn AUEventListenerNotify(
        listener: *mut c_void,
        object: *mut c_void,
        event: *const AudioUnitEvent,
    ) -> OSStatus;
}
#[link(name = "CoreFoundation", kind = "framework")]
unsafe extern "C" {
    fn CFRunLoopGetMain() -> CFTypeRef;
    static kCFRunLoopDefaultMode: CFTypeRef;
    fn CFURLGetTypeID() -> usize;
}

struct ListenerContext {
    enabled: AtomicBool,
    unit: usize,
    events: Arc<ParameterEvents>,
}
unsafe extern "C" fn parameter_changed(
    user: *mut c_void,
    _object: *mut c_void,
    event: *const AudioUnitEvent,
    _time: u64,
    value: f32,
) {
    // SAFETY: the listener owns its context until synchronous main-runloop disposal. A failed
    // disposal retains a disabled context so a late callback can never reach freed memory.
    let Some(context) = (unsafe { (user as *const ListenerContext).as_ref() }) else {
        return;
    };
    if !context.enabled.load(Ordering::Acquire) {
        return;
    }
    let Some(event) = (unsafe { event.as_ref() }) else {
        return;
    };
    if event.kind == PARAMETER_VALUE_CHANGE
        && event.parameter.unit as usize == context.unit
        && event.parameter.scope == SCOPE_GLOBAL
        && event.parameter.element == 0
    {
        context.events.native_change(event.parameter.id, value);
    }
}

struct Listener {
    raw: *mut c_void,
    context: *mut ListenerContext,
    can_dispose_unit: Arc<AtomicBool>,
}
impl Listener {
    fn new(
        unit: AudioUnit,
        events: Arc<ParameterEvents>,
        can_dispose_unit: Arc<AtomicBool>,
    ) -> Result<Self, String> {
        let context = Box::into_raw(Box::new(ListenerContext {
            enabled: AtomicBool::new(true),
            unit: unit as usize,
            events,
        }));
        let mut raw = ptr::null_mut();
        // SAFETY: registration and all listener lifecycle operations are on the main thread.
        let status = unsafe {
            AUEventListenerCreate(
                parameter_changed,
                context.cast(),
                CFRunLoopGetMain(),
                kCFRunLoopDefaultMode,
                0.02,
                0.001,
                &mut raw,
            )
        };
        if status != 0 || raw.is_null() {
            if !raw.is_null() {
                let listener = Self {
                    raw,
                    context,
                    can_dispose_unit,
                };
                drop(listener);
            } else {
                unsafe {
                    drop(Box::from_raw(context));
                }
            }
            return Err(format!(
                "Cannot observe Audio Unit parameter edits ({status})"
            ));
        }
        let listener = Self {
            raw,
            context,
            can_dispose_unit,
        };
        // The immutable table contains at most 4096 writable global parameters.
        for entry in unsafe { &*context }.events.entries() {
            let event = AudioUnitEvent {
                kind: PARAMETER_VALUE_CHANGE,
                parameter: AudioUnitParameter {
                    unit,
                    id: entry.id,
                    scope: SCOPE_GLOBAL,
                    element: 0,
                },
            };
            let status = unsafe { AUEventListenerAddEventType(raw, context.cast(), &event) };
            if status != 0 {
                return Err(format!(
                    "Cannot observe Audio Unit parameter {} ({status})",
                    entry.id
                ));
            }
        }
        Ok(listener)
    }
    fn notify_applied(&self, unit: AudioUnit) {
        let context = unsafe { &*self.context };
        for entry in context.events.entries() {
            if entry.take_host_change() {
                let event = AudioUnitEvent {
                    kind: PARAMETER_VALUE_CHANGE,
                    parameter: AudioUnitParameter {
                        unit,
                        id: entry.id,
                        scope: SCOPE_GLOBAL,
                        element: 0,
                    },
                };
                // Suppress our listener/object pair so worker acknowledgements cannot feed
                // another edit back into the worker. Other native views receive the update.
                unsafe {
                    AUEventListenerNotify(self.raw, self.context.cast(), &event);
                }
            }
        }
    }
}
impl Drop for Listener {
    fn drop(&mut self) {
        unsafe { &*self.context }
            .enabled
            .store(false, Ordering::Release);
        let status = unsafe { AUListenerDispose(self.raw) };
        unsafe {
            finish_listener_disposal(self.context, &self.can_dispose_unit, status);
        }
    }
}

/// The context is owned by the listener and has already been disabled before disposal.
unsafe fn finish_listener_disposal(
    context: *mut ListenerContext,
    can_dispose_unit: &AtomicBool,
    status: OSStatus,
) {
    if status == 0 {
        unsafe {
            drop(Box::from_raw(context));
        }
    } else {
        can_dispose_unit.store(false, Ordering::Release);
        log::error!("AUListenerDispose failed ({status})");
    }
}

/// Owns the property's +1 URL/string references, including every class in its flexible array.
struct CocoaInfo {
    refs: Vec<CFTypeRef>,
}
impl CocoaInfo {
    fn read(unit: AudioUnit) -> Result<Self, String> {
        let mut size = 0;
        let mut writable = 0;
        let status = unsafe {
            AudioUnitGetPropertyInfo(
                unit,
                PROP_COCOA_UI,
                SCOPE_GLOBAL,
                0,
                &mut size,
                &mut writable,
            )
        };
        let pointer_size = std::mem::size_of::<CFTypeRef>();
        if status != 0 {
            return Err("This Audio Unit does not provide a Cocoa editor".into());
        }
        if size as usize % pointer_size != 0
            || !(2..=MAX_VIEW_CLASSES + 1).contains(&(size as usize / pointer_size))
        {
            return Err("Audio Unit Cocoa editor metadata has an invalid size".into());
        }
        let mut refs: Vec<CFTypeRef> = vec![ptr::null(); size as usize / pointer_size];
        let capacity = size;
        let status = unsafe {
            AudioUnitGetProperty(
                unit,
                PROP_COCOA_UI,
                SCOPE_GLOBAL,
                0,
                refs.as_mut_ptr().cast(),
                &mut size,
            )
        };
        let info = Self { refs };
        if status != 0 || size != capacity {
            return Err(format!(
                "Cannot read Audio Unit Cocoa editor metadata ({status})"
            ));
        }
        if info.refs.iter().any(|object| object.is_null()) {
            return Err("Audio Unit Cocoa editor metadata is incomplete".into());
        }
        if unsafe { CFGetTypeID(info.refs[0]) != CFURLGetTypeID() } {
            return Err("Audio Unit Cocoa editor bundle URL is invalid".into());
        }
        for class_name in info.refs.iter().skip(1) {
            if unsafe { CFGetTypeID(*class_name) != CFStringGetTypeID() } {
                return Err("Audio Unit Cocoa editor class name is invalid".into());
            }
        }
        Ok(info)
    }
}
impl Drop for CocoaInfo {
    fn drop(&mut self) {
        for object in &self.refs {
            release(*object);
        }
    }
}

pub(super) struct CocoaEditor {
    unit: AudioUnit,
    listener: Option<Listener>,
    window: Option<NativeEditorWindow>,
    view: Option<Retained<NSView>>,
    factory: Option<Retained<AnyObject>>,
    bundle: Option<Retained<NSBundle>>,
    controller: Option<Retained<NSViewController>>,
    size: NSSize,
}
// These retains may be carried between owner locks, but dereferenced/released only on AppKit's
// main thread. StateLink retires an off-thread instance to the explicit UI cleanup queue.
unsafe impl Send for CocoaEditor {}

fn checked_size(size: NSSize) -> Result<NSSize, String> {
    if size.width.is_finite()
        && size.height.is_finite()
        && (1.0..=16384.0).contains(&size.width)
        && (1.0..=16384.0).contains(&size.height)
    {
        Ok(size)
    } else {
        Err("Audio Unit Cocoa editor size is invalid".into())
    }
}

impl CocoaEditor {
    pub(super) fn new(
        unit: AudioUnit,
        title: &str,
        events: Arc<ParameterEvents>,
        can_dispose_unit: Arc<AtomicBool>,
        is_v3: bool,
        pending_views: Arc<AtomicUsize>,
    ) -> Result<Self, String> {
        if !soundcraft_vst3_host::on_ui_thread() {
            return Err("Audio Unit editors require the main thread".into());
        }
        if is_v3 {
            return Self::new_v3(unit, title, events, can_dispose_unit, pending_views);
        }
        let info = CocoaInfo::read(unit)?;
        // CFURL and NSURL are toll-free bridged; info retains the URL during this call.
        let url = unsafe { &*info.refs[0].cast::<NSURL>() };
        let bundle =
            NSBundle::bundleWithURL(url).ok_or("Cannot open Audio Unit Cocoa editor bundle")?;
        if !unsafe { bundle.load() } {
            return Err("Cannot load Audio Unit Cocoa editor bundle".into());
        }
        // No bundle unload is ever requested: Objective-C classes can remain in the runtime
        // after their views close. NSBundle's loaded executable remains resident by design.
        let mut selected = None;
        for name in info.refs.iter().skip(1).map(|name| cf_string(*name)) {
            let Some(class) = bundle.classNamed(&NSString::from_str(&name)) else {
                continue;
            };
            if class
                .instance_method(sel!(uiViewForAudioUnit:withSize:))
                .is_none()
                || class.instance_method(sel!(interfaceVersion)).is_none()
            {
                continue;
            }
            let factory: Option<Retained<AnyObject>> = unsafe { msg_send![class, new] };
            let Some(factory) = factory else { continue };
            let version: u32 = unsafe { msg_send![&*factory, interfaceVersion] };
            if version != 0 {
                continue;
            }
            selected = Some(factory);
            break;
        }
        let factory = selected.ok_or("No compatible Audio Unit Cocoa view factory is available")?;
        let listener = Listener::new(unit, events, can_dispose_unit)?;
        let view: Option<Retained<NSView>> = unsafe {
            msg_send![&*factory, uiViewForAudioUnit: unit.cast::<OpaqueAudioComponentInstance>(), withSize: NSSize::new(640.0, 480.0)]
        };
        let view = view.ok_or("The Audio Unit Cocoa view factory returned no view")?;
        let is_view: Bool = unsafe { msg_send![&*view, isKindOfClass: NSView::class()] };
        if !is_view.as_bool() {
            return Err("The Audio Unit Cocoa view factory returned an invalid view".into());
        }
        let size = checked_size(view.frame().size)?;
        let window = NativeEditorWindow::new(title, size.width, size.height)?;
        let parent = unsafe { window.parent().cast::<NSView>().as_ref() }
            .ok_or("Audio Unit Cocoa parent view is unavailable")?;
        view.setFrameOrigin(NSPoint::new(0.0, 0.0));
        parent.addSubview(&view);
        window.raise();
        Ok(Self {
            unit,
            listener: Some(listener),
            window: Some(window),
            view: Some(view),
            factory: Some(factory),
            bundle: Some(bundle),
            controller: None,
            size,
        })
    }

    fn new_v3(
        unit: AudioUnit,
        title: &str,
        events: Arc<ParameterEvents>,
        can_dispose_unit: Arc<AtomicBool>,
        pending_views: Arc<AtomicUsize>,
    ) -> Result<Self, String> {
        let listener = Listener::new(unit, events, can_dispose_unit)?;
        let controller = super::async_au::request_view(unit, pending_views)?;
        let view = controller.view();
        let frame = view.frame().size;
        let preferred = controller.preferredContentSize();
        let (width, height) = crate::controller::initial_v3_view_size(
            (frame.width, frame.height),
            (preferred.width, preferred.height),
        );
        let size = NSSize::new(width, height);
        view.setFrameSize(size);
        let window = NativeEditorWindow::new(title, size.width, size.height)?;
        let native = unsafe { window.raw().cast::<NSWindow>().as_ref() }
            .ok_or("Audio Unit view-controller window is unavailable")?;
        // NSWindow owns its content controller so AppKit drives the controller/view lifecycle,
        // including remote AUv3 extension views. Merely adding its NSView is insufficient.
        native.setContentViewController(Some(&controller));
        window.resize(size.width, size.height);
        window.raise();
        Ok(Self {
            unit,
            listener: Some(listener),
            window: Some(window),
            view: Some(view),
            factory: None,
            bundle: None,
            controller: Some(controller),
            size,
        })
    }
    pub(super) fn is_open(&self) -> bool {
        self.window
            .as_ref()
            .is_some_and(NativeEditorWindow::is_visible)
    }
    pub(super) fn raise(&self) {
        if let Some(window) = &self.window {
            window.raise();
        }
    }
    pub(super) fn idle(&mut self) {
        if let (Some(view), Some(window)) = (&self.view, &self.window) {
            if let Ok(size) = checked_size(view.frame().size) {
                if size != self.size {
                    window.resize(size.width, size.height);
                    self.size = size;
                }
            }
        }
        if let Some(listener) = &self.listener {
            listener.notify_applied(self.unit);
        }
    }
}
impl Drop for CocoaEditor {
    fn drop(&mut self) {
        if !soundcraft_vst3_host::on_ui_thread() {
            // Last-resort safety for a caller violating the UI cleanup contract. The normal
            // Instance path always places this entire object on the retirement queue.
            if let Some(value) = self.listener.take() {
                std::mem::forget(value);
            }
            if let Some(value) = self.view.take() {
                std::mem::forget(value);
            }
            if let Some(value) = self.factory.take() {
                std::mem::forget(value);
            }
            if let Some(value) = self.bundle.take() {
                std::mem::forget(value);
            }
            if let Some(value) = self.controller.take() {
                std::mem::forget(value);
            }
            return;
        }
        self.listener.take();
        if self.controller.is_some() {
            if let Some(native) = self
                .window
                .as_ref()
                .and_then(|window| unsafe { window.raw().cast::<NSWindow>().as_ref() })
            {
                native.setContentViewController(None);
            }
        }
        if let Some(view) = &self.view {
            view.removeFromSuperview();
        }
        self.view.take();
        self.controller.take();
        self.factory.take();
        self.window.take();
        self.bundle.take();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use soundcraft_dsp::{Category, ParamInfo, PluginInfo, Taper, Unit};
    static INFO: PluginInfo = PluginInfo {
        id: "au:aufx:test:host",
        name: "Gain",
        short_name: "Gain",
        category: Category::Other,
        params: &[ParamInfo {
            id: "7",
            name: "Gain",
            min: -24.0,
            max: 24.0,
            default: 0.0,
            unit: Unit::Db,
            taper: Taper::Linear,
            choices: &[],
        }],
        is_instrument: false,
        audiosuite: false,
    };

    #[test]
    fn successful_listener_disposal_releases_callback_ownership() {
        let events = Arc::new(ParameterEvents::new(&INFO, &[0.0]));
        let context = Box::into_raw(Box::new(ListenerContext {
            enabled: AtomicBool::new(false),
            unit: 42,
            events: Arc::clone(&events),
        }));
        let can_dispose = AtomicBool::new(true);
        assert_eq!(Arc::strong_count(&events), 2);
        // SAFETY: this allocated disabled context has unique teardown ownership.
        unsafe {
            finish_listener_disposal(context, &can_dispose, 0);
        }
        assert!(can_dispose.load(Ordering::Acquire));
        assert_eq!(Arc::strong_count(&events), 1);
    }

    #[test]
    fn failed_listener_disposal_retains_context_and_prevents_unit_disposal() {
        let events = Arc::new(ParameterEvents::new(&INFO, &[0.0]));
        let context = Box::into_raw(Box::new(ListenerContext {
            enabled: AtomicBool::new(false),
            unit: 42,
            events: Arc::clone(&events),
        }));
        let can_dispose = AtomicBool::new(true);
        // SAFETY: failure must leave the disabled callback context alive for late notifications.
        unsafe {
            finish_listener_disposal(context, &can_dispose, -50);
        }
        assert!(!can_dispose.load(Ordering::Acquire));
        assert_eq!(Arc::strong_count(&events), 2);
        let event = AudioUnitEvent {
            kind: PARAMETER_VALUE_CHANGE,
            parameter: AudioUnitParameter {
                unit: 42usize as AudioUnit,
                id: 7,
                scope: SCOPE_GLOBAL,
                element: 0,
            },
        };
        unsafe {
            parameter_changed(context.cast(), ptr::null_mut(), &event, 0, 3.0);
        }
        assert!(events.drain_native().is_empty());
        // No actual listener exists in this test, so it is safe to reclaim its fake context.
        unsafe {
            drop(Box::from_raw(context));
        }
    }
    #[test]
    fn callback_validates_lifetime_unit_and_parameter_scope() {
        let events = Arc::new(ParameterEvents::new(&INFO, &[0.0]));
        let mut context = ListenerContext {
            enabled: AtomicBool::new(true),
            unit: 42,
            events: Arc::clone(&events),
        };
        let user = ptr::from_mut(&mut context).cast();
        let mut event = AudioUnitEvent {
            kind: PARAMETER_VALUE_CHANGE,
            parameter: AudioUnitParameter {
                unit: 42usize as AudioUnit,
                id: 7,
                scope: SCOPE_GLOBAL,
                element: 0,
            },
        };
        unsafe {
            parameter_changed(user, ptr::null_mut(), &event, 0, 3.0);
        }
        assert_eq!(events.drain_native(), vec![("7".into(), 3.0)]);
        event.parameter.scope = SCOPE_INPUT;
        unsafe {
            parameter_changed(user, ptr::null_mut(), &event, 0, 4.0);
        }
        event.parameter.scope = SCOPE_GLOBAL;
        context.enabled.store(false, Ordering::Release);
        unsafe {
            parameter_changed(user, ptr::null_mut(), &event, 0, 5.0);
        }
        assert!(events.drain_native().is_empty());
    }
    #[test]
    fn event_layout_and_logical_view_sizes_match_the_sdk() {
        assert_eq!(std::mem::size_of::<AudioUnitParameter>(), 24);
        assert_eq!(std::mem::offset_of!(AudioUnitEvent, parameter), 8);
        assert_eq!(std::mem::size_of::<AudioUnitEvent>(), 32);
        assert!(checked_size(NSSize::new(400.0, 300.0)).is_ok());
        assert!(checked_size(NSSize::new(f64::NAN, 300.0)).is_err());
        assert!(checked_size(NSSize::new(400.0, 0.0)).is_err());
    }
}
