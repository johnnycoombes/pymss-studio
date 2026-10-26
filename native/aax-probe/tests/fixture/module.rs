use std::ffi::c_void;
#[no_mangle]
pub extern "system" fn ACFRegisterPlugin(_: *mut c_void, _: *mut *mut c_void) -> i32 {
    -1
}
#[no_mangle]
pub extern "system" fn ACFRegisterComponent(_: *mut c_void, _: u32, _: *mut *mut c_void) -> i32 {
    -1
}
#[no_mangle]
pub extern "system" fn ACFGetClassFactory(
    _: *mut c_void,
    _: *const c_void,
    _: *const c_void,
    _: *mut *mut c_void,
) -> i32 {
    -1
}
#[no_mangle]
pub extern "system" fn ACFCanUnloadNow(_: *mut c_void) -> i32 {
    0
}
#[no_mangle]
pub extern "system" fn ACFStartup(_: *mut c_void) -> i32 {
    -1
}
#[no_mangle]
pub extern "system" fn ACFShutdown(_: *mut c_void) -> i32 {
    -1
}
#[cfg(not(missing_sdk))]
#[no_mangle]
pub unsafe extern "system" fn ACFGetSDKVersion(out: *mut u64) -> i32 {
    println!("native module output");
    if out.is_null() {
        return -1;
    }
    #[cfg(manual_behaviors)]
    {
        let behavior = std::env::var("PYMSS_AAX_FIXTURE_BEHAVIOR").unwrap_or_default();
        match behavior.as_str() {
            "crash" => std::process::abort(),
            "hang" => std::thread::sleep(std::time::Duration::from_secs(60)),
            "negative" => {
                unsafe {
                    *out = 0x0208000100000002;
                }
                return 0x80004005u32 as i32;
            }
            "positive" => {
                unsafe {
                    *out = 0x0208000100000002;
                }
                return 1;
            }
            "zero" => {
                unsafe {
                    *out = 0;
                }
                return 0;
            }
            _ => {}
        }
    }
    unsafe {
        *out = if cfg!(zero_version) {
            0
        } else {
            0x0208000100000002
        };
    }
    if cfg!(failure) {
        0x80004005u32 as i32
    } else if cfg!(positive_success) {
        1
    } else {
        0
    }
}
