//! X11/XWayland editor windows. The host owns a private X connection and only dispatches
//! its own embedding events; plug-in connections are driven by the VST3 IRunLoop.

use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::{CString, c_char, c_int, c_long, c_void};
use std::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};
use x11_dl::xlib::{self as x, Xlib};

pub const PLATFORM_TYPE: Option<*const c_char> =
    Some(vst3::Steinberg::kPlatformTypeX11EmbedWindowID);
const MAX_WINDOWS: usize = 256;
const MAX_CHILDREN: usize = 256;
const MAX_EVENTS: usize = 512;
const EMBEDDED_NOTIFY: c_long = 0;
const WINDOW_ACTIVATE: c_long = 1;
const WINDOW_DEACTIVATE: c_long = 2;
const REQUEST_FOCUS: c_long = 3;
const FOCUS_IN: c_long = 4;
const FOCUS_OUT: c_long = 5;
const FOCUS_CURRENT: c_long = 0;
static UI_THREAD: AtomicUsize = AtomicUsize::new(0);
static DISPLAY: AtomicPtr<x::Display> = AtomicPtr::new(std::ptr::null_mut());
type ErrorHandler = unsafe extern "C" fn(*mut x::Display, *mut x::XErrorEvent) -> c_int;
static PREVIOUS_ERROR_HANDLER: AtomicUsize = AtomicUsize::new(0);

thread_local! { static HOST: RefCell<Option<Result<Connection, String>>> = const { RefCell::new(None) }; }

// X errors are asynchronous: a child can be destroyed after an event was queued. Swallow
// stale-window errors for this host connection, delegating every other connection/error to
// the process's previous handler. Plug-in connections never share our event queue.
unsafe extern "C" fn error_handler(display: *mut x::Display, event: *mut x::XErrorEvent) -> c_int {
    if display == DISPLAY.load(Ordering::Acquire)
        && !event.is_null()
        // SAFETY: Xlib supplies a valid error event for this call.
        && unsafe {
            (*event).error_code == x::BadWindow
                // A window manager can defer mapping; SetInputFocus then reports BadMatch.
                || ((*event).error_code == x::BadMatch && (*event).request_code == 42)
        }
    {
        return 0;
    }
    let previous = PREVIOUS_ERROR_HANDLER.load(Ordering::Acquire);
    if previous == 0 {
        return 0;
    }
    // SAFETY: this value came from XSetErrorHandler and retains that function's exact ABI.
    let previous: ErrorHandler = unsafe { std::mem::transmute(previous) };
    // SAFETY: pass through the unmodified Xlib callback arguments.
    unsafe { previous(display, event) }
}

pub fn on_main_thread() -> bool {
    let owner = UI_THREAD.load(Ordering::Acquire);
    if owner == 0 {
        super::on_main_thread()
    } else {
        owner == super::os_thread()
    }
}

/// Establish UI ownership before any plug-in is loaded. Missing X11/DISPLAY is deliberately
/// deferred to editor creation so headless scanning, playback and rendering remain available.
pub fn initialize() -> Result<(), String> {
    if !on_main_thread() {
        return Err("plugin windows must be initialized on the main thread".into());
    }
    UI_THREAD.store(super::os_thread(), Ordering::Release);
    HOST.with(|host| {
        let mut host = host.borrow_mut();
        if host.is_none() {
            *host = Some(Connection::new());
        }
    });
    Ok(())
}

#[derive(Default)]
struct Child {
    notified: bool,
}
struct Window {
    visible: bool,
    focused: bool,
    children: HashMap<x::Window, Child>,
}
struct Connection {
    x: Xlib,
    display: *mut x::Display,
    wm_delete: x::Atom,
    wm_protocols: x::Atom,
    xembed: x::Atom,
    xembed_info: x::Atom,
    windows: HashMap<x::Window, Window>,
}

impl Connection {
    fn new() -> Result<Self, String> {
        let x =
            Xlib::open().map_err(|error| format!("X11 editor library is unavailable: {error}"))?;
        // SAFETY: called before loading plug-ins or opening any display in this helper.
        if unsafe { (x.XInitThreads)() } == 0 {
            return Err("X11 thread initialization failed".into());
        }
        Ok(Self {
            x,
            display: std::ptr::null_mut(),
            wm_delete: 0,
            wm_protocols: 0,
            xembed: 0,
            xembed_info: 0,
            windows: HashMap::new(),
        })
    }

    fn connect(&mut self) -> Result<(), String> {
        if !self.display.is_null() {
            return Ok(());
        }
        // SAFETY: null uses the current DISPLAY environment; the returned connection is ours.
        self.display = unsafe { (self.x.XOpenDisplay)(std::ptr::null()) };
        if self.display.is_null() {
            return Err("an X11 or XWayland display is required to open this editor".into());
        }
        DISPLAY.store(self.display, Ordering::Release);
        // SAFETY: install a process callback with a static lifetime. Other connections are
        // forwarded to the previous handler; only expected stale child-window errors are ignored.
        let previous = unsafe { (self.x.XSetErrorHandler)(Some(error_handler)) };
        PREVIOUS_ERROR_HANDLER.store(
            previous.map_or(0, |handler| handler as usize),
            Ordering::Release,
        );
        // SAFETY: live connection, static NUL-terminated atom names.
        unsafe {
            self.wm_delete = (self.x.XInternAtom)(self.display, c"WM_DELETE_WINDOW".as_ptr(), 0);
            self.wm_protocols = (self.x.XInternAtom)(self.display, c"WM_PROTOCOLS".as_ptr(), 0);
            self.xembed = (self.x.XInternAtom)(self.display, c"_XEMBED".as_ptr(), 0);
            self.xembed_info = (self.x.XInternAtom)(self.display, c"_XEMBED_INFO".as_ptr(), 0);
        }
        Ok(())
    }

    fn send_embed(
        &self,
        child: x::Window,
        message: c_long,
        detail: c_long,
        data1: c_long,
        data2: c_long,
    ) {
        let mut data = x::ClientMessageData::new();
        for (index, value) in [x::CurrentTime as c_long, message, detail, data1, data2]
            .into_iter()
            .enumerate()
        {
            data.set_long(index, value);
        }
        let mut event = x::XEvent {
            client_message: x::XClientMessageEvent {
                type_: x::ClientMessage,
                serial: 0,
                send_event: 1,
                display: self.display,
                window: child,
                message_type: self.xembed,
                format: 32,
                data,
            },
        };
        // SAFETY: a complete XClientMessageEvent. Stale child handles are caught by our handler.
        unsafe { (self.x.XSendEvent)(self.display, child, 0, x::NoEventMask, &mut event) };
    }

    fn child_flags(&self, child: x::Window) -> Option<c_long> {
        let (mut actual_type, mut format, mut count, mut remaining) = (0, 0, 0, 0);
        let mut value = std::ptr::null_mut();
        // SAFETY: all outputs are writable; request at most the two defined XEmbed fields.
        let result = unsafe {
            (self.x.XGetWindowProperty)(
                self.display,
                child,
                self.xembed_info,
                0,
                2,
                0,
                self.xembed_info,
                &mut actual_type,
                &mut format,
                &mut count,
                &mut remaining,
                &mut value,
            )
        };
        let flags = if result == x::Success as c_int
            && actual_type == self.xembed_info
            && format == 32
            && count >= 2
            && !value.is_null()
        {
            // SAFETY: Xlib returns format-32 properties as native longs; two elements checked.
            Some(unsafe { *value.cast::<c_long>().add(1) })
        } else {
            None
        };
        if !value.is_null() {
            // SAFETY: XGetWindowProperty allocates this buffer with Xlib's allocator.
            unsafe { (self.x.XFree)(value.cast()) };
        }
        flags
    }

    fn add_child(&mut self, parent: x::Window, child: x::Window) {
        let Some(window) = self.windows.get_mut(&parent) else {
            return;
        };
        if window.children.len() >= MAX_CHILDREN || window.children.contains_key(&child) {
            return;
        }
        window.children.insert(child, Child::default());
        // SAFETY: select only notifications, never redirect the plug-in's requests.
        unsafe {
            (self.x.XSelectInput)(
                self.display,
                child,
                x::PropertyChangeMask | x::StructureNotifyMask,
            )
        };
        self.refresh_child(parent, child);
    }

    fn refresh_child(&mut self, parent: x::Window, child: x::Window) {
        let Some(flags) = self.child_flags(child) else {
            return;
        };
        let Some(window) = self.windows.get_mut(&parent) else {
            return;
        };
        let Some(record) = window.children.get_mut(&child) else {
            return;
        };
        let (notify, visible, focused) = (!record.notified, window.visible, window.focused);
        record.notified = true;
        if notify {
            // The negotiated protocol version is min(client version, host version 0).
            self.send_embed(child, EMBEDDED_NOTIFY, 0, parent as c_long, 0);
            if visible {
                self.send_embed(child, WINDOW_ACTIVATE, 0, 0, 0);
            }
            if focused {
                self.send_embed(child, FOCUS_IN, FOCUS_CURRENT, 0, 0);
            }
        }
        // SAFETY: XEMBED_MAPPED governs whether the child is mapped. Host visibility is separate.
        unsafe {
            if flags & 1 != 0 {
                (self.x.XMapWindow)(self.display, child);
            } else {
                (self.x.XUnmapWindow)(self.display, child);
            }
        }
    }

    fn find_children(&mut self, parent: x::Window) {
        let (mut root, mut actual_parent, mut count) = (0, 0, 0);
        let mut children = std::ptr::null_mut();
        // SAFETY: outputs are writable; Xlib owns the returned array until XFree below.
        let success = unsafe {
            (self.x.XQueryTree)(
                self.display,
                parent,
                &mut root,
                &mut actual_parent,
                &mut children,
                &mut count,
            )
        };
        if success != 0 && !children.is_null() {
            // SAFETY: the returned array has `count` windows; cap work on hostile plug-ins.
            let ids =
                unsafe { std::slice::from_raw_parts(children, (count as usize).min(MAX_CHILDREN)) }
                    .to_vec();
            for child in ids {
                self.add_child(parent, child);
            }
        }
        if !children.is_null() {
            // SAFETY: allocated by XQueryTree above.
            unsafe { (self.x.XFree)(children.cast()) };
        }
    }

    fn focus(&mut self, parent: x::Window, focused: bool) {
        let Some(window) = self.windows.get_mut(&parent) else {
            return;
        };
        window.focused = focused;
        let children: Vec<_> = window
            .children
            .iter()
            .filter_map(|(id, child)| child.notified.then_some(*id))
            .collect();
        for child in children {
            self.send_embed(
                child,
                if focused {
                    WINDOW_ACTIVATE
                } else {
                    WINDOW_DEACTIVATE
                },
                0,
                0,
                0,
            );
            self.send_embed(
                child,
                if focused { FOCUS_IN } else { FOCUS_OUT },
                FOCUS_CURRENT,
                0,
                0,
            );
        }
    }

    fn hide(&mut self, parent: x::Window) {
        self.focus(parent, false);
        if let Some(window) = self.windows.get_mut(&parent) {
            window.visible = false;
        }
        // SAFETY: the host owns this window. Its view is detached by EditorLink on the next idle.
        unsafe { (self.x.XUnmapWindow)(self.display, parent) };
    }

    fn event(&mut self, event: x::XEvent) {
        // SAFETY: the discriminant selects the matching XEvent union member below.
        unsafe {
            match event.get_type() {
                x::ClientMessage => {
                    let message = event.client_message;
                    if message.message_type == self.wm_protocols
                        && message.format == 32
                        && message.data.get_long(0) as x::Atom == self.wm_delete
                        && self.windows.contains_key(&message.window)
                    {
                        self.hide(message.window);
                    } else if message.message_type == self.xembed
                        && message.format == 32
                        && message.data.get_long(1) == REQUEST_FOCUS
                        && self
                            .windows
                            .get(&message.window)
                            .is_some_and(|window| window.visible)
                    {
                        (self.x.XSetInputFocus)(
                            self.display,
                            message.window,
                            x::RevertToParent,
                            x::CurrentTime,
                        );
                        self.focus(message.window, true);
                    }
                }
                x::CreateNotify => {
                    self.add_child(event.create_window.parent, event.create_window.window)
                }
                x::ReparentNotify => {
                    let change = event.reparent;
                    for (id, window) in &mut self.windows {
                        if *id != change.parent {
                            window.children.remove(&change.window);
                        }
                    }
                    self.add_child(change.parent, change.window);
                }
                x::DestroyNotify => {
                    let id = event.destroy_window.window;
                    self.windows.remove(&id);
                    for window in self.windows.values_mut() {
                        window.children.remove(&id);
                    }
                }
                x::PropertyNotify if event.property.atom == self.xembed_info => {
                    let child = event.property.window;
                    if let Some(parent) = self.windows.iter().find_map(|(id, window)| {
                        window.children.contains_key(&child).then_some(*id)
                    }) {
                        self.refresh_child(parent, child);
                    }
                }
                x::FocusIn if event.focus_change.detail != x::NotifyInferior => {
                    self.focus(event.focus_change.window, true)
                }
                x::FocusOut if event.focus_change.detail != x::NotifyInferior => {
                    self.focus(event.focus_change.window, false)
                }
                _ => {}
            }
        }
    }

    fn pump(&mut self) {
        if self.display.is_null() {
            return;
        }
        for _ in 0..MAX_EVENTS {
            // SAFETY: live connection. XPending flushes requests and never waits for an event.
            if unsafe { (self.x.XPending)(self.display) } == 0 {
                break;
            }
            // SAFETY: zero is a valid uninitialised-event representation, immediately overwritten.
            let mut event = unsafe { std::mem::zeroed() };
            // SAFETY: the event queue is nonempty and only this UI thread consumes it.
            unsafe { (self.x.XNextEvent)(self.display, &mut event) };
            self.event(event);
        }
        // SAFETY: flush pending embedding notifications without waiting for a server reply.
        unsafe { (self.x.XFlush)(self.display) };
    }
}

pub fn pump() {
    if !on_main_thread() {
        return;
    }
    HOST.with(|host| {
        if let Some(Ok(connection)) = host.borrow_mut().as_mut() {
            connection.pump();
        }
    });
    // No RefCell borrow (or Xlib lock) survives across callbacks into plug-ins.
    super::linux_run_loop::pump();
}

pub fn shutdown() {
    if !on_main_thread() {
        return;
    }
    super::linux_run_loop::shutdown();
    HOST.with(|host| {
        if let Some(Ok(mut connection)) = host.borrow_mut().take() {
            if !connection.display.is_null() {
                for id in connection.windows.keys() {
                    // SAFETY: all plug-in views have already detached; these windows are ours.
                    unsafe { (connection.x.XDestroyWindow)(connection.display, *id) };
                }
                // SAFETY: balance XOpenDisplay. Process ends after shutdown; no reconnection.
                unsafe { (connection.x.XCloseDisplay)(connection.display) };
                DISPLAY.store(std::ptr::null_mut(), Ordering::Release);
                connection.display = std::ptr::null_mut();
                let previous = PREVIOUS_ERROR_HANDLER.load(Ordering::Acquire);
                let previous = if previous == 0 {
                    None
                } else {
                    // SAFETY: stored from XSetErrorHandler with this exact callback ABI.
                    Some(unsafe { std::mem::transmute::<usize, ErrorHandler>(previous) })
                };
                // SAFETY: restore our predecessor. If a plug-in installed a newer handler,
                // preserve that newer handler instead of overwriting its process setting.
                let current = unsafe { (connection.x.XSetErrorHandler)(previous) };
                if current.map(|handler| handler as usize)
                    != Some(error_handler as *const () as usize)
                {
                    unsafe { (connection.x.XSetErrorHandler)(current) };
                }
                PREVIOUS_ERROR_HANDLER.store(0, Ordering::Release);
            }
        }
    });
}

/// An integer XID is safe to move; all Xlib use stays on the owning UI thread.
pub struct HostWindow {
    id: x::Window,
}

impl HostWindow {
    pub fn new(title: &str, w: f64, h: f64) -> Result<Self, String> {
        initialize()?;
        HOST.with(|host| {
            let mut host = host.borrow_mut();
            let connection = host
                .as_mut()
                .ok_or("X11 editor host is unavailable")?
                .as_mut()
                .map_err(|error| error.clone())?;
            connection.connect()?;
            if connection.windows.len() >= MAX_WINDOWS {
                return Err("too many plugin windows are open".into());
            }
            let title = CString::new(
                title
                    .chars()
                    .filter(|c| *c != '\0')
                    .take(1024)
                    .collect::<String>(),
            )
            .map_err(|_| "invalid editor title")?;
            // SAFETY: live display and root; sane dimensions from rect_size.
            let id = unsafe {
                (connection.x.XCreateSimpleWindow)(
                    connection.display,
                    (connection.x.XDefaultRootWindow)(connection.display),
                    100,
                    100,
                    w as u32,
                    h as u32,
                    0,
                    0,
                    0,
                )
            };
            if id == 0 {
                return Err("cannot create the plugin editor window".into());
            }
            // SAFETY: host-owned window and valid title/protocol buffers for this call.
            unsafe {
                (connection.x.XStoreName)(connection.display, id, title.as_ptr());
                let name_atom =
                    (connection.x.XInternAtom)(connection.display, c"_NET_WM_NAME".as_ptr(), 0);
                let utf8_atom =
                    (connection.x.XInternAtom)(connection.display, c"UTF8_STRING".as_ptr(), 0);
                (connection.x.XChangeProperty)(
                    connection.display,
                    id,
                    name_atom,
                    utf8_atom,
                    8,
                    x::PropModeReplace,
                    title.as_ptr().cast(),
                    title.as_bytes().len() as c_int,
                );
                (connection.x.XSelectInput)(
                    connection.display,
                    id,
                    x::StructureNotifyMask | x::SubstructureNotifyMask | x::FocusChangeMask,
                );
                (connection.x.XSetWMProtocols)(
                    connection.display,
                    id,
                    &mut connection.wm_delete,
                    1,
                );
                let mut hints: x::XSizeHints = std::mem::zeroed();
                hints.flags = x::PMinSize | x::PMaxSize;
                hints.min_width = w as c_int;
                hints.max_width = w as c_int;
                hints.min_height = h as c_int;
                hints.max_height = h as c_int;
                (connection.x.XSetWMNormalHints)(connection.display, id, &mut hints);
            }
            connection.windows.insert(
                id,
                Window {
                    visible: false,
                    focused: false,
                    children: HashMap::new(),
                },
            );
            // The plugin attaches through its own Display connection. Complete creation on
            // the server before handing out the XID, or its reparent request can race our
            // buffered XCreateWindow and fail with BadWindow.
            // SAFETY: this UI thread owns the live connection; discard=false preserves events.
            unsafe { (connection.x.XSync)(connection.display, 0) };
            Ok(Self { id })
        })
    }
    pub fn raw(&self) -> *mut c_void {
        self.id as usize as *mut c_void
    }
    pub fn parent(&self) -> *mut c_void {
        self.raw()
    }
    pub fn is_visible(&self) -> bool {
        on_main_thread()
            && HOST.with(|host| {
                host.borrow()
                    .as_ref()
                    .and_then(|host| host.as_ref().ok())
                    .and_then(|connection| connection.windows.get(&self.id))
                    .is_some_and(|window| window.visible)
            })
    }
    pub fn raise(&self) {
        if !on_main_thread() {
            return;
        }
        HOST.with(|host| {
            if let Some(Ok(connection)) = host.borrow_mut().as_mut() {
                if let Some(window) = connection.windows.get_mut(&self.id) {
                    window.visible = true;
                    connection.find_children(self.id);
                    // SAFETY: this live host-owned window is ready for the window manager.
                    unsafe {
                        (connection.x.XMapRaised)(connection.display, self.id);
                        (connection.x.XFlush)(connection.display);
                    }
                }
            }
        });
    }
    pub fn close(self) {
        if !on_main_thread() {
            return;
        }
        HOST.with(|host| {
            if let Some(Ok(connection)) = host.borrow_mut().as_mut() {
                if connection.windows.remove(&self.id).is_some() {
                    // SAFETY: EditorLink detached the plug-in view before closing this window.
                    unsafe {
                        (connection.x.XDestroyWindow)(connection.display, self.id);
                        (connection.x.XFlush)(connection.display);
                    }
                }
            }
        });
    }
}

pub fn resize(window: *mut c_void, w: f64, h: f64) {
    if !on_main_thread() || window.is_null() {
        return;
    }
    let id = window as usize as x::Window;
    HOST.with(|host| {
        if let Some(Ok(connection)) = host.borrow_mut().as_mut() {
            if connection.windows.contains_key(&id) {
                // SAFETY: only the main thread calls this, while the frame owns a live window.
                unsafe {
                    let mut hints: x::XSizeHints = std::mem::zeroed();
                    hints.flags = x::PMinSize | x::PMaxSize;
                    hints.min_width = w as c_int;
                    hints.max_width = w as c_int;
                    hints.min_height = h as c_int;
                    hints.max_height = h as c_int;
                    (connection.x.XSetWMNormalHints)(connection.display, id, &mut hints);
                    (connection.x.XResizeWindow)(connection.display, id, w as u32, h as u32);
                    (connection.x.XFlush)(connection.display);
                }
            }
        }
    });
}
