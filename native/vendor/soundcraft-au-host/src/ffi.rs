//! The only module with `unsafe`: hand-written AudioToolbox / CoreFoundation declarations and a
//! thin safe wrapper over one Audio Unit instance.
//!
//! Every pointer handed to the Audio Unit points into memory this module owns for at least as
//! long as the unit can use it: the input state the render callback reads and the host time the
//! host callbacks read are leaked `Box`es freed only after the unit is disposed, and output buffers
//! are re-pointed before each render.

use soundcraft_dsp::Transport;
use std::ffi::{CStr, c_char, c_void};
use std::ptr;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
#[path = "async_au.rs"]
mod async_au;
#[path = "cocoa.rs"]
mod cocoa;

type OSStatus = i32;
type AudioComponent = *mut c_void;
type AudioUnit = *mut c_void;
type CFTypeRef = *const c_void;
type CFIndex = isize;

/// Most channels on one Audio Unit bus that we set up.
pub const MAX_AU_CHANNELS: usize = 8;
/// Largest render slice we configure.
pub const MAX_SLICE: usize = 8192;
/// Most parameters read from one unit.
const MAX_PARAMS: usize = 4096;
/// Most components listed for one type.
const MAX_COMPONENTS: usize = 4096;
/// Most value strings read for one indexed parameter.
const MAX_VALUE_STRINGS: CFIndex = 256;
/// Largest state blob accepted or produced.
const MAX_STATE: usize = 32 << 20;
/// Longest CFString converted (bytes).
const MAX_STRING: CFIndex = 4096;

const K_AUDIO_COMPONENT_FLAG_IS_V3: u32 = 4;

const PROP_CLASS_INFO: u32 = 0;
const PROP_PARAMETER_LIST: u32 = 3;
const PROP_PARAMETER_INFO: u32 = 4;
const PROP_STREAM_FORMAT: u32 = 8;
const PROP_ELEMENT_COUNT: u32 = 11;
const PROP_LATENCY: u32 = 12;
const PROP_MAX_FRAMES: u32 = 14;
const PROP_PARAMETER_VALUE_STRINGS: u32 = 16;
const PROP_TAIL_TIME: u32 = 20;
const PROP_SET_RENDER_CALLBACK: u32 = 23;
const PROP_HOST_CALLBACKS: u32 = 27;
const PROP_OFFLINE_RENDER: u32 = 37;

const SCOPE_GLOBAL: u32 = 0;
const SCOPE_INPUT: u32 = 1;
const SCOPE_OUTPUT: u32 = 2;

const FORMAT_LINEAR_PCM: u32 = u32::from_be_bytes(*b"lpcm");
const FORMAT_FLAG_FLOAT: u32 = 1;
const FORMAT_FLAG_PACKED: u32 = 1 << 3;
const FORMAT_FLAG_NON_INTERLEAVED: u32 = 1 << 5;

const TIMESTAMP_SAMPLE_TIME_VALID: u32 = 1;

const CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
const CF_PROPERTY_LIST_BINARY_FORMAT: CFIndex = 200;

pub const PARAM_FLAG_CF_NAME_RELEASE: u32 = 1 << 4;
pub const PARAM_FLAG_HAS_CF_NAME: u32 = 1 << 27;
pub const PARAM_FLAG_DISPLAY_LOG: u32 = 1 << 22;
#[cfg(test)]
pub const PARAM_FLAG_READABLE: u32 = 1 << 30;
pub const PARAM_FLAG_WRITABLE: u32 = 1 << 31;
pub const PARAM_FLAG_METER_READ_ONLY: u32 = 1 << 15;

pub const UNIT_INDEXED: u32 = 1;
pub const UNIT_CUSTOM: u32 = 26;

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct AudioComponentDescription {
    component_type: u32,
    component_sub_type: u32,
    component_manufacturer: u32,
    component_flags: u32,
    component_flags_mask: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct AudioStreamBasicDescription {
    sample_rate: f64,
    format_id: u32,
    format_flags: u32,
    bytes_per_packet: u32,
    frames_per_packet: u32,
    bytes_per_frame: u32,
    channels_per_frame: u32,
    bits_per_channel: u32,
    reserved: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct SmpteTime {
    subframes: i16,
    subframe_divisor: i16,
    counter: u32,
    smpte_type: u32,
    flags: u32,
    hours: i16,
    minutes: i16,
    seconds: i16,
    frames: i16,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct AudioTimeStamp {
    sample_time: f64,
    host_time: u64,
    rate_scalar: f64,
    word_clock_time: u64,
    smpte_time: SmpteTime,
    flags: u32,
    reserved: u32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct AudioBuffer {
    number_channels: u32,
    data_byte_size: u32,
    data: *mut c_void,
}

/// `AudioBufferList` with room for [`MAX_AU_CHANNELS`] buffers (the unit reads only
/// `number_buffers` of them).
#[repr(C)]
struct AudioBufferList {
    number_buffers: u32,
    buffers: [AudioBuffer; MAX_AU_CHANNELS],
}

/// The variable-length list the unit hands the render callback (we only see its header and
/// index into the buffers it says it has).
#[repr(C)]
struct AudioBufferListHeader {
    number_buffers: u32,
    first: AudioBuffer,
}

type BeatAndTempoProc = unsafe extern "C" fn(*mut c_void, *mut f64, *mut f64) -> OSStatus;
type MusicalTimeProc =
    unsafe extern "C" fn(*mut c_void, *mut u32, *mut f32, *mut u32, *mut f64) -> OSStatus;
type TransportStateProc = unsafe extern "C" fn(
    *mut c_void,
    *mut u8,
    *mut u8,
    *mut f64,
    *mut u8,
    *mut f64,
    *mut f64,
) -> OSStatus;
type TransportState2Proc = unsafe extern "C" fn(
    *mut c_void,
    *mut u8,
    *mut u8,
    *mut u8,
    *mut f64,
    *mut u8,
    *mut f64,
    *mut f64,
) -> OSStatus;

#[repr(C)]
struct HostCallbackInfo {
    host_user_data: *mut c_void,
    beat_and_tempo: Option<BeatAndTempoProc>,
    musical_time_location: Option<MusicalTimeProc>,
    transport_state: Option<TransportStateProc>,
    transport_state2: Option<TransportState2Proc>,
}

/// What the host callbacks report: the transport of the block being rendered. Atomics, because a
/// unit may also ask from another thread.
struct HostTime {
    playing: AtomicBool,
    recording: AtomicBool,
    changed: AtomicBool,
    samples: AtomicI64,
    tempo: AtomicU64,
    quarters: AtomicU64,
    bar_start: AtomicU64,
    numerator: AtomicU32,
    denominator: AtomicU32,
    sample_rate: AtomicU64,
}

impl HostTime {
    fn new() -> HostTime {
        HostTime {
            playing: AtomicBool::new(false),
            recording: AtomicBool::new(false),
            changed: AtomicBool::new(false),
            samples: AtomicI64::new(0),
            tempo: AtomicU64::new(120f64.to_bits()),
            quarters: AtomicU64::new(0),
            bar_start: AtomicU64::new(0),
            numerator: AtomicU32::new(4),
            denominator: AtomicU32::new(4),
            sample_rate: AtomicU64::new(48_000f64.to_bits()),
        }
    }

    fn set(&self, t: &Transport) {
        if self.playing.swap(t.playing, Ordering::Relaxed) != t.playing {
            self.changed.store(true, Ordering::Relaxed);
        }
        if self.recording.swap(t.recording, Ordering::Relaxed) != t.recording {
            self.changed.store(true, Ordering::Relaxed);
        }
        self.samples.store(t.samples, Ordering::Relaxed);
        let tempo = if t.tempo.is_finite() && t.tempo > 0.0 {
            t.tempo
        } else {
            120.0
        };
        self.tempo.store(tempo.to_bits(), Ordering::Relaxed);
        self.quarters.store(
            if t.quarters.is_finite() {
                t.quarters.to_bits()
            } else {
                0
            },
            Ordering::Relaxed,
        );
        self.bar_start.store(
            if t.bar_start.is_finite() {
                t.bar_start.to_bits()
            } else {
                0
            },
            Ordering::Relaxed,
        );
        self.numerator
            .store(t.numerator.clamp(1, 32), Ordering::Relaxed);
        self.denominator.store(
            if t.denominator.is_power_of_two() && t.denominator <= 64 {
                t.denominator
            } else {
                4
            },
            Ordering::Relaxed,
        );
    }
}

fn load_f64(a: &AtomicU64) -> f64 {
    f64::from_bits(a.load(Ordering::Relaxed))
}

/// Writes through an out-pointer the unit may leave null.
///
/// # Safety
/// `p` is null or valid for writing a `T`.
unsafe fn put<T>(p: *mut T, v: T) {
    // SAFETY: per the contract above.
    if let Some(p) = unsafe { p.as_mut() } {
        *p = v;
    }
}

unsafe extern "C" fn beat_and_tempo(
    user: *mut c_void,
    beat: *mut f64,
    tempo: *mut f64,
) -> OSStatus {
    // SAFETY: `user` is the `HostTime` leaked by `Instance::new`, alive until after the unit is
    // disposed.
    let Some(t) = (unsafe { (user as *const HostTime).as_ref() }) else {
        return -50;
    };
    // SAFETY: the unit's out-pointers (or null).
    unsafe {
        put(beat, load_f64(&t.quarters));
        put(tempo, load_f64(&t.tempo));
    }
    0
}

unsafe extern "C" fn musical_time(
    user: *mut c_void,
    to_next_beat: *mut u32,
    numerator: *mut f32,
    denominator: *mut u32,
    downbeat: *mut f64,
) -> OSStatus {
    // SAFETY: as in `beat_and_tempo`.
    let Some(t) = (unsafe { (user as *const HostTime).as_ref() }) else {
        return -50;
    };
    let q = load_f64(&t.quarters);
    let per_quarter = load_f64(&t.sample_rate) * 60.0 / load_f64(&t.tempo).max(1.0);
    let next = ((q.ceil() - q) * per_quarter).round();
    let next = if next.is_finite() {
        next.clamp(0.0, f64::from(u32::MAX)) as u32
    } else {
        0
    };
    // SAFETY: the unit's out-pointers (or null).
    unsafe {
        put(to_next_beat, next);
        put(numerator, t.numerator.load(Ordering::Relaxed) as f32);
        put(denominator, t.denominator.load(Ordering::Relaxed));
        put(downbeat, load_f64(&t.bar_start));
    }
    0
}

unsafe extern "C" fn transport_state2(
    user: *mut c_void,
    playing: *mut u8,
    recording: *mut u8,
    changed: *mut u8,
    sample: *mut f64,
    cycling: *mut u8,
    cycle_start: *mut f64,
    cycle_end: *mut f64,
) -> OSStatus {
    // SAFETY: as in `beat_and_tempo`.
    let Some(t) = (unsafe { (user as *const HostTime).as_ref() }) else {
        return -50;
    };
    // SAFETY: the unit's out-pointers (or null).
    unsafe {
        put(playing, u8::from(t.playing.load(Ordering::Relaxed)));
        put(recording, u8::from(t.recording.load(Ordering::Relaxed)));
        put(changed, u8::from(t.changed.swap(false, Ordering::Relaxed)));
        put(sample, t.samples.load(Ordering::Relaxed) as f64);
        put(cycling, 0);
        put(cycle_start, 0.0);
        put(cycle_end, 0.0);
    }
    0
}

unsafe extern "C" fn transport_state(
    user: *mut c_void,
    playing: *mut u8,
    changed: *mut u8,
    sample: *mut f64,
    cycling: *mut u8,
    cycle_start: *mut f64,
    cycle_end: *mut f64,
) -> OSStatus {
    // SAFETY: the same pointers as `transport_state2`, without the recording flag.
    unsafe {
        transport_state2(
            user,
            playing,
            ptr::null_mut(),
            changed,
            sample,
            cycling,
            cycle_start,
            cycle_end,
        )
    }
}

type AuRenderCallback = unsafe extern "C" fn(
    ref_con: *mut c_void,
    action_flags: *mut u32,
    time_stamp: *const AudioTimeStamp,
    bus: u32,
    frames: u32,
    data: *mut AudioBufferListHeader,
) -> OSStatus;

#[repr(C)]
struct AuRenderCallbackStruct {
    input_proc: Option<AuRenderCallback>,
    input_proc_ref_con: *mut c_void,
}

#[repr(C)]
struct AudioUnitParameterInfo {
    name: [c_char; 52],
    unit_name: CFTypeRef,
    clump_id: u32,
    cf_name_string: CFTypeRef,
    unit: u32,
    min_value: f32,
    max_value: f32,
    default_value: f32,
    flags: u32,
}

struct PropertyChanges {
    dirty: AtomicBool,
}
type PropertyListener = unsafe extern "C" fn(*mut c_void, AudioUnit, u32, u32, u32);

unsafe extern "C" fn property_changed(
    user: *mut c_void,
    _unit: AudioUnit,
    _id: u32,
    _scope: u32,
    _element: u32,
) {
    // SAFETY: Instance owns this allocation until its listeners have been removed and the unit
    // disposed. Notifications only publish a flag; they never call back into the Audio Unit.
    if let Some(changes) = unsafe { (user as *const PropertyChanges).as_ref() } {
        changes.dirty.store(true, Ordering::Release);
    }
}

#[link(name = "AudioToolbox", kind = "framework")]
unsafe extern "C" {
    fn AudioComponentFindNext(
        component: AudioComponent,
        desc: *const AudioComponentDescription,
    ) -> AudioComponent;
    fn AudioComponentCopyName(component: AudioComponent, name: *mut CFTypeRef) -> OSStatus;
    fn AudioComponentGetDescription(
        component: AudioComponent,
        desc: *mut AudioComponentDescription,
    ) -> OSStatus;
    fn AudioComponentGetVersion(component: AudioComponent, version: *mut u32) -> OSStatus;
    fn AudioComponentInstanceNew(component: AudioComponent, instance: *mut AudioUnit) -> OSStatus;
    fn AudioComponentInstanceDispose(instance: AudioUnit) -> OSStatus;
    fn AudioUnitInitialize(unit: AudioUnit) -> OSStatus;
    fn AudioUnitUninitialize(unit: AudioUnit) -> OSStatus;
    fn AudioUnitGetPropertyInfo(
        unit: AudioUnit,
        id: u32,
        scope: u32,
        element: u32,
        size: *mut u32,
        writable: *mut u8,
    ) -> OSStatus;
    fn AudioUnitGetProperty(
        unit: AudioUnit,
        id: u32,
        scope: u32,
        element: u32,
        data: *mut c_void,
        size: *mut u32,
    ) -> OSStatus;
    fn AudioUnitSetProperty(
        unit: AudioUnit,
        id: u32,
        scope: u32,
        element: u32,
        data: *const c_void,
        size: u32,
    ) -> OSStatus;
    fn AudioUnitGetParameter(
        unit: AudioUnit,
        id: u32,
        scope: u32,
        element: u32,
        value: *mut f32,
    ) -> OSStatus;
    fn AudioUnitSetParameter(
        unit: AudioUnit,
        id: u32,
        scope: u32,
        element: u32,
        value: f32,
        offset: u32,
    ) -> OSStatus;
    fn AudioUnitRender(
        unit: AudioUnit,
        action_flags: *mut u32,
        time_stamp: *const AudioTimeStamp,
        bus: u32,
        frames: u32,
        data: *mut AudioBufferList,
    ) -> OSStatus;
    fn AudioUnitReset(unit: AudioUnit, scope: u32, element: u32) -> OSStatus;
    fn AudioUnitAddPropertyListener(
        unit: AudioUnit,
        id: u32,
        listener: PropertyListener,
        user: *mut c_void,
    ) -> OSStatus;
    fn AudioUnitRemovePropertyListenerWithUserData(
        unit: AudioUnit,
        id: u32,
        listener: PropertyListener,
        user: *mut c_void,
    ) -> OSStatus;
}

#[link(name = "CoreFoundation", kind = "framework")]
unsafe extern "C" {
    fn CFRelease(cf: CFTypeRef);
    fn CFGetTypeID(cf: CFTypeRef) -> usize;
    fn CFStringGetTypeID() -> usize;
    fn CFDictionaryGetTypeID() -> usize;
    fn CFArrayGetTypeID() -> usize;
    fn CFStringGetLength(s: CFTypeRef) -> CFIndex;
    fn CFStringGetMaximumSizeForEncoding(length: CFIndex, encoding: u32) -> CFIndex;
    fn CFStringGetCString(s: CFTypeRef, buffer: *mut c_char, size: CFIndex, encoding: u32) -> u8;
    fn CFArrayGetCount(a: CFTypeRef) -> CFIndex;
    fn CFArrayGetValueAtIndex(a: CFTypeRef, i: CFIndex) -> CFTypeRef;
    fn CFDataCreate(alloc: CFTypeRef, bytes: *const u8, length: CFIndex) -> CFTypeRef;
    fn CFDataGetLength(d: CFTypeRef) -> CFIndex;
    fn CFDataGetBytePtr(d: CFTypeRef) -> *const u8;
    fn CFPropertyListCreateData(
        alloc: CFTypeRef,
        plist: CFTypeRef,
        format: CFIndex,
        options: usize,
        error: *mut CFTypeRef,
    ) -> CFTypeRef;
    fn CFPropertyListCreateWithData(
        alloc: CFTypeRef,
        data: CFTypeRef,
        options: usize,
        format: *mut CFIndex,
        error: *mut CFTypeRef,
    ) -> CFTypeRef;
}

/// Releases a CF object we own (null is ignored).
fn release(cf: CFTypeRef) {
    if !cf.is_null() {
        // SAFETY: `cf` is a non-null CF object the caller owns a +1 reference to.
        unsafe { CFRelease(cf) }
    }
}

/// Converts a borrowed CFString to a Rust string (empty on any failure or a non-string).
fn cf_string(s: CFTypeRef) -> String {
    if s.is_null() {
        return String::new();
    }
    // SAFETY: `s` is a live, non-null CF object (borrowed from the caller); the type check
    // guards the CFString calls, and the buffer is sized by CoreFoundation's own maximum.
    unsafe {
        if CFGetTypeID(s) != CFStringGetTypeID() {
            return String::new();
        }
        let len = CFStringGetLength(s).clamp(0, MAX_STRING);
        let cap = CFStringGetMaximumSizeForEncoding(len, CF_STRING_ENCODING_UTF8);
        if cap <= 0 {
            return String::new();
        }
        let cap = cap.saturating_add(1).min(MAX_STRING * 4 + 1);
        let mut buf = vec![0 as c_char; cap as usize];
        if CFStringGetCString(s, buf.as_mut_ptr(), cap, CF_STRING_ENCODING_UTF8) == 0 {
            return String::new();
        }
        CStr::from_ptr(buf.as_ptr()).to_string_lossy().into_owned()
    }
}

/// One registered component.
#[derive(Debug, Clone)]
pub struct RawComponent {
    pub ty: u32,
    pub subtype: u32,
    pub manufacturer: u32,
    /// `"Vendor: Name"` as registered.
    pub name: String,
    pub version: u32,
    pub is_v3: bool,
}

/// Every registered component of type `ty`, including asynchronous extensions.
pub fn components(ty: u32) -> Vec<RawComponent> {
    let desc = AudioComponentDescription {
        component_type: ty,
        ..Default::default()
    };
    let mut out = Vec::new();
    let mut comp: AudioComponent = ptr::null_mut();
    for _ in 0..MAX_COMPONENTS {
        // SAFETY: `desc` is a valid description; `comp` is null or a component returned by the
        // previous call (components live for the process).
        comp = unsafe { AudioComponentFindNext(comp, &desc) };
        if comp.is_null() {
            break;
        }
        let mut d = AudioComponentDescription::default();
        let mut name: CFTypeRef = ptr::null();
        let mut version = 0u32;
        // SAFETY: `comp` is a live component; the out pointers point at locals of the right types.
        let (ds, ns) = unsafe {
            let ds = AudioComponentGetDescription(comp, &mut d);
            let ns = AudioComponentCopyName(comp, &mut name);
            let _ = AudioComponentGetVersion(comp, &mut version);
            (ds, ns)
        };
        let full = if ns == 0 {
            cf_string(name)
        } else {
            String::new()
        };
        release(name);
        if ds != 0 || d.component_type != ty {
            continue;
        }
        out.push(RawComponent {
            ty,
            subtype: d.component_sub_type,
            manufacturer: d.component_manufacturer,
            name: full,
            version,
            is_v3: d.component_flags & K_AUDIO_COMPONENT_FLAG_IS_V3 != 0,
        });
    }
    out
}

/// One global-scope parameter as the unit describes it.
#[derive(Debug, Clone)]
pub struct RawParam {
    pub id: u32,
    pub name: String,
    pub unit: u32,
    pub custom_unit: String,
    pub min: f32,
    pub max: f32,
    pub default: f32,
    pub flags: u32,
    /// Names of the values of an indexed parameter (when it has them).
    pub value_strings: Vec<String>,
}

/// The state the input render callback reads: one buffer per channel, filled by the host before
/// each render.
struct InputState {
    bufs: Vec<Vec<f32>>,
    frames: usize,
    borrowed: [InputBuffer; MAX_AU_CHANNELS],
    bus: u32,
    silent: bool,
}

#[derive(Clone, Copy)]
struct InputBuffer {
    data: *mut f32,
    len: usize,
}
impl InputBuffer {
    const EMPTY: Self = Self {
        data: ptr::null_mut(),
        len: 0,
    };
}

impl InputState {
    fn refresh_pointers(&mut self) {
        self.borrowed.fill(InputBuffer::EMPTY);
        for (raw, buf) in self.borrowed.iter_mut().zip(&mut self.bufs) {
            *raw = InputBuffer {
                data: buf.as_mut_ptr(),
                len: buf.len(),
            };
        }
    }
}

/// Supplies the effect's input: copies (or lends) our input buffers into the unit's list.
unsafe extern "C" fn input_callback(
    ref_con: *mut c_void,
    _action_flags: *mut u32,
    _time_stamp: *const AudioTimeStamp,
    bus: u32,
    frames: u32,
    data: *mut AudioBufferListHeader,
) -> OSStatus {
    if ref_con.is_null() || data.is_null() {
        return -50; // paramErr
    }
    // SAFETY: `ref_con` is the `InputState` leaked by `Instance::new`, alive until after the unit
    // is disposed, and only read here while the host is inside `AudioUnitRender` (it does not
    // touch it concurrently). `data` is the unit's buffer list with `number_buffers` buffers laid
    // out contiguously from `first`.
    unsafe {
        let input = &*(ref_con as *const InputState);
        let n = (*data).number_buffers as usize;
        if bus != input.bus || frames as usize > input.frames || n != input.bufs.len() || n > MAX_AU_CHANNELS {
            return -50;
        }
        let first: *mut AudioBuffer = ptr::addr_of_mut!((*data).first);
        let frames = (frames as usize).min(MAX_SLICE);
        for i in 0..n {
            let b = &mut *first.add(i);
            if b.number_channels != 1 {
                return -50;
            }
            let Some(src) = input.borrowed.get(i) else {
                return -50;
            };
            if src.data.is_null() || src.len < frames {
                return -50;
            }
            if b.data.is_null() {
                if input.silent { ptr::write_bytes(src.data, 0, frames); }
                // Lend our buffer (the unit may process in place; it is ours to scribble on).
                b.data = src.data.cast();
                b.data_byte_size = (frames * 4) as u32;
                b.number_channels = 1;
                continue;
            }
            if !(b.data as usize).is_multiple_of(std::mem::align_of::<f32>())
                || (b.data_byte_size as usize) < frames * 4
            {
                return -50;
            }
            // A unit can retain the pointer lent during an earlier callback in this render.
            // Raw copy permits overlap without creating aliased mutable/shared slices.
            if input.silent {
                ptr::write_bytes(b.data.cast::<f32>(), 0, frames);
            } else {
                ptr::copy(src.data, b.data.cast::<f32>(), frames);
            }
        }
    }
    0
}

/// A live Audio Unit instance.
pub struct Instance {
    unit: AudioUnit,
    /// Leaked `Box<InputState>` read by `input_callback`; freed in `Drop` after disposal.
    input: *mut InputState,
    /// Stable callback contexts, retired alongside the AU before being freed.
    auxiliary_inputs: Vec<*mut InputState>,
    /// Leaked `Box<HostTime>` read by the host callbacks; freed in `Drop` after disposal.
    time: *mut HostTime,
    changes: *mut PropertyChanges,
    latency_listener: bool,
    tail_listener: bool,
    outputs: Vec<Vec<f32>>,
    abl: AudioBufferList,
    has_input: bool,
    initialized: bool,
    layout: crate::channels::ChannelLayout,
    max_frames: usize,
    sample_rate: f64,
    sample_time: f64,
    link: std::sync::Arc<StateLink>,
}

// SAFETY: an Audio Unit instance may be used from any thread as long as calls are not
// concurrent; `Instance` is only reachable through `&mut`/`&` of its owner, which the `Plugin`
// contract keeps on one thread at a time. The input state and host time are owned exclusively by
// it (the host time is all atomics).
unsafe impl Send for Instance {}

impl Instance {
    /// Creates (but does not initialize) the component `(ty, subtype, manufacturer)`.
    pub fn new(
        ty: u32,
        subtype: u32,
        manufacturer: u32,
        has_input: bool,
        is_v3: bool,
    ) -> Result<Instance, String> {
        let desc = AudioComponentDescription {
            component_type: ty,
            component_sub_type: subtype,
            component_manufacturer: manufacturer,
            component_flags: if is_v3 {
                K_AUDIO_COMPONENT_FLAG_IS_V3
            } else {
                0
            },
            component_flags_mask: K_AUDIO_COMPONENT_FLAG_IS_V3,
            ..Default::default()
        };
        // SAFETY: `desc` is a valid description.
        let mut comp = ptr::null_mut();
        let mut d = AudioComponentDescription::default();
        for _ in 0..MAX_COMPONENTS {
            comp = unsafe { AudioComponentFindNext(comp, &desc) };
            if comp.is_null() {
                break;
            }
            let status = unsafe { AudioComponentGetDescription(comp, &mut d) };
            if status == 0 && (d.component_flags & K_AUDIO_COMPONENT_FLAG_IS_V3 != 0) == is_v3 {
                break;
            }
        }
        if comp.is_null() {
            return Err("component not registered".into());
        }
        // SAFETY: `comp` is a live component; `d` is a local of the right type.
        let status = unsafe { AudioComponentGetDescription(comp, &mut d) };
        if status != 0 {
            return Err(format!("AudioComponentGetDescription failed ({status})"));
        }
        if (d.component_flags & K_AUDIO_COMPONENT_FLAG_IS_V3 != 0) != is_v3 {
            return Err("Matching Audio Unit variant is not registered".into());
        }
        if d.component_type != ty
            || d.component_sub_type != subtype
            || d.component_manufacturer != manufacturer
        {
            return Err("Matching Audio Unit component is not registered".into());
        }
        eprintln!("[audio.au] instantiate component={ty:08X}:{subtype:08X}:{manufacturer:08X} flags=0x{:08X}", d.component_flags);
        let unit = if let Some(options) = crate::attempts::async_options(d.component_flags) {
            async_au::instantiate(comp, options)?
        } else {
            let mut unit = ptr::null_mut();
            let status = unsafe { AudioComponentInstanceNew(comp, &mut unit) };
            if status != 0 || unit.is_null() {
                async_au::retire_uninitialized_unit(unit);
                return Err(format!("AudioComponentInstanceNew failed ({status})"));
            }
            unit
        };
        eprintln!("[audio.au] instantiated component={ty:08X}:{subtype:08X}:{manufacturer:08X}");
        let input = Box::into_raw(Box::new(InputState {
            bufs: Vec::new(),
            frames: 0,
            borrowed: [InputBuffer::EMPTY; MAX_AU_CHANNELS],
            bus: 0,
            silent: false,
        }));
        let time = Box::into_raw(Box::new(HostTime::new()));
        let empty = AudioBuffer {
            number_channels: 1,
            data_byte_size: 0,
            data: ptr::null_mut(),
        };
        let mut inst = Instance {
            unit,
            input,
            auxiliary_inputs: Vec::new(),
            time,
            changes: Box::into_raw(Box::new(PropertyChanges {
                dirty: AtomicBool::new(true),
            })),
            latency_listener: false,
            tail_listener: false,
            outputs: Vec::new(),
            abl: AudioBufferList {
                number_buffers: 0,
                buffers: [empty; MAX_AU_CHANNELS],
            },
            has_input,
            link: Arc::new(StateLink::new(unit, is_v3)),
            initialized: false,
            layout: crate::channels::ChannelLayout {
                inputs: 0,
                outputs: 0,
            },
            max_frames: 0,
            sample_rate: 48_000.0,
            sample_time: 0.0,
        };
        let callbacks = HostCallbackInfo {
            host_user_data: time.cast(),
            beat_and_tempo: Some(beat_and_tempo),
            musical_time_location: Some(musical_time),
            transport_state: Some(transport_state),
            transport_state2: Some(transport_state2),
        };
        // Units that never ask for host time may refuse it.
        let _ = inst.set_property(PROP_HOST_CALLBACKS, SCOPE_GLOBAL, 0, &callbacks);
        // SAFETY: the allocation is owned by Instance until listeners are removed in Drop.
        let status = unsafe {
            AudioUnitAddPropertyListener(unit, PROP_LATENCY, property_changed, inst.changes.cast())
        };
        if status != 0 {
            return Err(format!("cannot observe Audio Unit latency ({status})"));
        }
        inst.latency_listener = true;
        // A tail-only change also requires the graph to update export duration.
        let status = unsafe {
            AudioUnitAddPropertyListener(
                unit,
                PROP_TAIL_TIME,
                property_changed,
                inst.changes.cast(),
            )
        };
        if status != 0 {
            return Err(format!("cannot observe Audio Unit tail time ({status})"));
        }
        inst.tail_listener = true;
        if is_v3 {
            inst.validate_parameter_bridge()?;
        }
        Ok(inst)
    }

    fn validate_parameter_bridge(&self) -> Result<(), String> {
        // AUv3 addresses are 64-bit. The documented C compatibility bridge publishes its own
        // AudioUnitParameterID list; only those IDs enter this host. Never truncate an address.
        let mut size = 0_u32;
        let mut writable = 0;
        let status = unsafe {
            AudioUnitGetPropertyInfo(
                self.unit,
                PROP_PARAMETER_LIST,
                SCOPE_GLOBAL,
                0,
                &mut size,
                &mut writable,
            )
        };
        if status != 0
            || size as usize % std::mem::size_of::<u32>() != 0
            || size as usize / 4 > MAX_PARAMS
        {
            return Err(format!(
                "Audio Unit extension does not expose a supported C parameter bridge ({status})"
            ));
        }
        if size == 0 {
            return Ok(());
        }
        let mut ids = vec![0_u32; size as usize / 4];
        let expected = size;
        let status = unsafe {
            AudioUnitGetProperty(
                self.unit,
                PROP_PARAMETER_LIST,
                SCOPE_GLOBAL,
                0,
                ids.as_mut_ptr().cast(),
                &mut size,
            )
        };
        if status != 0 || size != expected {
            return Err(format!(
                "Cannot read Audio Unit extension parameter IDs ({status})"
            ));
        }
        let mut seen = std::collections::HashSet::new();
        for id in ids {
            if !seen.insert(id) || self.param_info(id).is_none() {
                return Err("Audio Unit extension parameter IDs are not supported by the C compatibility bridge".into());
            }
        }
        Ok(())
    }

    fn time(&self) -> &HostTime {
        // SAFETY: `time` is the `HostTime` leaked in `new`, freed only in `Drop`.
        unsafe { &*self.time }
    }

    pub fn latency_dirty(&self) -> bool {
        // SAFETY: the flag allocation remains alive for this instance and its listeners.
        unsafe { &*self.changes }.dirty.load(Ordering::Acquire)
    }

    pub fn clear_latency_dirty(&self) {
        // SAFETY: see latency_dirty. Called while the host's processing worker is stopped.
        unsafe { &*self.changes }
            .dirty
            .store(false, Ordering::Release);
    }

    /// The transport the host callbacks report during the next renders.
    pub fn set_transport(&mut self, t: &Transport) {
        if (self.sample_time - t.samples as f64).abs() > 1.0 {
            self.time().changed.store(true, Ordering::Relaxed);
        }
        self.sample_time = t.samples as f64;
        self.time().set(t);
    }

    fn set_property<T>(&self, id: u32, scope: u32, element: u32, value: &T) -> OSStatus {
        // SAFETY: `value` points at a live `T` of `size_of::<T>()` bytes, matching what the
        // property expects (callers pass the documented type).
        unsafe {
            AudioUnitSetProperty(
                self.unit,
                id,
                scope,
                element,
                (value as *const T).cast(),
                std::mem::size_of::<T>() as u32,
            )
        }
    }

    fn get_f64(&self, id: u32) -> Option<f64> {
        let mut v = 0f64;
        let mut size = std::mem::size_of::<f64>() as u32;
        // SAFETY: the property is a `Float64`; `v`/`size` are locals of the right types.
        let st = unsafe {
            AudioUnitGetProperty(
                self.unit,
                id,
                SCOPE_GLOBAL,
                0,
                (&mut v as *mut f64).cast(),
                &mut size,
            )
        };
        if st != 0 {
            None
        } else if size as usize != std::mem::size_of::<f64>() {
            Some(f64::NAN)
        } else {
            Some(v)
        }
    }

    fn format(&self, scope: u32, bus: u32, sample_rate: f64, channels: usize) -> OSStatus {
        let asbd = AudioStreamBasicDescription {
            sample_rate,
            format_id: FORMAT_LINEAR_PCM,
            format_flags: FORMAT_FLAG_FLOAT | FORMAT_FLAG_PACKED | FORMAT_FLAG_NON_INTERLEAVED,
            bytes_per_packet: 4,
            frames_per_packet: 1,
            bytes_per_frame: 4,
            channels_per_frame: channels as u32,
            bits_per_channel: 32,
            reserved: 0,
        };
        self.set_property(PROP_STREAM_FORMAT, scope, bus, &asbd)
    }

    fn reduce_buses(&self, scope: u32, count: u32) -> Result<u32, String> {
        let mut size = 0;
        let mut writable = 0_u8;
        // Only units advertising a writable ElementCount may have their topology reduced.
        let status = unsafe { AudioUnitGetPropertyInfo(self.unit, PROP_ELEMENT_COUNT, scope, 0, &mut size, &mut writable) };
        if status == 0 && writable != 0 && size == std::mem::size_of::<u32>() as u32 {
            let _ = self.set_property(PROP_ELEMENT_COUNT, scope, 0, &1_u32);
            return self.bus_count(scope);
        }
        Ok(count)
    }

    fn auxiliary_format(&self, scope: u32, bus: u32, rate: f64, preferred: usize) -> Result<usize, OSStatus> {
        let mut last = -10868;
        for channels in [preferred, 3 - preferred] {
            last = self.format(scope, bus, rate, channels);
            if last == 0 { return Ok(channels); }
        }
        Err(last)
    }

    fn configure_auxiliary_buses(&mut self, counts: (u32, u32), rate: f64, frames: usize, layout: crate::channels::ChannelLayout) -> Result<(), OSStatus> {
        for bus in 1..counts.0 {
            let channels = self.auxiliary_format(SCOPE_INPUT, bus, rate, layout.inputs)?;
            while self.auxiliary_inputs.len() < bus as usize {
                let index = self.auxiliary_inputs.len() as u32 + 1;
                self.auxiliary_inputs.push(Box::into_raw(Box::new(InputState {
                    bufs: Vec::new(), frames: 0, borrowed: [InputBuffer::EMPTY; MAX_AU_CHANNELS], bus: index, silent: true,
                })));
            }
            let pointer = self.auxiliary_inputs[bus as usize - 1];
            // DSP is stopped and all callback contexts retain stable addresses until disposal.
            let input = unsafe { &mut *pointer };
            input.bufs = (0..channels).map(|_| vec![0.0; frames]).collect();
            input.frames = frames;
            input.refresh_pointers();
            let callback = AuRenderCallbackStruct { input_proc: Some(input_callback), input_proc_ref_con: pointer.cast() };
            let status = self.set_property(PROP_SET_RENDER_CALLBACK, SCOPE_INPUT, bus, &callback);
            if status != 0 { return Err(status); }
        }
        // Auxiliary outputs remain unconnected. The host pulls only the main output bus.
        for bus in 1..counts.1 {
            self.auxiliary_format(SCOPE_OUTPUT, bus, rate, layout.outputs)?;
        }
        Ok(())
    }

    fn bus_count(&self, scope: u32) -> Result<u32, String> {
        let mut count = 0_u32;
        let mut size = std::mem::size_of::<u32>() as u32;
        // SAFETY: ElementCount is a UInt32 on the requested input/output scope.
        let status = unsafe {
            AudioUnitGetProperty(
                self.unit,
                PROP_ELEMENT_COUNT,
                scope,
                0,
                ptr::from_mut(&mut count).cast(),
                &mut size,
            )
        };
        if status != 0 || size != std::mem::size_of::<u32>() as u32 {
            return Err(format!("cannot read Audio Unit bus count ({status})"));
        }
        Ok(count)
    }

    /// (Re)configures and initializes the unit for `sample_rate`, `max_frames`-frame slices and
    /// (preferably) `channels` channels on its main input and output buses. Input/output widths
    /// are negotiated independently. Not realtime-safe (allocates the buffers).
    pub fn configure(
        &mut self,
        sample_rate: f64,
        max_frames: usize,
        channels: usize,
    ) -> Result<crate::channels::ChannelLayout, String> {
        if !(1..=2).contains(&channels) {
            return Err("invalid Audio Unit configuration".into());
        }
        let layouts = crate::channels::candidates(channels, self.has_input);
        self.configure_layouts(sample_rate, max_frames, &layouts)
    }

    fn configure_layouts(
        &mut self,
        sample_rate: f64,
        max_frames: usize,
        layouts: &[crate::channels::ChannelLayout],
    ) -> Result<crate::channels::ChannelLayout, String> {
        if self.initialized {
            // SAFETY: the unit is live and initialized.
            let status = unsafe { AudioUnitUninitialize(self.unit) };
            if status != 0 {
                return Err(format!("AudioUnitUninitialize failed ({status})"));
            }
            self.initialized = false;
        }
        self.max_frames = 0;
        if !sample_rate.is_finite()
            || !(8_000.0..=384_000.0).contains(&sample_rate)
            || max_frames == 0
            || max_frames > MAX_SLICE
            || layouts.is_empty()
            || layouts.iter().any(|layout| {
                !(1..=2).contains(&layout.outputs)
                    || if self.has_input {
                        !(1..=2).contains(&layout.inputs)
                    } else {
                        layout.inputs != 0
                    }
            })
        {
            return Err("invalid Audio Unit configuration".into());
        }
        let mf = max_frames;
        eprintln!("[audio.au] configure sampleRate={sample_rate} maxFrames={max_frames}");
        let counts = crate::channels::main_buses(
            self.bus_count(SCOPE_INPUT)?, self.bus_count(SCOPE_OUTPUT)?, self.has_input,
            |scope, count| self.reduce_buses(match scope { crate::channels::BusScope::Input => SCOPE_INPUT, crate::channels::BusScope::Output => SCOPE_OUTPUT }, count),
        )?;
        let status = self.set_property(PROP_MAX_FRAMES, SCOPE_GLOBAL, 0, &(mf as u32));
        if status != 0 {
            return Err(format!("cannot set the maximum render slice ({status})"));
        }
        if self.has_input {
            let cb = AuRenderCallbackStruct {
                input_proc: Some(input_callback),
                input_proc_ref_con: self.input.cast(),
            };
            let st = self.set_property(PROP_SET_RENDER_CALLBACK, SCOPE_INPUT, 0, &cb);
            if st != 0 {
                return Err(format!("cannot set the input callback ({st})"));
            }
        }
        let layout = crate::channels::negotiate(layouts, |layout| {
            let o = self.format(SCOPE_OUTPUT, 0, sample_rate, layout.outputs);
            let i = if self.has_input {
                self.format(SCOPE_INPUT, 0, sample_rate, layout.inputs)
            } else {
                0
            };
            if o != 0 || i != 0 {
                return Err(if o != 0 { o } else { i });
            }
            self.configure_auxiliary_buses(counts, sample_rate, mf, layout)?;
            let input = unsafe { &mut *self.input };
            input.bufs = (0..layout.inputs).map(|_| vec![0.0; mf]).collect();
            input.frames = mf;
            input.refresh_pointers();
            // SAFETY: the unit is live and uninitialized.
            let st = unsafe { AudioUnitInitialize(self.unit) };
            if st == 0 {
                Ok(())
            } else {
                let _ = unsafe { AudioUnitUninitialize(self.unit) };
                Err(st)
            }
        }).map_err(|status| format!("the unit accepts none of the requested mono/stereo input/output float formats ({status})"))?;
        self.initialized = true;
        self.layout = layout;
        self.sample_rate = sample_rate;
        self.time()
            .sample_rate
            .store(sample_rate.to_bits(), Ordering::Relaxed);
        self.outputs = (0..layout.outputs).map(|_| vec![0.0; mf]).collect();
        // SAFETY: the callback only runs inside `AudioUnitRender`, which is not running now
        // (we hold `&mut self`), so this exclusive access does not alias.
        let input = unsafe { &mut *self.input };
        input.frames = 0;
        self.max_frames = mf;
        eprintln!("[audio.au] configured inputs={} outputs={} mainChannels={}/{}", counts.0, counts.1, layout.inputs, layout.outputs);
        Ok(layout)
    }

    pub fn is_ready(&self) -> bool {
        self.initialized && self.max_frames > 0
    }

    pub fn max_frames(&self) -> usize {
        self.max_frames
    }

    /// Input buffer `ch` (for effects), `max_frames` long.
    pub fn input_mut(&mut self, ch: usize) -> Option<&mut [f32]> {
        // SAFETY: see `configure`: no render is running while we hold `&mut self`.
        let input = unsafe { &mut *self.input };
        input.bufs.get_mut(ch).map(Vec::as_mut_slice)
    }

    /// Renders `frames` (≤ `max_frames`) into the output buffers. Never allocates.
    pub fn render(&mut self, frames: usize) -> Result<(), OSStatus> {
        if !self.is_ready() || frames == 0 || frames > self.max_frames {
            return Err(-50);
        }
        {
            // SAFETY: as in `input_mut`.
            let input = unsafe { &mut *self.input };
            input.frames = frames;
            // Refresh after all host-side mutable slice borrows have ended. The callback
            // accesses only this raw pointer table throughout AudioUnitRender.
            input.refresh_pointers();
        }
        for &pointer in &self.auxiliary_inputs {
            // An auxiliary input is always silent; its callback rewrites every requested block.
            unsafe { (*pointer).frames = frames; }
        }
        let n = self.outputs.len().min(MAX_AU_CHANNELS);
        for (b, out) in self.abl.buffers.iter_mut().zip(self.outputs.iter_mut()) {
            *b = AudioBuffer {
                number_channels: 1,
                data_byte_size: (frames * 4) as u32,
                data: out.as_mut_ptr().cast(),
            };
        }
        self.abl.number_buffers = n as u32;
        let ts = AudioTimeStamp {
            sample_time: self.sample_time,
            flags: TIMESTAMP_SAMPLE_TIME_VALID,
            ..Default::default()
        };
        let mut flags = 0u32;
        // SAFETY: the unit is initialized; `ts` and `flags` are locals; `abl` holds `n` buffers
        // each pointing at `frames` floats we own; the input callback's state is alive.
        let st =
            unsafe { AudioUnitRender(self.unit, &mut flags, &ts, 0, frames as u32, &mut self.abl) };
        self.sample_time += frames as f64;
        if st == 0 { Ok(()) } else { Err(st) }
    }

    /// Output channel `ch` of the last render (`frames` long), wherever the unit left it.
    pub fn output(&self, ch: usize, frames: usize) -> Option<&[f32]> {
        if self.abl.number_buffers as usize != self.layout.outputs || ch >= self.layout.outputs {
            return None;
        }
        let b = self.abl.buffers.get(ch)?;
        if b.data.is_null()
            || b.number_channels != 1
            || !(b.data as usize).is_multiple_of(std::mem::align_of::<f32>())
        {
            return None;
        }
        let len = (b.data_byte_size as usize / 4).min(frames);
        // SAFETY: after a successful render each buffer points at `data_byte_size` bytes of
        // floats, either ours or the unit's own (valid until its next render).
        Some(unsafe { std::slice::from_raw_parts(b.data as *const f32, len) })
    }

    pub fn channel_layout(&self) -> crate::channels::ChannelLayout {
        self.layout
    }

    pub fn reset(&mut self) -> Result<(), OSStatus> {
        // SAFETY: the unit is live.
        let status = unsafe { AudioUnitReset(self.unit, SCOPE_GLOBAL, 0) };
        self.sample_time = 0.0;
        if status == 0 { Ok(()) } else { Err(status) }
    }

    /// Latency in samples at the configured rate.
    pub fn latency(&self) -> usize {
        crate::timing::seconds_to_samples(self.get_f64(PROP_LATENCY), self.sample_rate)
    }

    /// Uncapped tail in samples at the configured rate; the graph caps and reports truncation.
    pub fn tail(&self) -> usize {
        crate::timing::seconds_to_samples(self.get_f64(PROP_TAIL_TIME), self.sample_rate)
    }

    pub fn get_param(&self, id: u32) -> Option<f32> {
        let mut v = 0f32;
        // SAFETY: the unit is live; `v` is a local `f32`.
        let st = unsafe { AudioUnitGetParameter(self.unit, id, SCOPE_GLOBAL, 0, &mut v) };
        (st == 0 && v.is_finite()).then_some(v)
    }

    pub fn set_param(&self, id: u32, value: f32) -> bool {
        // SAFETY: the unit is live; the call only takes plain values.
        let ok = unsafe { AudioUnitSetParameter(self.unit, id, SCOPE_GLOBAL, 0, value, 0) == 0 };
        if ok {
            if let Some(events) = self.link.events.get() {
                events.applied_change(id);
            }
        }
        ok
    }

    /// The global-scope parameters.
    pub fn params(&self) -> Vec<RawParam> {
        let mut size = 0u32;
        let mut writable = 0u8;
        // SAFETY: the unit is live; out pointers are locals.
        let st = unsafe {
            AudioUnitGetPropertyInfo(
                self.unit,
                PROP_PARAMETER_LIST,
                SCOPE_GLOBAL,
                0,
                &mut size,
                &mut writable,
            )
        };
        if st != 0 || size == 0 {
            return Vec::new();
        }
        let n = (size as usize / 4).min(MAX_PARAMS);
        let mut ids = vec![0u32; n];
        let mut size = (n * 4) as u32;
        // SAFETY: `ids` has room for `size` bytes.
        let st = unsafe {
            AudioUnitGetProperty(
                self.unit,
                PROP_PARAMETER_LIST,
                SCOPE_GLOBAL,
                0,
                ids.as_mut_ptr().cast(),
                &mut size,
            )
        };
        if st != 0 {
            return Vec::new();
        }
        ids.truncate(size as usize / 4);
        ids.into_iter()
            .filter_map(|id| self.param_info(id))
            .collect()
    }

    fn param_info(&self, id: u32) -> Option<RawParam> {
        // SAFETY: an all-zero `AudioUnitParameterInfo` is valid (null pointers, zero numbers).
        let mut info: AudioUnitParameterInfo = unsafe { std::mem::zeroed() };
        let mut size = std::mem::size_of::<AudioUnitParameterInfo>() as u32;
        // SAFETY: `info` is a local of the documented type and size.
        let st = unsafe {
            AudioUnitGetProperty(
                self.unit,
                PROP_PARAMETER_INFO,
                SCOPE_GLOBAL,
                id,
                (&mut info as *mut AudioUnitParameterInfo).cast(),
                &mut size,
            )
        };
        if st != 0 {
            return None;
        }
        let owned = info.flags & PARAM_FLAG_CF_NAME_RELEASE != 0;
        let mut name = if info.flags & PARAM_FLAG_HAS_CF_NAME != 0 {
            cf_string(info.cf_name_string)
        } else {
            String::new()
        };
        if name.is_empty() {
            let bytes: Vec<u8> = info
                .name
                .iter()
                .take_while(|&&c| c != 0)
                .map(|&c| c as u8)
                .collect();
            name = String::from_utf8_lossy(&bytes).into_owned();
        }
        let custom_unit = if info.unit == UNIT_CUSTOM {
            cf_string(info.unit_name)
        } else {
            String::new()
        };
        if owned {
            if info.flags & PARAM_FLAG_HAS_CF_NAME != 0 {
                release(info.cf_name_string);
            }
            // CFNameRelease transfers the parameter name only; the custom unit label is borrowed.
        }
        let value_strings = if info.unit == UNIT_INDEXED {
            self.value_strings(id)
        } else {
            Vec::new()
        };
        Some(RawParam {
            id,
            name,
            unit: info.unit,
            custom_unit,
            min: info.min_value,
            max: info.max_value,
            default: info.default_value,
            flags: info.flags,
            value_strings,
        })
    }

    fn value_strings(&self, id: u32) -> Vec<String> {
        let mut arr: CFTypeRef = ptr::null();
        let mut size = std::mem::size_of::<CFTypeRef>() as u32;
        // SAFETY: the property is a `CFArrayRef` (returned +1); `arr` is a local pointer.
        let st = unsafe {
            AudioUnitGetProperty(
                self.unit,
                PROP_PARAMETER_VALUE_STRINGS,
                SCOPE_GLOBAL,
                id,
                (&mut arr as *mut CFTypeRef).cast(),
                &mut size,
            )
        };
        if st != 0 || arr.is_null() {
            return Vec::new();
        }
        // SAFETY: `arr` is a live CF object returned by the unit. Validate the type before
        // interpreting a plugin-provided property as an array.
        if unsafe { CFGetTypeID(arr) != CFArrayGetTypeID() } {
            release(arr);
            return Vec::new();
        }
        // SAFETY: `arr` is a live CFArray we own; indices are below its count.
        let out = unsafe {
            let n = CFArrayGetCount(arr).clamp(0, MAX_VALUE_STRINGS);
            (0..n)
                .map(|i| cf_string(CFArrayGetValueAtIndex(arr, i)))
                .collect()
        };
        release(arr);
        out
    }

    /// The link for state handles.
    pub fn state_link(&self) -> std::sync::Arc<StateLink> {
        std::sync::Arc::clone(&self.link)
    }

    pub fn editor(
        &self,
        info: &'static soundcraft_dsp::PluginInfo,
        values: &[f32],
    ) -> Box<dyn soundcraft_dsp::PluginEditor> {
        self.link
            .events
            .get_or_init(|| Arc::new(crate::events::ParameterEvents::new(info, values)));
        Box::new(NativeController {
            link: Arc::clone(&self.link),
            generic: crate::controller::ParameterController { info },
        })
    }

    pub fn close_editor(&self) {
        self.link.close_editor();
    }

    /// Restores a blob from [`StateLink::save_state`] (hostile input: parsed by CoreFoundation,
    /// must be a dictionary, and the unit validates it).
    pub fn load_state(&self, bytes: &[u8]) -> bool {
        if bytes.is_empty() || bytes.len() > MAX_STATE {
            return false;
        }
        // SAFETY: `bytes` is a live slice of `len` bytes (copied by CFDataCreate).
        let data = unsafe { CFDataCreate(ptr::null(), bytes.as_ptr(), bytes.len() as CFIndex) };
        if data.is_null() {
            return false;
        }
        let mut err: CFTypeRef = ptr::null();
        // SAFETY: `data` is a live CFData; the format out pointer may be null; `err` is local.
        let plist = unsafe {
            CFPropertyListCreateWithData(ptr::null(), data, 0, ptr::null_mut(), &mut err)
        };
        release(err);
        release(data);
        if plist.is_null() {
            return false;
        }
        // SAFETY: `plist` is a live CF object we own.
        let is_dict = unsafe { CFGetTypeID(plist) == CFDictionaryGetTypeID() };
        let st = if is_dict {
            self.set_property(PROP_CLASS_INFO, SCOPE_GLOBAL, 0, &plist)
        } else {
            -50
        };
        release(plist);
        st == 0
    }
}

#[derive(Clone, Copy)]
pub(crate) struct RenderConfiguration {
    sample_rate: f64,
    max_frames: usize,
    layout: crate::channels::ChannelLayout,
}

impl crate::offline::OfflineUnit for Instance {
    type Configuration = RenderConfiguration;

    fn configuration(&self) -> Option<Self::Configuration> {
        self.initialized.then_some(RenderConfiguration {
            sample_rate: self.sample_rate,
            max_frames: self.max_frames,
            layout: self.layout,
        })
    }

    fn stop(&mut self) -> Result<(), String> {
        if self.initialized {
            // SAFETY: the caller stopped processing before entering a mode transition.
            let status = unsafe { AudioUnitUninitialize(self.unit) };
            if status != 0 {
                return Err(format!(
                    "AudioUnitUninitialize failed while changing render mode ({status})"
                ));
            }
            self.initialized = false;
        }
        Ok(())
    }

    fn supports_offline(&self) -> Result<bool, String> {
        let mut size = 0;
        let mut writable = 0;
        // SAFETY: OfflineRender metadata uses UInt32 size and a Boolean writability flag.
        let status = unsafe {
            AudioUnitGetPropertyInfo(
                self.unit,
                PROP_OFFLINE_RENDER,
                SCOPE_GLOBAL,
                0,
                &mut size,
                &mut writable,
            )
        };
        crate::offline::optional_property(status, size, writable != 0)
    }

    fn write_offline(&mut self, offline: bool) -> Result<(), String> {
        let status = self.set_property(PROP_OFFLINE_RENDER, SCOPE_GLOBAL, 0, &u32::from(offline));
        if status == 0 {
            Ok(())
        } else {
            Err(format!("Audio Unit OfflineRender change failed ({status})"))
        }
    }

    fn restart(&mut self, configuration: Self::Configuration) -> Result<(), String> {
        self.configure_layouts(
            configuration.sample_rate,
            configuration.max_frames,
            &[configuration.layout],
        )?;
        Ok(())
    }

    fn invalidate(&mut self) {
        // Do not render a partly restored configuration. Disposal still owns the same unit,
        // editor link and callback allocations, even if this final cleanup attempt fails.
        if self.initialized {
            unsafe {
                AudioUnitUninitialize(self.unit);
            }
        }
        self.initialized = false;
        self.max_frames = 0;
    }
}

struct LinkedUnit {
    unit: Option<AudioUnit>,
    editor: Option<cocoa::CocoaEditor>,
    retired: Option<RetiredInstance>,
}

// SAFETY: the pointer is only used under the link's mutex, while the instance keeps the unit alive.
unsafe impl Send for LinkedUnit {}

/// The link between an instance and its state handles: the unit's state is read on the main
/// thread while the instance renders on the audio thread (hosts make property calls from other
/// threads during rendering). The instance clears the link under its lock before it disposes of
/// the unit, so a handle never reads a dead unit.
pub(crate) struct StateLink {
    inner: Mutex<LinkedUnit>,
    events: OnceLock<Arc<crate::events::ParameterEvents>>,
    can_dispose_unit: Arc<AtomicBool>,
    pending_views: Arc<AtomicUsize>,
    is_v3: bool,
}

impl StateLink {
    fn new(unit: AudioUnit, is_v3: bool) -> Self {
        Self {
            inner: Mutex::new(LinkedUnit {
                unit: Some(unit),
                editor: None,
                retired: None,
            }),
            events: OnceLock::new(),
            can_dispose_unit: Arc::new(AtomicBool::new(true)),
            pending_views: Arc::new(AtomicUsize::new(0)),
            is_v3,
        }
    }
    fn lock(&self) -> std::sync::MutexGuard<'_, LinkedUnit> {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// The unit's `ClassInfo` as a binary property list (`None` once the instance is gone).
    pub fn save_state(&self) -> Option<Vec<u8>> {
        let g = self.lock();
        let unit = g.unit?;
        let mut plist: CFTypeRef = ptr::null();
        let mut size = std::mem::size_of::<CFTypeRef>() as u32;
        // SAFETY: the unit is live while the link holds it (see the type); the property is a
        // `CFPropertyListRef` returned +1; `plist` is a local pointer.
        let st = unsafe {
            AudioUnitGetProperty(
                unit,
                PROP_CLASS_INFO,
                SCOPE_GLOBAL,
                0,
                (&mut plist as *mut CFTypeRef).cast(),
                &mut size,
            )
        };
        if st != 0 || plist.is_null() {
            release(plist);
            return None;
        }
        // SAFETY: the unit returned a live CF object. ClassInfo must be a dictionary.
        if unsafe { CFGetTypeID(plist) != CFDictionaryGetTypeID() } {
            release(plist);
            return None;
        }
        let mut err: CFTypeRef = ptr::null();
        // SAFETY: `plist` is a live property list; `err` is a local out pointer.
        let data = unsafe {
            CFPropertyListCreateData(
                ptr::null(),
                plist,
                CF_PROPERTY_LIST_BINARY_FORMAT,
                0,
                &mut err,
            )
        };
        release(err);
        release(plist);
        if data.is_null() {
            return None;
        }
        // SAFETY: `data` is a live CFData we own; its byte pointer is valid for its length.
        let out = unsafe {
            let len = CFDataGetLength(data);
            let p = CFDataGetBytePtr(data);
            if len <= 0 || len as usize > MAX_STATE || p.is_null() {
                None
            } else {
                Some(std::slice::from_raw_parts(p, len as usize).to_vec())
            }
        };
        release(data);
        out
    }

    fn close_editor(&self) {
        if !soundcraft_vst3_host::on_ui_thread() {
            return;
        }
        let mut state = self.lock();
        // A gesture may have changed the AU before its coalesced runloop notification fires.
        // Close runs with DSP stopped, so capture final values once instead of polling in idle.
        if state.editor.is_some() {
            if let (Some(unit), Some(events)) = (state.unit, self.events.get()) {
                for entry in events.entries() {
                    let mut value = 0.0;
                    let status = unsafe {
                        AudioUnitGetParameter(unit, entry.id, SCOPE_GLOBAL, 0, &mut value)
                    };
                    if status == 0 {
                        events.native_change(entry.id, value);
                    }
                }
            }
        }
        state.editor.take();
    }

    fn cleanup(&self) -> bool {
        if !soundcraft_vst3_host::on_ui_thread() {
            return false;
        }
        let mut state = self.lock();
        state.editor.take();
        if self.pending_views.load(Ordering::Acquire) != 0 {
            return false;
        }
        if let Some(retired) = state.retired.take() {
            if self.can_dispose_unit.load(Ordering::Acquire) {
                retired.dispose();
            } else {
                // A utility listener that refused disposal can still read its parameter's
                // unit internally, even though our callback is disabled. Keep that unit and
                // its callback storage alive until helper-process exit.
                std::mem::forget(retired);
            }
        }
        true
    }
}

struct NativeController {
    link: Arc<StateLink>,
    generic: crate::controller::ParameterController,
}
impl soundcraft_dsp::PluginEditor for NativeController {
    fn open(&mut self) -> Result<(), String> {
        if !soundcraft_vst3_host::on_ui_thread() {
            return Err("Audio Unit editors require the main thread".into());
        }
        pump_ui();
        {
            let state = self.link.lock();
            if let Some(editor) = &state.editor {
                if editor.is_open() {
                    editor.raise();
                    return Ok(());
                }
            }
        }
        // Reopening a user-closed window must preserve any final coalesced parameter edits.
        self.link.close_editor();
        if !self.link.can_dispose_unit.load(Ordering::Acquire) {
            return Err(
                "Audio Unit editor cleanup failed. Reload the plugin host before reopening".into(),
            );
        }
        if self.link.pending_views.load(Ordering::Acquire) != 0 {
            return Err("The previous Audio Unit view request is still pending. Wait for it to complete or reload the plugin host".into());
        }
        let mut state = self.link.lock();
        let unit = state
            .unit
            .ok_or("The Audio Unit instance has been released")?;
        let events = self
            .link
            .events
            .get()
            .ok_or("Audio Unit parameter controller is unavailable")?;
        state.editor = Some(cocoa::CocoaEditor::new(
            unit,
            self.generic.info.name,
            Arc::clone(events),
            Arc::clone(&self.link.can_dispose_unit),
            self.link.is_v3,
            Arc::clone(&self.link.pending_views),
        )?);
        Ok(())
    }
    fn close(&mut self) {
        self.link.close_editor();
    }
    fn is_open(&self) -> bool {
        soundcraft_vst3_host::on_ui_thread()
            && self
                .link
                .lock()
                .editor
                .as_ref()
                .is_some_and(cocoa::CocoaEditor::is_open)
    }
    fn idle(&mut self) -> Vec<(String, f32)> {
        if !soundcraft_vst3_host::on_ui_thread() {
            return Vec::new();
        }
        {
            let mut state = self.link.lock();
            if let Some(editor) = &mut state.editor {
                // A user-closed native window is retired by close() on the next explicit
                // host action. While hidden it has no event traffic and stays attached safely.
                if editor.is_open() {
                    editor.idle();
                }
            }
        }
        self.link
            .events
            .get()
            .map_or_else(Vec::new, |events| events.drain_native())
    }
    fn normalize(&self, id: &str, value: f32) -> Option<f64> {
        self.generic.normalize(id, value)
    }
    fn set_param(&mut self, _id: &str, _value: f32) {}
    fn value_text(&mut self, id: &str, value: f32) -> Option<String> {
        self.generic.value_text(id, value)
    }
}

struct RetiredInstance {
    unit: AudioUnit,
    input: *mut InputState,
    auxiliary_inputs: Vec<*mut InputState>,
    time: *mut HostTime,
    changes: *mut PropertyChanges,
    initialized: bool,
    latency_listener: bool,
    tail_listener: bool,
}
// Raw parts transfer exclusive teardown ownership to the main thread; never dereferenced in queue.
unsafe impl Send for RetiredInstance {}
impl RetiredInstance {
    fn dispose(self) {
        // SAFETY: called on the UI thread after the worker stopped and all views detached.
        unsafe {
            if self.latency_listener {
                AudioUnitRemovePropertyListenerWithUserData(
                    self.unit,
                    PROP_LATENCY,
                    property_changed,
                    self.changes.cast(),
                );
            }
            if self.tail_listener {
                AudioUnitRemovePropertyListenerWithUserData(
                    self.unit,
                    PROP_TAIL_TIME,
                    property_changed,
                    self.changes.cast(),
                );
            }
            if self.initialized {
                AudioUnitUninitialize(self.unit);
            }
            let status = AudioComponentInstanceDispose(self.unit);
            if status == 0 {
                drop(Box::from_raw(self.input));
                for pointer in self.auxiliary_inputs { drop(Box::from_raw(pointer)); }
                drop(Box::from_raw(self.time));
                drop(Box::from_raw(self.changes));
            } else {
                log::error!("AudioComponentInstanceDispose failed ({status})");
            }
        }
    }
}

fn retirement_queue() -> &'static Mutex<Vec<Arc<StateLink>>> {
    static QUEUE: OnceLock<Mutex<Vec<Arc<StateLink>>>> = OnceLock::new();
    QUEUE.get_or_init(|| Mutex::new(Vec::new()))
}
pub(crate) fn pump_ui() {
    if !soundcraft_vst3_host::on_ui_thread() {
        return;
    }
    async_au::pump_cleanup();
    let queue = std::mem::take(
        &mut *retirement_queue()
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner),
    );
    for link in queue {
        if !link.cleanup() {
            retirement_queue()
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .push(link);
        }
    }
}

impl Drop for Instance {
    fn drop(&mut self) {
        {
            let mut state = self.link.lock();
            state.unit = None;
            state.retired = Some(RetiredInstance {
                unit: self.unit,
                input: self.input,
                auxiliary_inputs: std::mem::take(&mut self.auxiliary_inputs),
                time: self.time,
                changes: self.changes,
                initialized: self.initialized,
                latency_listener: self.latency_listener,
                tail_listener: self.tail_listener,
            });
        }
        if soundcraft_vst3_host::on_ui_thread() {
            if !self.link.cleanup() {
                retirement_queue()
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .push(Arc::clone(&self.link));
            }
        } else {
            retirement_queue()
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .push(Arc::clone(&self.link));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn property_notifications_only_publish_a_dirty_flag() {
        let changes = PropertyChanges {
            dirty: AtomicBool::new(false),
        };
        let user = ptr::from_ref(&changes).cast_mut().cast();
        // SAFETY: user points at the live changes object, and callback ignores the unit.
        unsafe { property_changed(user, ptr::null_mut(), PROP_LATENCY, SCOPE_GLOBAL, 0) };
        assert!(changes.dirty.swap(false, Ordering::Acquire));
        // SAFETY: null user pointers are explicitly accepted without a dereference.
        unsafe { property_changed(ptr::null_mut(), ptr::null_mut(), PROP_TAIL_TIME, 0, 0) };
        assert!(!changes.dirty.load(Ordering::Relaxed));
    }

    #[test]
    fn input_callback_copies_and_rejects_invalid_bus_shapes() {
        let mut input = InputState {
            bufs: vec![vec![0.25, -0.5, 0.75, 1.0]],
            frames: 4,
            borrowed: [InputBuffer::EMPTY; MAX_AU_CHANNELS],
            bus: 0,
            silent: false,
        };
        input.refresh_pointers();
        let mut output = [0.0_f32; 4];
        let user = ptr::from_mut(&mut input).cast();
        let mut list = AudioBufferListHeader {
            number_buffers: 1,
            first: AudioBuffer {
                number_channels: 1,
                data_byte_size: 16,
                data: output.as_mut_ptr().cast(),
            },
        };
        // SAFETY: user/list/output are valid for this synchronous invocation.
        assert_eq!(
            unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 0, 4, &mut list) },
            0
        );
        assert_eq!(output, [0.25, -0.5, 0.75, 1.0]);
        // SAFETY: all pointers are valid; frame/buffer bounds are rejected before accessing them.
        assert_ne!(
            unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 0, 5, &mut list) },
            0
        );
        list.number_buffers = 2;
        // SAFETY: the invalid count is rejected without indexing beyond this single-buffer list.
        assert_ne!(
            unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 0, 4, &mut list) },
            0
        );
        list.number_buffers = 1;
        list.first.data_byte_size = 4;
        // SAFETY: the short output allocation is rejected before writing audio.
        assert_ne!(
            unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 0, 4, &mut list) },
            0
        );
    }

    #[test]
    fn input_callback_accepts_its_previously_lent_buffer() {
        let mut input = InputState {
            bufs: vec![vec![0.25, -0.5, 0.75, 1.0]],
            frames: 4,
            borrowed: [InputBuffer::EMPTY; MAX_AU_CHANNELS],
            bus: 0,
            silent: false,
        };
        input.refresh_pointers();
        let user = ptr::from_mut(&mut input).cast();
        let mut list = AudioBufferListHeader {
            number_buffers: 1,
            first: AudioBuffer {
                number_channels: 1,
                data_byte_size: 0,
                data: ptr::null_mut(),
            },
        };
        // SAFETY: null buffer data asks the callback to lend the owned input allocation.
        assert_eq!(
            unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 0, 4, &mut list) },
            0
        );
        let lent = list.first.data;
        assert!(!lent.is_null());
        assert_eq!(list.first.data_byte_size, 16);
        // SAFETY: the same live allocation may legally be supplied on a later callback.
        assert_eq!(
            unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 0, 4, &mut list) },
            0
        );
        assert_eq!(list.first.data, lent);
        // SAFETY: callback has returned and the allocation still holds four initialized floats.
        assert_eq!(
            unsafe { std::slice::from_raw_parts(lent.cast::<f32>(), 4) },
            &[0.25, -0.5, 0.75, 1.0]
        );
        // SAFETY: non-main buses are rejected before dereferencing the buffer list.
        assert_ne!(
            unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 1, 4, &mut list) },
            0
        );
    }

    #[test]
    fn auxiliary_callback_returns_silence_for_its_bus_without_retaining_in_place_writes() {
        let mut input = InputState {
            bufs: vec![vec![0.75; 4]], frames: 4,
            borrowed: [InputBuffer::EMPTY; MAX_AU_CHANNELS], bus: 1, silent: true,
        };
        input.refresh_pointers();
        let user = ptr::from_mut(&mut input).cast();
        let mut output = [0.5_f32; 4];
        let mut list = AudioBufferListHeader { number_buffers: 1, first: AudioBuffer {
            number_channels: 1, data_byte_size: 16, data: output.as_mut_ptr().cast(),
        }};
        assert_eq!(unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 1, 4, &mut list) }, 0);
        assert_eq!(output, [0.0; 4]);
        assert_ne!(unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 0, 4, &mut list) }, 0);
        assert_ne!(unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 1, 5, &mut list) }, 0);
        list.first.data = ptr::null_mut();
        assert_eq!(unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 1, 4, &mut list) }, 0);
        assert_eq!(input.bufs[0], [0.0; 4]);
        input.bufs[0].fill(0.9);
        assert_eq!(unsafe { input_callback(user, ptr::null_mut(), ptr::null(), 1, 4, &mut list) }, 0);
        assert_eq!(input.bufs[0], [0.0; 4]);
    }

    #[test]
    fn hostile_transport_values_are_sanitized() {
        let time = HostTime::new();
        time.set(&Transport {
            tempo: f64::NAN,
            quarters: f64::INFINITY,
            bar_start: f64::NAN,
            numerator: 0,
            denominator: 3,
            ..Transport::default()
        });
        assert_eq!(load_f64(&time.tempo), 120.0);
        assert_eq!(load_f64(&time.quarters), 0.0);
        assert_eq!(load_f64(&time.bar_start), 0.0);
        assert_eq!(time.numerator.load(Ordering::Relaxed), 1);
        assert_eq!(time.denominator.load(Ordering::Relaxed), 4);
    }

    #[test]
    fn host_callbacks_report_the_transport() {
        let time = HostTime::new();
        time.set(&Transport {
            playing: true,
            tempo: 120.0,
            numerator: 3,
            denominator: 4,
            samples: 48_000,
            quarters: 2.25,
            ..Transport::default()
        });
        let user = ptr::from_ref(&time).cast_mut().cast::<c_void>();
        let (mut beat, mut tempo) = (0.0, 0.0);
        // SAFETY: `user` points at a live `HostTime`; the out-pointers are locals.
        assert_eq!(unsafe { beat_and_tempo(user, &mut beat, &mut tempo) }, 0);
        assert_eq!((beat, tempo), (2.25, 120.0));
        let (mut next, mut num, mut den, mut down) = (0u32, 0f32, 0u32, -1.0);
        // SAFETY: as above.
        assert_eq!(
            unsafe { musical_time(user, &mut next, &mut num, &mut den, &mut down) },
            0
        );
        assert_eq!((next, num, den, down), (18_000, 3.0, 4, 0.0));
        let (mut playing, mut recording, mut changed, mut sample) = (0u8, 1u8, 0u8, 0.0);
        let (no_flag, no_beat) = (ptr::null_mut::<u8>(), ptr::null_mut::<f64>());
        // SAFETY: as above; the unit may pass null for what it doesn't want.
        let st = unsafe {
            transport_state2(
                user,
                &mut playing,
                &mut recording,
                &mut changed,
                &mut sample,
                no_flag,
                no_beat,
                no_beat,
            )
        };
        assert_eq!(
            (st, playing, recording, changed, sample),
            (0, 1, 0, 1, 48_000.0)
        );
        // SAFETY: as above.
        unsafe {
            transport_state(
                user,
                &mut playing,
                &mut changed,
                no_beat,
                no_flag,
                no_beat,
                no_beat,
            )
        };
        assert_eq!(changed, 0, "a change is reported once");
        // SAFETY: a null user pointer is refused before anything is read.
        assert_eq!(
            unsafe { beat_and_tempo(ptr::null_mut(), &mut beat, &mut tempo) },
            -50
        );
    }

    #[test]
    fn struct_layouts_match_the_c_headers() {
        assert_eq!(std::mem::size_of::<AudioComponentDescription>(), 20);
        assert_eq!(std::mem::size_of::<AudioStreamBasicDescription>(), 40);
        assert_eq!(std::mem::size_of::<SmpteTime>(), 24);
        assert_eq!(std::mem::size_of::<AudioTimeStamp>(), 64);
        assert_eq!(std::mem::size_of::<AudioBuffer>(), 16);
        assert_eq!(std::mem::offset_of!(AudioBufferList, buffers), 8);
        assert_eq!(std::mem::offset_of!(AudioBufferListHeader, first), 8);
        assert_eq!(std::mem::size_of::<AuRenderCallbackStruct>(), 16);
        assert_eq!(std::mem::size_of::<HostCallbackInfo>(), 40);
        assert_eq!(std::mem::offset_of!(AudioUnitParameterInfo, unit_name), 56);
        assert_eq!(
            std::mem::offset_of!(AudioUnitParameterInfo, cf_name_string),
            72
        );
        assert_eq!(std::mem::offset_of!(AudioUnitParameterInfo, flags), 96);
        assert_eq!(std::mem::size_of::<AudioUnitParameterInfo>(), 104);
    }
}
