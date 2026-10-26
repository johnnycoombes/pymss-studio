//! Audio Unit (AUv2/AUv3, macOS) effect host based on SoundCraft's adapter.
//!
//! Lists the effect (`aufx`), music effect (`aumf`) and instrument (`aumu`) Audio Units
//! registered with the system's component manager and exposes each one as a
//! [`soundcraft_dsp::Plugin`]. Instantiation is limited to effects and music effects.
//! Legacy ids are `au:<type>:<subtype>:<manufacturer>`; extensions use `au3:` with the same
//! fields so a v2 and v3 component cannot collide. The three four-char codes are written out
//! (`au:aufx:dely:appl`); a code with bytes outside printable ASCII is written `#` + 8 upper-case
//! hex digits instead. Parameter ids are global-scope `AudioUnitParameterID`s as decimal strings;
//! values are the Audio Unit's own (plain) values, ranges and units, so no mapping is needed.
//! Boolean parameters become toggles, indexed ones starting at 0 become choices.
//!
//! Like `soundcraft-vst3-host`, this is an isolated `unsafe` crate (craftrules never-crash):
//! `unsafe` is denied crate-wide and allowed only in the private `ffi` module (hand-written
//! AudioToolbox / CoreFoundation declarations). The public API is safe, returns
//! `Result`/`Option` and never panics. Limits of in-process hosting: an Audio Unit that itself
//! crashes takes the process down with it. V3/async components use AudioComponentInstantiate
//! with a bounded active UI-runloop wait; late results retire on the UI thread.
//!
//! `PluginInfo` must be `&'static`: the registry leaks exactly one `PluginInfo` (with its param
//! table) per plugin *type* the first time that type is instantiated, and caches it.
//!
//! Plugin state: [`Plugin::save_state`] returns the unit's `ClassInfo` property list
//! (`kAudioUnitProperty_ClassInfo`) as a binary plist; [`Plugin::load_state`] parses it back and
//! sets it, then re-reads the parameter values.
//!
//! Host callbacks report the session's tempo, meter, position and play state.
//!
//! Cocoa `AUCocoaUIBase` editors are hosted on macOS, with AUEventListener notifications for
//! native edits and a pure generic-parameter controller. Open/close and state readers require
//! the processing worker to be stopped. Editor idle drains bounded notifications without
//! polling every AU parameter. Call [`pump_ui`] each UI tick to release retired instances.
//! AUv3 views use the documented kAudioUnitProperty_RequestViewController bridge and retain
//! their NSViewController. DSP, state and parameter notifications use Apple's AUv2 compatibility
//! bridge; native 64-bit AUParameterAddress values are never truncated into parameter IDs.
//!
//! The main bus is rendered in mono/stereo. Writable auxiliary buses are reduced when possible;
//! fixed auxiliary inputs receive silence and auxiliary outputs remain unconnected. External
//! sidechain routing, instruments/MIDI and parameters outside global scope are not hosted yet.
//! Units without a compatible custom Cocoa view retain generic parameter controls.
//! The optional OfflineRender context is supported for these ordinary effects. Dedicated `auol`
//! offline units require a separate preflight/priming workflow and remain outside this host.
//!
//! Off macOS (and on `wasm32`) the crate compiles to stubs: scans are empty and nothing can be
//! created.

#![deny(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::unimplemented,
    clippy::todo,
    clippy::unreachable
)]

#[cfg(any(target_os = "macos", test))]
mod attempts;
#[cfg(any(target_os = "macos", test))]
mod channels;
#[cfg(any(target_os = "macos", test))]
mod completion;
#[cfg(any(target_os = "macos", test))]
mod controller;
#[cfg(any(target_os = "macos", test))]
mod events;
#[cfg(target_os = "macos")]
#[allow(unsafe_code)]
mod ffi;
#[cfg(any(target_os = "macos", test))]
mod offline;
#[cfg(target_os = "macos")]
mod plugin;
#[cfg(target_os = "macos")]
mod registry;
#[cfg(any(target_os = "macos", test))]
mod timing;

#[cfg(target_os = "macos")]
pub use plugin::AuPlugin;
#[cfg(not(target_os = "macos"))]
pub use unsupported::AuPlugin;

use soundcraft_dsp::{Category, Plugin, PluginInfo};

/// Prefix of every hosted-Audio-Unit plugin id.
pub const ID_PREFIX: &str = "au:";
/// Distinguishes a v3 extension from a legacy component with the same four-char codes.
pub const V3_ID_PREFIX: &str = "au3:";

/// `aufx`: an audio effect.
pub const TYPE_EFFECT: u32 = u32::from_be_bytes(*b"aufx");
/// `aumf`: a music effect (an effect that also takes MIDI).
pub const TYPE_MUSIC_EFFECT: u32 = u32::from_be_bytes(*b"aumf");
/// `aumu`: a music device (instrument).
pub const TYPE_INSTRUMENT: u32 = u32::from_be_bytes(*b"aumu");
/// The component types SoundCraft hosts.
pub const SUPPORTED_TYPES: [u32; 3] = [TYPE_EFFECT, TYPE_MUSIC_EFFECT, TYPE_INSTRUMENT];

/// True when a component type is one SoundCraft hosts (effects, music effects, instruments).
pub fn is_supported_type(ty: u32) -> bool {
    SUPPORTED_TYPES.contains(&ty)
}

/// Writes a four-char code: its 4 characters when all are printable ASCII (`"dls "`), else `#`
/// followed by 8 upper-case hex digits.
pub fn fourcc_string(code: u32) -> String {
    let b = code.to_be_bytes();
    if b.iter().all(|c| (0x20..=0x7e).contains(c)) {
        b.iter().map(|&c| char::from(c)).collect()
    } else {
        format!("#{code:08X}")
    }
}

/// Parses one four-char code written by [`fourcc_string`].
pub fn parse_fourcc(s: &str) -> Option<u32> {
    match parse_field(s)? {
        (code, "") => Some(code),
        _ => None,
    }
}

/// Parses one code at the start of `s`, returning it and the rest.
fn parse_field(s: &str) -> Option<(u32, &str)> {
    let bytes = s.as_bytes();
    // `#XXXXXXXX`, only when followed by the end or a field separator (a printable code may
    // itself start with `#`).
    if bytes.first() == Some(&b'#')
        && let Some(hex) = s.get(1..9)
        && hex.bytes().all(|c| c.is_ascii_hexdigit())
        && matches!(bytes.get(9), None | Some(b':'))
        && let Ok(code) = u32::from_str_radix(hex, 16)
    {
        return Some((code, s.get(9..)?));
    }
    let four = bytes.get(..4)?;
    if !four.iter().all(|c| (0x20..=0x7e).contains(c)) {
        return None;
    }
    let code = u32::from_be_bytes([*four.first()?, *four.get(1)?, *four.get(2)?, *four.get(3)?]);
    Some((code, s.get(4..)?))
}

/// The SoundCraft id of a component: `au:<type>:<subtype>:<manufacturer>`.
pub fn format_id(ty: u32, subtype: u32, manufacturer: u32) -> String {
    format!(
        "{ID_PREFIX}{}:{}:{}",
        fourcc_string(ty),
        fourcc_string(subtype),
        fourcc_string(manufacturer)
    )
}

pub fn format_variant_id(ty: u32, subtype: u32, manufacturer: u32, is_v3: bool) -> String {
    if is_v3 {
        format!(
            "{V3_ID_PREFIX}{}:{}:{}",
            fourcc_string(ty),
            fourcc_string(subtype),
            fourcc_string(manufacturer)
        )
    } else {
        format_id(ty, subtype, manufacturer)
    }
}

pub fn is_v3_id(id: &str) -> bool {
    id.starts_with(V3_ID_PREFIX) && parse_id(id).is_some()
}

/// The `(type, subtype, manufacturer)` codes of an `au:…` id (`None` for other ids or a
/// component type SoundCraft does not host).
pub fn parse_id(id: &str) -> Option<(u32, u32, u32)> {
    let rest = id
        .strip_prefix(ID_PREFIX)
        .or_else(|| id.strip_prefix(V3_ID_PREFIX))?;
    let (ty, rest) = parse_field(rest)?;
    let (sub, rest) = parse_field(rest.strip_prefix(':')?)?;
    let (manu, rest) = parse_field(rest.strip_prefix(':')?)?;
    (rest.is_empty() && is_supported_type(ty)).then_some((ty, sub, manu))
}

/// The insert-menu category of a component, from its type, subtype and name.
pub fn category_for(ty: u32, subtype: u32, name: &str) -> Category {
    if ty == TYPE_INSTRUMENT {
        return Category::Instrument;
    }
    let by_sub = match &subtype.to_be_bytes() {
        b"dely" | b"sdly" | b"mdly" => Some(Category::Delay),
        b"lpas" | b"hpas" | b"bpas" | b"hshf" | b"lshf" | b"pmeq" | b"nbeq" | b"geq " => {
            Some(Category::Eq)
        }
        b"dcmp" | b"mcmp" | b"lmtr" | b"xpnd" | b"ngte" => Some(Category::Dynamics),
        b"rvb2" | b"mrev" => Some(Category::Reverb),
        b"dist" => Some(Category::Harmonic),
        b"tmpt" | b"pitc" | b"nutp" => Some(Category::PitchShift),
        _ => None,
    };
    if let Some(c) = by_sub {
        return c;
    }
    let n = name.to_ascii_lowercase();
    let has = |words: &[&str]| words.iter().any(|w| n.contains(w));
    if has(&["reverb", "verb", "room", "hall", "plate"]) {
        Category::Reverb
    } else if has(&["delay", "echo"]) {
        Category::Delay
    } else if has(&["eq", "filter", "lowpass", "highpass", "bandpass", "shelf"]) {
        Category::Eq
    } else if has(&["compress", "limit", "gate", "expand", "dynamic", "de-ess"]) {
        Category::Dynamics
    } else if has(&[
        "chorus", "flang", "phase", "tremolo", "vibrato", "rotary", "ring mod",
    ]) {
        Category::Modulation
    } else if has(&["distort", "satur", "overdrive", "fuzz", "bitcrush"]) {
        Category::Harmonic
    } else if has(&["pitch", "tune", "shift"]) {
        Category::PitchShift
    } else {
        Category::Other
    }
}

/// Splits a component name `"Vendor: Plugin"` into `(vendor, name)`.
pub fn split_component_name(full: &str) -> (String, String) {
    match full.split_once(':') {
        Some((v, n)) if !n.trim().is_empty() => (v.trim().to_string(), n.trim().to_string()),
        _ => (String::new(), full.trim().to_string()),
    }
}

/// One Audio Unit found by a scan.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct AuDescriptor {
    /// Component id: `au:<type>:<subtype>:<manufacturer>` or the `au3:` extension variant.
    pub id: String,
    pub name: String,
    pub vendor: String,
    /// `major.minor.bugfix` from the component version.
    pub version: String,
    /// The four-char codes as written in the id.
    pub component_type: String,
    pub subtype: String,
    pub manufacturer: String,
    pub category: Category,
    pub is_instrument: bool,
    /// True for an Audio Unit extension, even if a v2 component shares its four-char codes.
    pub is_v3: bool,
}

/// Why an Audio Unit operation failed.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum AuError {
    #[error("no Audio Unit `{0}`")]
    NotFound(String),
    #[error("cannot create Audio Unit `{0}`: {1}")]
    Instantiate(String, String),
    #[error("Audio Unit processing: {0}")]
    Process(String),
    #[error("Audio Units are not supported on this platform")]
    Unsupported,
}

#[cfg(target_os = "macos")]
mod api {
    use super::*;

    pub fn scan() -> Vec<AuDescriptor> {
        registry::scan()
    }
    pub fn rescan() -> Vec<AuDescriptor> {
        registry::rescan()
    }
    pub fn instantiate(id: &str) -> Result<Box<dyn Plugin>, AuError> {
        registry::instantiate(id)
    }
    pub fn plugin_info(id: &str) -> Option<&'static PluginInfo> {
        registry::plugin_info(id)
    }
}

#[cfg(not(target_os = "macos"))]
mod api {
    use super::*;

    pub fn scan() -> Vec<AuDescriptor> {
        Vec::new()
    }
    pub fn rescan() -> Vec<AuDescriptor> {
        Vec::new()
    }
    pub fn instantiate(_id: &str) -> Result<Box<dyn Plugin>, AuError> {
        Err(AuError::Unsupported)
    }
    pub fn plugin_info(_id: &str) -> Option<&'static PluginInfo> {
        None
    }
}

/// Every hostable Audio Unit registered on the system (effects, music effects, instruments).
/// Listed on first call (reads the component registry, creates no instances), then cached.
pub fn scan() -> Vec<AuDescriptor> {
    api::scan()
}

/// Forgets the cached scan (and remembered failures) and lists again.
pub fn rescan() -> Vec<AuDescriptor> {
    api::rescan()
}

/// Creates an unprepared effect by its `au:` or `au3:` component id.
/// Call `try_prepare` before processing.
pub fn instantiate(id: &str) -> Result<Box<dyn Plugin>, AuError> {
    api::instantiate(id)
}

/// Like [`instantiate`], but returns the concrete [`AuPlugin`].
pub fn instantiate_plugin(id: &str) -> Result<AuPlugin, AuError> {
    #[cfg(target_os = "macos")]
    {
        registry::instantiate_plugin(id)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = id;
        Err(AuError::Unsupported)
    }
}

/// Resolves a single registry component without loading its executable code.
pub fn scan_component(id: &str) -> Result<Vec<AuDescriptor>, AuError> {
    parse_id(id).ok_or_else(|| AuError::NotFound(id.to_owned()))?;
    #[cfg(not(target_os = "macos"))]
    {
        Err(AuError::Unsupported)
    }
    #[cfg(target_os = "macos")]
    {
        let descriptor = rescan()
            .into_iter()
            .find(|descriptor| descriptor.id == id)
            .ok_or_else(|| AuError::NotFound(id.to_owned()))?;
        Ok(vec![descriptor])
    }
}

/// Whether this build hosts native Cocoa views (generic parameter controls remain available).
pub const NATIVE_EDITOR_SUPPORTED: bool = cfg!(target_os = "macos");

/// Drains instances retired from a worker thread. Call every helper UI tick and after sessions
/// are dropped; it is a no-op on other platforms and off the main thread.
pub fn pump_ui() {
    #[cfg(target_os = "macos")]
    ffi::pump_ui();
}

/// Releases any remaining retired AU editor and component resources on the main thread.
pub fn shutdown_ui() {
    pump_ui();
}

#[cfg(not(target_os = "macos"))]
mod unsupported {
    use super::*;
    /// Unconstructible platform stub; supported factories return `AuError::Unsupported`.
    pub struct AuPlugin {
        _private: (),
    }
    impl AuPlugin {
        pub fn try_prepare(&mut self, _: f32, _: usize, _: usize) -> Result<(), String> {
            Err(AuError::Unsupported.to_string())
        }
        pub fn try_process(&mut self, _: &mut [Vec<f32>], _: usize) -> Result<(), String> {
            Err(AuError::Unsupported.to_string())
        }
        pub fn refresh_latency(&mut self) -> usize {
            0
        }
        pub fn refresh_latency_control(&mut self) -> usize {
            0
        }
        pub fn flush_pending(&mut self) -> Result<(), String> {
            Err(AuError::Unsupported.to_string())
        }
        pub fn queue_normalized(&mut self, _: &str, _: f64, _: f32) -> bool {
            false
        }
        pub fn set_offline(&mut self, _: bool) -> Result<(), String> {
            Err(AuError::Unsupported.to_string())
        }
        pub fn is_offline(&self) -> bool {
            false
        }
    }
    impl Plugin for AuPlugin {
        fn info(&self) -> &'static PluginInfo {
            static INFO: PluginInfo = PluginInfo {
                id: "au:unsupported",
                name: "Audio Unit",
                short_name: "AU",
                category: Category::Other,
                params: &[],
                is_instrument: false,
                audiosuite: false,
            };
            &INFO
        }
        fn prepare(&mut self, _: f32, _: usize, _: usize) {}
        fn reset(&mut self) {}
        fn set_param(&mut self, _: &str, _: f32) -> bool {
            false
        }
        fn param(&self, _: &str) -> Option<f32> {
            None
        }
        fn process(&mut self, io: &mut [Vec<f32>], frames: usize) {
            for ch in io {
                let n = frames.min(ch.len());
                ch[..n].fill(0.0);
            }
        }
    }
}

/// Creates a plugin by `au:…` id; `None` if unknown or it fails to load.
pub fn create(id: &str) -> Option<Box<dyn Plugin>> {
    parse_id(id)?;
    match api::instantiate(id) {
        Ok(p) => Some(p),
        Err(e) => {
            log::warn!("{e}");
            None
        }
    }
}

/// The description of an `au:…` plugin. The first call for a type instantiates it once to read
/// its parameters; the result is cached (and leaked, see the crate docs).
pub fn plugin_info(id: &str) -> Option<&'static PluginInfo> {
    api::plugin_info(id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fourccs_round_trip() {
        assert_eq!(parse_fourcc("aufx"), Some(TYPE_EFFECT));
        assert_eq!(fourcc_string(TYPE_INSTRUMENT), "aumu");
        assert_eq!(fourcc_string(u32::from_be_bytes(*b"dls ")), "dls ");
        assert_eq!(fourcc_string(0x0102_0304), "#01020304");
        assert_eq!(parse_fourcc("#01020304"), Some(0x0102_0304));
        assert_eq!(parse_fourcc("#0102030"), None);
        assert_eq!(parse_fourcc("#abc"), Some(u32::from_be_bytes(*b"#abc")));
        assert_eq!(parse_fourcc("abc"), None);
        assert_eq!(parse_fourcc("abcde"), None);
        assert_eq!(parse_fourcc("ab\u{e9}"), None);
        assert_eq!(parse_fourcc(""), None);
        for code in [
            0u32,
            0x7f7f_7f7f,
            0x2020_2020,
            u32::MAX,
            u32::from_be_bytes(*b"a:b:"),
        ] {
            assert_eq!(parse_fourcc(&fourcc_string(code)), Some(code), "{code:08X}");
        }
    }

    #[test]
    fn ids_parse_and_format() {
        let dly = (
            TYPE_EFFECT,
            u32::from_be_bytes(*b"dely"),
            u32::from_be_bytes(*b"appl"),
        );
        assert_eq!(format_id(dly.0, dly.1, dly.2), "au:aufx:dely:appl");
        assert_eq!(parse_id("au:aufx:dely:appl"), Some(dly));
        let dls = (
            TYPE_INSTRUMENT,
            u32::from_be_bytes(*b"dls "),
            u32::from_be_bytes(*b"appl"),
        );
        assert_eq!(parse_id("au:aumu:dls :appl"), Some(dls));
        // Colons inside a code are fine: fields have fixed widths.
        let odd = (TYPE_MUSIC_EFFECT, u32::from_be_bytes(*b"a:b:"), 0x0000_00FF);
        let s = format_id(odd.0, odd.1, odd.2);
        assert_eq!(s, "au:aumf:a:b::#000000FF");
        assert_eq!(parse_id(&s), Some(odd));
        for bad in [
            "au:",
            "au:aufx",
            "au:aufx:dely",
            "au:aufx:dely:",
            "au:aufx:dely:appl:",
            "au:aufx:dely:appl ",
            "au:aufxdelyappl",
            "au:aufx;dely;appl",
            "AU:aufx:dely:appl",
            "au:aufc:dely:appl",
            "au:auou:def :appl",
            "au:aufx:dé:appl",
            "vst3:0123",
            "eq_7band",
            "",
        ] {
            assert_eq!(parse_id(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn v3_and_legacy_components_with_identical_codes_have_distinct_ids() {
        let codes = (
            TYPE_EFFECT,
            u32::from_be_bytes(*b"dely"),
            u32::from_be_bytes(*b"appl"),
        );
        let legacy = format_variant_id(codes.0, codes.1, codes.2, false);
        let extension = format_variant_id(codes.0, codes.1, codes.2, true);
        assert_eq!(legacy, "au:aufx:dely:appl");
        assert_eq!(extension, "au3:aufx:dely:appl");
        assert_eq!(parse_id(&legacy), Some(codes));
        assert_eq!(parse_id(&extension), Some(codes));
        assert!(!is_v3_id(&legacy));
        assert!(is_v3_id(&extension));
        for malformed in [
            "au3:system",
            "au3:aufx:dely:appl:extra",
            "au3:aufx:dely",
            "AU3:aufx:dely:appl",
        ] {
            assert_eq!(parse_id(malformed), None);
            assert!(!is_v3_id(malformed));
        }
    }

    #[test]
    fn dedicated_offline_effects_are_not_mistaken_for_normal_effects_in_offline_context() {
        assert!(!is_supported_type(u32::from_be_bytes(*b"auol")));
        assert!(parse_id("au:auol:norm:appl").is_none());
        assert!(parse_id("au3:auol:norm:appl").is_none());
        assert!(instantiate_plugin("au:auol:norm:appl").is_err());
    }

    #[test]
    fn categories_and_names() {
        let c = |ty: &[u8; 4], sub: &[u8; 4], name: &str| {
            category_for(u32::from_be_bytes(*ty), u32::from_be_bytes(*sub), name)
        };
        assert_eq!(c(b"aumu", b"dls ", "DLSMusicDevice"), Category::Instrument);
        assert_eq!(c(b"aufx", b"dely", "AUDelay"), Category::Delay);
        assert_eq!(c(b"aufx", b"lpas", "AULowpass"), Category::Eq);
        assert_eq!(
            c(b"aufx", b"dcmp", "AUDynamicsProcessor"),
            Category::Dynamics
        );
        assert_eq!(c(b"aufx", b"mrev", "AUMatrixReverb"), Category::Reverb);
        assert_eq!(c(b"aufx", b"xxxx", "Big Plate Verb"), Category::Reverb);
        assert_eq!(c(b"aufx", b"xxxx", "Tape Echo"), Category::Delay);
        assert_eq!(c(b"aufx", b"xxxx", "Thing"), Category::Other);
        assert_eq!(
            split_component_name("Apple: AUDelay"),
            ("Apple".into(), "AUDelay".into())
        );
        assert_eq!(
            split_component_name("NoVendor"),
            (String::new(), "NoVendor".into())
        );
        assert_eq!(split_component_name("Odd:"), (String::new(), "Odd:".into()));
    }

    #[test]
    fn foreign_ids_are_rejected_without_scanning() {
        assert!(create("eq_7band").is_none());
        assert!(create("vst3:0123456789ABCDEF0123456789ABCDEF").is_none());
        assert!(plugin_info("reverb").is_none());
        assert!(plugin_info("au:aufx").is_none());
    }

    #[test]
    fn unknown_components_are_errors_not_crashes() {
        let missing = "au:aufx:zzzz:zzzz";
        assert!(create(missing).is_none());
        assert!(plugin_info(missing).is_none());
        assert!(instantiate(missing).is_err());
    }
}
