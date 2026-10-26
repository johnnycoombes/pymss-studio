//! Host-owned VST3 messages. Calls and borrowed pointers follow ivstmessage.h /
//! ivstattributes.h; getAttributes borrows, while createInstance transfers a COM ref.
#![allow(non_snake_case)]

use std::collections::HashMap;
use std::ffi::{c_char, c_void};
use std::sync::{Mutex, PoisonError};
use vst3::Steinberg::Vst::{self as sv, IAttributeListTrait, IMessageTrait};
use vst3::Steinberg::{self as sb, FUnknown};
use vst3::com_scrape_types::Unknown;
use vst3::{Class, ComPtr, ComWrapper, Interface};

const MAX_KEYS: usize = 4096;
const MAX_ID_BYTES: usize = 4096;
const MAX_VALUE_BYTES: usize = 32 << 20;
const MAX_TOTAL_BYTES: usize = 64 << 20;

enum Attribute {
    Integer(i64),
    Float(f64),
    Text(Vec<u16>),
    Binary(Vec<u8>),
}

impl Attribute {
    fn size(&self) -> usize {
        match self {
            Self::Integer(_) | Self::Float(_) => 8,
            Self::Text(v) => v.len() * 2,
            Self::Binary(v) => v.len(),
        }
    }
}

#[derive(Default)]
struct AttributesInner {
    entries: HashMap<Vec<u8>, Attribute>,
    bytes: usize,
}

#[derive(Default)]
struct Attributes(Mutex<AttributesInner>);

impl Class for Attributes {
    type Interfaces = (sv::IAttributeList,);
}

// Read no farther than the documented NUL terminator and our allocation bound.
unsafe fn terminated<T: Copy + Default + PartialEq>(
    pointer: *const T,
    limit: usize,
) -> Result<Vec<T>, sb::tresult> {
    if pointer.is_null() {
        return Err(sb::kInvalidArgument);
    }
    let mut value = Vec::new();
    for index in 0..limit {
        // SAFETY: the ABI caller supplies a NUL-terminated array of this element type.
        let next = unsafe { pointer.add(index).read() };
        value.try_reserve(1).map_err(|_| sb::kOutOfMemory)?;
        value.push(next);
        if next == T::default() {
            return Ok(value);
        }
    }
    Err(sb::kInvalidArgument)
}

unsafe fn key(id: *const c_char) -> Result<Vec<u8>, sb::tresult> {
    // SAFETY: forwarded AttrID contract. Retain bytes without lossy UTF-8 conversion.
    let mut bytes = unsafe { terminated(id.cast::<u8>(), MAX_ID_BYTES) }?;
    bytes.pop();
    Ok(bytes)
}

impl Attributes {
    fn insert(&self, key: Vec<u8>, value: Attribute) -> sb::tresult {
        let mut inner = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let previous = inner.entries.get(&key).map(Attribute::size);
        let bytes = inner.bytes - previous.unwrap_or(0) + value.size();
        if value.size() > MAX_VALUE_BYTES
            || bytes > MAX_TOTAL_BYTES
            || (previous.is_none() && inner.entries.len() >= MAX_KEYS)
        {
            return sb::kOutOfMemory;
        }
        if previous.is_none() && inner.entries.try_reserve(1).is_err() {
            return sb::kOutOfMemory;
        }
        inner.entries.insert(key, value);
        inner.bytes = bytes;
        sb::kResultOk
    }
}

impl IAttributeListTrait for Attributes {
    unsafe fn setInt(&self, id: *const c_char, value: i64) -> sb::tresult {
        // SAFETY: AttrID is supplied by the ABI caller.
        match unsafe { key(id) } {
            Ok(id) => self.insert(id, Attribute::Integer(value)),
            Err(e) => e,
        }
    }
    unsafe fn getInt(&self, id: *const c_char, value: *mut i64) -> sb::tresult {
        // SAFETY: the caller supplies a writable integer output pointer.
        let Some(output) = (unsafe { value.as_mut() }) else {
            return sb::kInvalidArgument;
        };
        // SAFETY: AttrID contract.
        let id = match unsafe { key(id) } {
            Ok(id) => id,
            Err(e) => return e,
        };
        let inner = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(Attribute::Integer(value)) = inner.entries.get(&id) {
            *output = *value;
            sb::kResultOk
        } else {
            sb::kResultFalse
        }
    }
    unsafe fn setFloat(&self, id: *const c_char, value: f64) -> sb::tresult {
        // SAFETY: AttrID contract. Generic attributes may legitimately contain NaN sentinels.
        match unsafe { key(id) } {
            Ok(id) => self.insert(id, Attribute::Float(value)),
            Err(e) => e,
        }
    }
    unsafe fn getFloat(&self, id: *const c_char, value: *mut f64) -> sb::tresult {
        // SAFETY: the caller supplies a writable floating point output pointer.
        let Some(output) = (unsafe { value.as_mut() }) else {
            return sb::kInvalidArgument;
        };
        // SAFETY: AttrID contract.
        let id = match unsafe { key(id) } {
            Ok(id) => id,
            Err(e) => return e,
        };
        let inner = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(Attribute::Float(value)) = inner.entries.get(&id) {
            *output = *value;
            sb::kResultOk
        } else {
            sb::kResultFalse
        }
    }
    unsafe fn setString(&self, id: *const c_char, string: *const u16) -> sb::tresult {
        // SAFETY: ABI pointers refer to terminated AttrID / UTF-16 arrays.
        let id = match unsafe { key(id) } {
            Ok(id) => id,
            Err(e) => return e,
        };
        let text = match unsafe { terminated(string, MAX_VALUE_BYTES / 2) } {
            Ok(v) => v,
            Err(e) => return e,
        };
        self.insert(id, Attribute::Text(text))
    }
    unsafe fn getString(
        &self,
        id: *const c_char,
        string: *mut u16,
        sizeInBytes: u32,
    ) -> sb::tresult {
        if string.is_null() && sizeInBytes != 0 {
            return sb::kInvalidArgument;
        }
        // SAFETY: AttrID contract.
        let id = match unsafe { key(id) } {
            Ok(id) => id,
            Err(e) => return e,
        };
        let inner = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let Some(Attribute::Text(value)) = inner.entries.get(&id) else {
            return sb::kResultFalse;
        };
        let count = (sizeInBytes as usize).min(value.len() * 2);
        if count != 0 {
            // SAFETY: SDK size is in BYTES, including truncated/odd buffers. Copy only that
            // declared capacity; the stored UTF-16 includes its terminating zero.
            unsafe {
                std::ptr::copy_nonoverlapping(
                    value.as_ptr().cast::<u8>(),
                    string.cast::<u8>(),
                    count,
                );
            }
        }
        sb::kResultOk
    }
    unsafe fn setBinary(
        &self,
        id: *const c_char,
        data: *const c_void,
        sizeInBytes: u32,
    ) -> sb::tresult {
        if data.is_null() && sizeInBytes != 0 {
            return sb::kInvalidArgument;
        }
        if sizeInBytes as usize > MAX_VALUE_BYTES {
            return sb::kOutOfMemory;
        }
        // SAFETY: AttrID contract.
        let id = match unsafe { key(id) } {
            Ok(id) => id,
            Err(e) => return e,
        };
        let mut bytes = Vec::new();
        if bytes.try_reserve_exact(sizeInBytes as usize).is_err() {
            return sb::kOutOfMemory;
        }
        if sizeInBytes != 0 {
            // SAFETY: the caller declares this many readable bytes; clone them into host ownership.
            bytes.extend_from_slice(unsafe {
                std::slice::from_raw_parts(data.cast::<u8>(), sizeInBytes as usize)
            });
        }
        self.insert(id, Attribute::Binary(bytes))
    }
    unsafe fn getBinary(
        &self,
        id: *const c_char,
        data: *mut *const c_void,
        sizeInBytes: *mut u32,
    ) -> sb::tresult {
        // SAFETY: both outputs must be writable ABI out-pointers.
        let (Some(output), Some(size)) =
            (unsafe { data.as_mut() }, unsafe { sizeInBytes.as_mut() })
        else {
            return sb::kInvalidArgument;
        };
        *output = std::ptr::null();
        *size = 0;
        // SAFETY: AttrID contract.
        let id = match unsafe { key(id) } {
            Ok(id) => id,
            Err(e) => return e,
        };
        let inner = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let Some(Attribute::Binary(value)) = inner.entries.get(&id) else {
            return sb::kResultFalse;
        };
        // The SDK returns a borrow valid until replacement/destruction. A Vec's heap allocation
        // stays stable when unrelated attributes are inserted or the hash table grows.
        *output = if value.is_empty() {
            std::ptr::null()
        } else {
            value.as_ptr().cast()
        };
        *size = value.len() as u32;
        sb::kResultOk
    }
}

struct Message {
    id: Mutex<Option<Vec<u8>>>,
    attributes: ComPtr<sv::IAttributeList>,
}

impl Message {
    fn new() -> Option<Self> {
        Some(Self {
            id: Mutex::new(None),
            attributes: ComWrapper::new(Attributes::default()).to_com_ptr()?,
        })
    }
}

impl Class for Message {
    type Interfaces = (sv::IMessage,);
}

impl IMessageTrait for Message {
    unsafe fn getMessageID(&self) -> *const c_char {
        self.id
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .map_or(std::ptr::null(), |id| id.as_ptr().cast())
    }
    unsafe fn setMessageID(&self, id: *const c_char) {
        if id.is_null() {
            *self.id.lock().unwrap_or_else(PoisonError::into_inner) = None;
            return;
        }
        // SAFETY: FIDString is a terminated byte array. Own a copy, not the plugin's temporary buffer.
        if let Ok(value) = unsafe { terminated(id.cast::<u8>(), MAX_ID_BYTES) } {
            *self.id.lock().unwrap_or_else(PoisonError::into_inner) = Some(value);
        }
    }
    unsafe fn getAttributes(&self) -> *mut sv::IAttributeList {
        self.attributes.as_ptr()
    }
}

pub(super) unsafe fn create_instance(
    cid: *mut sb::TUID,
    iid: *mut sb::TUID,
    obj: *mut *mut c_void,
) -> sb::tresult {
    // SAFETY: createInstance's object argument is a writable out-pointer.
    let Some(output) = (unsafe { obj.as_mut() }) else {
        return sb::kInvalidArgument;
    };
    *output = std::ptr::null_mut();
    if cid.is_null() || iid.is_null() {
        return sb::kInvalidArgument;
    }
    // SAFETY: SDK identifiers are 16-byte TUIDs. Read bytes to avoid signed-char / alignment assumptions.
    let class = unsafe { std::slice::from_raw_parts(cid.cast::<u8>(), 16) };
    let requested = unsafe { std::slice::from_raw_parts(iid.cast::<u8>(), 16) };
    let source: Option<ComPtr<FUnknown>> = if class == sv::IMessage::IID {
        if requested != sv::IMessage::IID && requested != FUnknown::IID {
            return sb::kResultFalse;
        }
        Message::new().and_then(|value| ComWrapper::new(value).to_com_ptr())
    } else if class == sv::IAttributeList::IID {
        if requested != sv::IAttributeList::IID && requested != FUnknown::IID {
            return sb::kResultFalse;
        }
        ComWrapper::new(Attributes::default()).to_com_ptr()
    } else {
        return sb::kResultFalse;
    };
    let Some(source) = source else {
        return sb::kOutOfMemory;
    };
    let mut requested_id = [0; 16];
    requested_id.copy_from_slice(requested);
    // SAFETY: a live COM interface; query_interface transfers its own retained reference.
    match unsafe { FUnknown::query_interface(source.as_ptr(), &requested_id) } {
        Some(pointer) => {
            *output = pointer;
            sb::kResultOk
        }
        None => sb::kResultFalse,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CStr;

    unsafe fn make<I: Interface>() -> ComPtr<I> {
        let mut cid = I::IID;
        let mut iid = I::IID;
        let mut raw = std::ptr::null_mut();
        // SAFETY: valid IID buffers/out-pointer; the factory transfers one reference.
        assert_eq!(
            unsafe {
                create_instance(
                    std::ptr::addr_of_mut!(cid).cast(),
                    std::ptr::addr_of_mut!(iid).cast(),
                    &mut raw,
                )
            },
            sb::kResultOk
        );
        unsafe { ComPtr::from_raw(raw.cast()) }.expect("created interface")
    }

    #[test]
    fn message_and_attributes_survive_independent_com_references() {
        // SAFETY: all calls use valid COM objects and declared buffers.
        unsafe {
            let message = make::<sv::IMessage>();
            assert!(message.getMessageID().is_null());
            let mut temporary = b"processor-message\0".to_vec();
            message.setMessageID(temporary.as_ptr().cast());
            temporary[0] = b'X';
            assert_eq!(
                CStr::from_ptr(message.getMessageID()).to_bytes(),
                b"processor-message"
            );
            let attributes = ComPtr::from_raw(message.getAttributes()).expect("attributes");
            // getAttributes borrows; explicitly retain before storing an independent ComPtr.
            sv::IAttributeList::add_ref(attributes.as_ptr());
            assert_eq!(attributes.setInt(c"number".as_ptr(), 73), sb::kResultOk);
            drop(message);
            let mut number = 0;
            assert_eq!(
                attributes.getInt(c"number".as_ptr(), &mut number),
                sb::kResultOk
            );
            assert_eq!(number, 73);
        }
    }

    #[test]
    fn factory_unknown_reference_can_be_queried_after_its_initial_owner_is_released() {
        unsafe {
            let mut cid = sv::IMessage::IID;
            let mut iid = FUnknown::IID;
            let mut raw = std::ptr::null_mut();
            assert_eq!(
                create_instance(
                    std::ptr::addr_of_mut!(cid).cast(),
                    std::ptr::addr_of_mut!(iid).cast(),
                    &mut raw
                ),
                sb::kResultOk
            );
            let base = ComPtr::<FUnknown>::from_raw(raw.cast()).expect("unknown reference");
            let message = base.cast::<sv::IMessage>().expect("message interface");
            drop(base);
            message.setMessageID(c"retained-message".as_ptr());
            assert_eq!(
                CStr::from_ptr(message.getMessageID()).to_bytes(),
                b"retained-message"
            );
            assert!(message.cast::<sv::IAttributeList>().is_none());
        }
    }

    #[test]
    fn attribute_limits_allow_replacement_without_losing_previously_stored_values() {
        let attributes = Attributes::default();
        for i in 0..MAX_KEYS {
            assert_eq!(
                attributes.insert(i.to_le_bytes().to_vec(), Attribute::Integer(i as i64)),
                sb::kResultOk
            );
        }
        assert_eq!(
            attributes.insert(b"extra-key".to_vec(), Attribute::Integer(1)),
            sb::kOutOfMemory
        );
        assert_eq!(
            attributes.insert(0usize.to_le_bytes().to_vec(), Attribute::Integer(99)),
            sb::kResultOk
        );
        let inner = attributes.0.lock().unwrap();
        assert_eq!(inner.entries.len(), MAX_KEYS);
        assert_eq!(inner.bytes, MAX_KEYS * 8);
        assert!(matches!(
            inner.entries.get(&0usize.to_le_bytes().to_vec()),
            Some(Attribute::Integer(99))
        ));
    }

    #[test]
    fn attributes_preserve_types_and_copy_binary_input() {
        unsafe {
            let attributes = make::<sv::IAttributeList>();
            assert_eq!(
                attributes.setFloat(c"value".as_ptr(), f64::NAN),
                sb::kResultOk
            );
            let mut float = 0.0;
            assert_eq!(
                attributes.getFloat(c"value".as_ptr(), &mut float),
                sb::kResultOk
            );
            assert!(float.is_nan());
            let mut integer = 99;
            assert_eq!(
                attributes.getInt(c"value".as_ptr(), &mut integer),
                sb::kResultFalse
            );
            assert_eq!(integer, 99);
            let mut input = [1u8, 2, 3];
            assert_eq!(
                attributes.setBinary(c"bytes".as_ptr(), input.as_ptr().cast(), 3),
                sb::kResultOk
            );
            input.fill(9);
            let mut data = std::ptr::null();
            let mut size = 0;
            assert_eq!(
                attributes.getBinary(c"bytes".as_ptr(), &mut data, &mut size),
                sb::kResultOk
            );
            assert_eq!(
                std::slice::from_raw_parts(data.cast::<u8>(), size as usize),
                &[1, 2, 3]
            );
            assert_eq!(attributes.setInt(c"other".as_ptr(), -7), sb::kResultOk);
            assert_eq!(std::slice::from_raw_parts(data.cast::<u8>(), 3), &[1, 2, 3]);
        }
    }

    #[test]
    fn utf16_size_is_bytes_and_never_overwrites_the_declared_capacity() {
        unsafe {
            let attributes = make::<sv::IAttributeList>();
            let mut input: Vec<u16> = "音频🎵".encode_utf16().chain([0]).collect();
            assert_eq!(
                attributes.setString(c"text".as_ptr(), input.as_ptr()),
                sb::kResultOk
            );
            input[0] = 0;
            let mut full = [0xaaaa; 8];
            assert_eq!(
                attributes.getString(c"text".as_ptr(), full.as_mut_ptr(), 16),
                sb::kResultOk
            );
            assert_eq!(
                &full[..5],
                &"音频🎵".encode_utf16().chain([0]).collect::<Vec<_>>()
            );
            let mut short = [0xaaaau16; 4];
            assert_eq!(
                attributes.getString(c"text".as_ptr(), short.as_mut_ptr(), 3),
                sb::kResultOk
            );
            assert_eq!(short[0], full[0]);
            assert_eq!(short[2], 0xaaaa);
            assert_eq!(
                attributes.getString(c"text".as_ptr(), std::ptr::null_mut(), 0),
                sb::kResultOk
            );
        }
    }

    #[test]
    fn invalid_inputs_do_not_create_objects_or_destroy_existing_attributes() {
        unsafe {
            let attributes = make::<sv::IAttributeList>();
            assert_eq!(attributes.setInt(c"keep".as_ptr(), 5), sb::kResultOk);
            assert_eq!(attributes.setInt(std::ptr::null(), 3), sb::kInvalidArgument);
            assert_eq!(
                attributes.setBinary(c"keep".as_ptr(), std::ptr::null(), 8),
                sb::kInvalidArgument
            );
            assert_eq!(
                attributes.setBinary(
                    c"keep".as_ptr(),
                    std::ptr::dangling::<u8>().cast(),
                    (MAX_VALUE_BYTES + 1) as u32
                ),
                sb::kOutOfMemory
            );
            let mut integer = 0;
            assert_eq!(
                attributes.getInt(c"keep".as_ptr(), &mut integer),
                sb::kResultOk
            );
            assert_eq!(integer, 5);
            let mut cid = sv::IMessage::IID;
            let mut wrong = sv::IAttributeList::IID;
            let mut output = std::ptr::dangling_mut::<u8>().cast::<c_void>();
            assert_eq!(
                create_instance(
                    std::ptr::addr_of_mut!(cid).cast(),
                    std::ptr::addr_of_mut!(wrong).cast(),
                    &mut output
                ),
                sb::kResultFalse
            );
            assert!(output.is_null());
        }
    }
}
