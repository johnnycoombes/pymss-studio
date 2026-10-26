use std::sync::atomic::{AtomicBool, Ordering};

static TERMINAL_ATTACHED: AtomicBool = AtomicBool::new(false);
static VERBOSE_OUTPUT: AtomicBool = AtomicBool::new(false);

/// --debug expands terminal context. Developer mode controls collection separately,
/// so its complete records remain available in log files and the Debug page.
pub fn set_verbose(enabled: bool) {
    VERBOSE_OUTPUT.store(enabled, Ordering::Relaxed);
}

/// Connect a release GUI application to the terminal that launched it, when one exists.
///
/// Unix processes inherit stdout/stderr naturally. Windows GUI-subsystem executables do not,
/// so they have to opt in to their parent's console. `AttachConsole` does not allocate a new
/// console, which is what keeps Explorer/Finder/desktop launches free of a debug window.
pub fn attach_parent() {
    #[cfg(windows)]
    {
        const ATTACH_PARENT_PROCESS: u32 = u32::MAX;
        const FILE_TYPE_DISK: u32 = 1;
        const FILE_TYPE_PIPE: u32 = 3;

        #[link(name = "Kernel32")]
        extern "system" {
            fn AttachConsole(process_id: u32) -> i32;
            fn GetConsoleWindow() -> *mut core::ffi::c_void;
            fn GetStdHandle(kind: u32) -> *mut core::ffi::c_void;
            fn SetStdHandle(kind: u32, handle: *mut core::ffi::c_void) -> i32;
            fn GetFileType(handle: *mut core::ffi::c_void) -> u32;
        }

        // AttachConsole can replace inherited file/pipe handles. Keep redirected output
        // intact so CMD's `start /b /wait ... > file 2>&1` and piped launches retain logs.
        // SAFETY: These are borrowed process handles; none are closed or owned by Rust.
        let attached = unsafe {
            let redirected = [(-11_i32) as u32, (-12_i32) as u32].map(|kind| {
                let handle = GetStdHandle(kind);
                matches!(GetFileType(handle), FILE_TYPE_DISK | FILE_TYPE_PIPE)
                    .then_some((kind, handle))
            });
            let redirected_stderr = redirected[1].is_some();
            let console = AttachConsole(ATTACH_PARENT_PROCESS) != 0
                || !GetConsoleWindow().is_null();
            for (kind, handle) in redirected.into_iter().flatten() {
                SetStdHandle(kind, handle);
            }
            console || redirected_stderr
        };
        TERMINAL_ATTACHED.store(attached, Ordering::Relaxed);
    }

    // Unix desktop launches normally inherit a sink for stderr, while shell launches may inherit
    // either a terminal, a pipe, or a redirected file. Always attempt the write so redirection
    // keeps standard command-line semantics; write failures (including a closed pipe) are ignored.
    #[cfg(not(windows))]
    TERMINAL_ATTACHED.store(true, Ordering::Relaxed);
}

pub fn write(line: &str) {
    if is_attached() {
        use std::io::Write;
        let formatted = terminal_output(
            line,
            supports_color(),
            VERBOSE_OUTPUT.load(Ordering::Relaxed),
        );
        let _ = std::io::stderr().lock().write_all(formatted.as_bytes());
    }
}

pub fn write_early(line: &str) {
    use std::io::Write;
    let formatted = terminal_output(
        line,
        supports_color(),
        VERBOSE_OUTPUT.load(Ordering::Relaxed),
    );
    let _ = std::io::stderr().lock().write_all(formatted.as_bytes());
}

fn color_override(
    no_color: bool,
    force_color: Option<&std::ffi::OsStr>,
    forwarded_color: bool,
) -> Option<bool> {
    if no_color {
        Some(false)
    } else {
        force_color
            .map(|value| value != "0")
            .or_else(|| forwarded_color.then_some(true))
    }
}

fn supports_color() -> bool {
    use std::io::IsTerminal;
    if let Some(enabled) = color_override(
        std::env::var_os("NO_COLOR").is_some(),
        std::env::var_os("FORCE_COLOR").as_deref(),
        std::env::var("PYMSS_STUDIO_TERMINAL_COLOR").is_ok_and(|value| value == "1"),
    ) {
        // The CLI captures terminal capability before Tauri creates its stderr pipe.
        return enabled;
    }
    if !std::io::stderr().is_terminal() {
        return false;
    }
    #[cfg(windows)]
    {
        #[link(name = "Kernel32")]
        extern "system" {
            fn GetStdHandle(kind: u32) -> *mut core::ffi::c_void;
            fn GetConsoleMode(handle: *mut core::ffi::c_void, mode: *mut u32) -> i32;
            fn SetConsoleMode(handle: *mut core::ffi::c_void, mode: u32) -> i32;
        }
        let mut mode = 0;
        // SAFETY: the process owns its stderr handle; mode points to a valid u32.
        unsafe {
            let handle = GetStdHandle((-12_i32) as u32);
            GetConsoleMode(handle, &mut mode) != 0 && SetConsoleMode(handle, mode | 0x0004) != 0
        }
    }
    #[cfg(not(windows))]
    {
        std::env::var("TERM").is_ok_and(|term| term != "dumb")
    }
}

fn colored(text: &str, code: &str, enabled: bool) -> String {
    if enabled {
        format!("\x1b[{code}m{text}\x1b[0m")
    } else {
        text.to_string()
    }
}

fn context_text(value: &serde_json::Value) -> Option<String> {
    match value {
        serde_json::Value::Null => None,
        serde_json::Value::String(value) if value.is_empty() => None,
        serde_json::Value::String(value) => Some(value.replace('\r', "\\r").replace('\n', "\\n")),
        value => Some(value.to_string()),
    }
}

fn compact_text(value: &str, limit: usize) -> String {
    let value = value
        .replace('\r', "\\r")
        .replace('\n', "\\n")
        .replace('\t', "\\t");
    let mut chars = value.chars();
    let mut text: String = chars.by_ref().take(limit).collect();
    if chars.next().is_some() {
        text.push('…');
    }
    text
}

fn has_error_context(context: &serde_json::Map<String, serde_json::Value>) -> bool {
    context.iter().any(|(key, value)| {
        let key = key.to_ascii_lowercase();
        (key.contains("error")
            || key.contains("exception")
            || key.contains("traceback")
            || matches!(key.as_str(), "failed" | "failedcount"))
            && match value {
                serde_json::Value::Null | serde_json::Value::Bool(false) => false,
                serde_json::Value::String(value) => !value.trim().is_empty(),
                serde_json::Value::Number(value) => {
                    value.as_f64().is_some_and(|value| value != 0.0)
                }
                serde_json::Value::Array(value) => !value.is_empty(),
                serde_json::Value::Object(value) => !value.is_empty(),
                serde_json::Value::Bool(true) => true,
            }
    })
}

fn terminal_output(line: &str, color: bool, verbose: bool) -> String {
    match (verbose, color) {
        (true, false) => format_line(line),
        (true, true) => format_with_color(line, true),
        (false, color) => format_record(line, color, false),
    }
}

fn format_with_color(line: &str, color: bool) -> String {
    format_record(line, color, true)
}

fn format_record(line: &str, color: bool, verbose: bool) -> String {
    let Ok(record) = serde_json::from_str::<serde_json::Value>(line) else {
        return format_legacy_line(line);
    };
    if record
        .get("schemaVersion")
        .and_then(serde_json::Value::as_u64)
        != Some(1)
    {
        return format_legacy_line(line);
    }
    let value = |key: &str| {
        record
            .get(key)
            .and_then(serde_json::Value::as_str)
            .unwrap_or("")
    };
    let level = value("level").to_ascii_uppercase();
    let level = if level.is_empty() { "RAW" } else { &level };
    let context = record.get("context").and_then(serde_json::Value::as_object);
    let expanded = verbose
        || matches!(level, "WARN" | "ERROR" | "FATAL" | "RAW")
        || !value("detail").is_empty()
        || context.is_some_and(has_error_context);
    if !expanded
        && value("source") == "studio"
        && matches!(level, "DEBUG" | "INFO" | "TRACE")
        && matches!(value("event"), "worker.request" | "worker.spawn")
    {
        return String::new();
    }
    let level_color = match level {
        "ERROR" | "FATAL" => "1;31",
        "WARN" => "1;33",
        "INFO" => "32",
        _ => "90",
    };
    let (source, source_color) = match value("source") {
        "studio" => ("Studio", "94"),
        "worker" => ("Worker", "96"),
        "pymss" => ("pymss", "95"),
        _ => ("External", "93"),
    };
    let clock =
        terminal_clock(value("timestamp")).unwrap_or_else(|| value("timestamp").to_string());
    let message = value("message");
    let message = if message.is_empty() {
        value("event")
    } else {
        message
    };
    let message = if expanded {
        message.to_string()
    } else {
        compact_text(message, 180)
    };
    let message = match level {
        "WARN" => colored(&message, "33", color),
        "ERROR" | "FATAL" => colored(&message, "1;31", color),
        _ => message,
    };
    let mut scope = Vec::new();
    let mut displayed_keys = Vec::new();
    for key in ["taskId", "requestId", "nodeId", "jobId"] {
        if !expanded && !scope.is_empty() && key != "nodeId" {
            continue;
        }
        if !value(key).is_empty() {
            let text = if expanded {
                value(key).to_string()
            } else {
                compact_text(value(key), 96)
            };
            scope.push(format!("{key}={text}"));
            displayed_keys.push(key);
        }
    }
    if let Some(context) = context {
        let keys: &[&str] = if expanded {
            &["command", "model", "backend", "version", "status"]
        } else {
            &[
                "command",
                "model",
                "backend",
                "version",
                "status",
                "stage",
                "phase",
                "progress",
                "completed",
                "total",
                "succeeded",
                "failedCount",
                "warningCount",
                "outputCount",
                "durationMs",
                "name",
                "count",
                "torchVersion",
                "torchAvailable",
                "cudaAvailable",
                "cudaDeviceCount",
                "logsDir",
            ]
        };
        for &key in keys {
            if !expanded
                && context
                    .get(key)
                    .is_some_and(|value| value.is_array() || value.is_object())
            {
                continue;
            }
            if let Some(text) = context.get(key).and_then(context_text) {
                let text = if expanded {
                    text
                } else {
                    compact_text(&text, 96)
                };
                scope.push(format!("{key}={text}"));
                displayed_keys.push(key);
            }
        }
    }
    let mut formatted = format!(
        "{} {} {} {}",
        colored(&clock, "90", color),
        colored(&format!("{level:<5}"), level_color, color),
        colored(&format!("[{source}]"), source_color, color),
        message
    );
    if !scope.is_empty() {
        formatted.push_str(&colored(&format!("  {}", scope.join(" ")), "90", color));
    }
    formatted.push('\n');
    if expanded && !value("logger").is_empty() && value("logger") != value("source") {
        formatted.push_str(&colored(
            &format!("    logger={}\n", value("logger")),
            "90",
            color,
        ));
    }
    if let Some(location) = record
        .get("location")
        .filter(|_| expanded)
        .and_then(context_text)
    {
        formatted.push_str(&colored(&format!("    location={location}\n"), "90", color));
    }
    if let Some(context) = context.filter(|_| expanded) {
        for (key, value) in context {
            // Keep useful diagnostics even when a producer adds new context fields. Only
            // omit transport bookkeeping and values already present on the compact line.
            if displayed_keys.contains(&key.as_str())
                || matches!(key.as_str(), "processId" | "thread")
            {
                continue;
            }
            if let Some(text) = context_text(value) {
                formatted.push_str(&colored(&format!("    {key}={text}\n"), "90", color));
            }
        }
    }
    if expanded && !value("detail").is_empty() {
        for detail in value("detail").lines() {
            formatted.push_str(&colored(
                &format!("    {detail}\n"),
                if level == "ERROR" || level == "FATAL" {
                    "31"
                } else {
                    "90"
                },
                color,
            ));
        }
    }
    formatted
}

/// Render complete diagnostic context without changing the machine-oriented log files.
/// Live terminal output is compact; --debug, errors, raw stderr and explicit details expand it.
/// Rust writes epoch timestamps while Python writes ISO timestamps; both become a compact
/// local clock value in the terminal, leaving the event payload untouched.
pub fn format_line(line: &str) -> String {
    format_with_color(line, false)
}

fn format_legacy_line(line: &str) -> String {
    let Some((timestamp, rest)) = line.split_once(' ') else {
        return line.to_string();
    };
    let Some(clock) = terminal_clock(timestamp) else {
        return line.to_string();
    };
    format!("{clock} {rest}")
}

fn terminal_clock(timestamp: &str) -> Option<String> {
    use chrono::{DateTime, Local, TimeZone};

    if timestamp.contains('T') {
        let parsed = DateTime::parse_from_rfc3339(timestamp).ok()?;
        return Some(
            parsed
                .with_timezone(&Local)
                .format("%H:%M:%S%.3f")
                .to_string(),
        );
    }
    let (seconds, millis) = timestamp.trim_end_matches('Z').split_once('.')?;
    let seconds = seconds.parse::<i64>().ok()?;
    let millis = millis.get(..3).unwrap_or(millis).parse::<i64>().ok()?;
    let timestamp_millis = seconds.checked_mul(1000)?.checked_add(millis)?;
    Local
        .timestamp_millis_opt(timestamp_millis)
        .single()
        .map(|value| value.format("%H:%M:%S%.3f").to_string())
}

pub fn is_attached() -> bool {
    TERMINAL_ATTACHED.load(Ordering::Relaxed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn structured_output_has_one_level_and_source_and_preserves_exception_lines() {
        let record = serde_json::json!({"schemaVersion":1,"timestamp":"2026-10-19T12:00:00Z","source":"pymss","level":"error","message":"Allocation failed","taskId":"task-1","detail":"Traceback:\n  model.forward()"}).to_string();
        let plain = format_line(&record);
        assert_eq!(plain.matches("ERROR").count(), 1);
        assert!(plain.contains("[pymss] Allocation failed"));
        assert!(plain.contains("    Traceback:\n      model.forward()"));
        assert!(!plain.contains("\x1b["));
        let color = format_with_color(&record, true);
        assert!(color.contains("\x1b[1;31mERROR"));
        assert!(color.contains("\x1b[95m[pymss]"));
    }

    #[test]
    fn stderr_without_a_level_is_not_presented_as_warning() {
        let record = serde_json::json!({"schemaVersion":1,"source":"third_party","level":null,"message":"Loading decoder"}).to_string();
        let plain = format_line(&record);
        assert!(plain.contains("RAW"));
        assert!(!plain.contains("WARN"));
    }

    #[test]
    fn redirected_output_preserves_diagnostic_context_without_repeated_scope() {
        let record = serde_json::json!({
            "schemaVersion": 1, "timestamp": "2026-10-19T12:00:00Z",
            "source": "studio", "level": "debug", "message": "worker.command", "logger": "studio.worker",
            "location": {"file": "worker.rs", "line": 72, "function": "probe"},
            "taskId": "task-a", "requestId": "request-a", "nodeId": "node-a", "jobId": "job-a",
            "context": {
                "command": "env_info", "taskId": "task-a", "requestId": "request-a",
                "nodeId": "node-a", "jobId": "job-a", "backend": "cpu",
                "python": "D:/runtime/python.exe", "worker": "D:/app/python/worker.py",
                "payloadFile": "D:/Temp/payload.json", "path": "D:/Pymss Studio/app.exe",
                "dataRoot": "D:/Pymss Studio/data", "logsDir": "D:/Pymss Studio/logs",
                "torchError": "DLL load failed: missing dependency",
                "cudaAvailableError": "Driver unavailable", "torchAvailable": false,
                "deviceCount": 0, "futureDiagnostic": {"enabled": true, "devices": [1, 2]},
                "processId": 123, "thread": "MainThread", "empty": "", "missing": null
            }
        });
        let input = record.to_string();
        let plain = format_line(&input);
        for (key, value) in record["context"].as_object().unwrap() {
            if matches!(key.as_str(), "processId" | "thread" | "empty" | "missing") {
                continue;
            }
            let text = context_text(value).unwrap();
            assert!(
                plain.contains(&format!("{key}={text}")),
                "missing {key}: {plain}"
            );
            assert_eq!(
                plain.matches(&format!("{key}=")).count(),
                1,
                "repeated {key}"
            );
        }
        assert_eq!(plain.matches("DEBUG").count(), 1);
        assert_eq!(plain.matches("[Studio]").count(), 1);
        let clock = terminal_clock("2026-10-19T12:00:00Z").unwrap();
        assert_eq!(plain.matches(&clock).count(), 1);
        assert!(!plain.contains('\u{1b}'));
        assert!(plain.contains("    logger=studio.worker\n"));
        assert!(plain.contains(&format!("    location={}\n", record["location"])));
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&input).unwrap(),
            record
        );
        let color = format_with_color(&input, true);
        assert!(color.contains("\x1b[94m[Studio]"));
        assert!(color.contains("\x1b[90m    torchError=DLL load failed: missing dependency"));
    }

    #[test]
    fn context_values_keep_multiline_diagnostics_on_indented_lines() {
        let input = serde_json::json!({
            "schemaVersion": 1, "source": "worker", "level": "info", "message": "Probe complete",
            "context": {"probeError": "first line\nsecond line\rthird line", "status": false}
        })
        .to_string();
        let plain = format_line(&input);
        assert!(plain.contains("status=false"));
        assert!(plain.contains("\n    probeError=first line\\nsecond line\\rthird line\n"));
    }

    #[test]
    fn compact_worker_output_omits_summary_and_transport_details() {
        let input = serde_json::json!({
            "schemaVersion": 1, "source": "studio", "level": "debug",
            "logger": "studio.worker", "event": "worker.command", "message": "worker.command",
            "context": {
                "command": "audio_metadata", "operationId": "rust-123-456-789",
                "python": "D:/runtime/python.exe", "worker": "D:/app/python/worker.py",
                "feature": "other", "parameters": {},
                "_summary": {"limits": {"errorChars": 600}, "truncated": false}
            }
        })
        .to_string();
        let compact = terminal_output(&input, false, false);
        assert_eq!(compact.lines().count(), 1);
        assert!(compact.contains("DEBUG [Studio] worker.command  command=audio_metadata"));
        for text in [
            "logger=",
            "operationId=",
            "python=",
            "worker=",
            "_summary",
            "parameters",
            "feature=",
        ] {
            assert!(!compact.contains(text), "unexpected {text}: {compact}");
        }
        let full = terminal_output(&input, false, true);
        for text in [
            "logger=studio.worker",
            "operationId=rust-123-456-789",
            "python=D:/runtime/python.exe",
            "_summary=",
        ] {
            assert!(full.contains(text), "missing {text}: {full}");
        }
    }

    #[test]
    fn compact_output_suppresses_only_redundant_normal_studio_worker_events() {
        for event in ["worker.request", "worker.spawn"] {
            let mut input = serde_json::json!({"schemaVersion":1,"source":"studio","level":"debug",
                "event":event,"message":"Operation requested","context":{"command":"runtime_info"}});
            assert!(terminal_output(&input.to_string(), false, false).is_empty());
            assert!(!terminal_output(&input.to_string(), false, true).is_empty());
            input["level"] = "error".into();
            assert!(!terminal_output(&input.to_string(), false, false).is_empty());
            input["level"] = "debug".into();
            input["source"] = "worker".into();
            assert!(!terminal_output(&input.to_string(), false, false).is_empty());
        }
        for event in ["operation.request", "worker.command", "worker.exit"] {
            let input = serde_json::json!({"schemaVersion":1,"source":"studio","level":"info",
                "event":event,"message":event,"context":{"command":"infer","durationMs":"1240"}});
            let output = terminal_output(&input.to_string(), false, false);
            assert!(output.contains(event));
            assert!(output.contains("command=infer durationMs=1240"));
        }
    }

    #[test]
    fn compact_warnings_and_errors_keep_complete_context_and_tracebacks() {
        for level in ["warn", "error", "fatal"] {
            let input = serde_json::json!({"schemaVersion":1,"source":"worker","level":level,
                "logger":"worker.inference","event":"task.failed","message":"Separation failed",
                "detail":"Traceback:\n  model.forward()\nRuntimeError: allocation failed",
                "taskId":"task-a","requestId":"request-a","nodeId":"node-a","jobId":"job-a",
                "location":{"file":"infer.py","line":42},
                "context":{"command":"infer","operationId":"operation-a","customDiagnostic":{"memory":1024}}});
            let output = terminal_output(&input.to_string(), false, false);
            assert!(output.contains("logger=worker.inference"));
            assert!(output.contains("location="));
            assert!(output.contains("customDiagnostic="));
            assert!(output.contains("requestId=request-a"));
            assert!(output.contains("operationId=operation-a"));
            assert!(output.contains(
                "    Traceback:\n      model.forward()\n    RuntimeError: allocation failed\n"
            ));
        }
    }

    #[test]
    fn informational_probe_failures_are_not_hidden_by_compact_output() {
        let input = serde_json::json!({"schemaVersion":1,"source":"studio","level":"info",
            "event":"runtime.torch","message":"runtime.torch",
            "context":{"torchAvailable":false,"torchError":"DLL load failed\nmissing module",
                "python":"D:/runtime/python.exe"}});
        let output = terminal_output(&input.to_string(), false, false);
        assert!(output.contains("    torchError=DLL load failed\\nmissing module\n"));
        assert!(output.contains("    python=D:/runtime/python.exe\n"));
        assert!(output.contains("torchAvailable=false"));
        let success = serde_json::json!({"schemaVersion":1,"source":"studio","level":"info",
            "message":"Probe complete","context":{"torchAvailable":true,"torchError":"","errorCount":0}});
        assert_eq!(
            terminal_output(&success.to_string(), false, false)
                .lines()
                .count(),
            1
        );
    }

    #[test]
    fn compact_output_bounds_unicode_values_and_keeps_a_single_logical_line() {
        let input = serde_json::json!({"schemaVersion":1,"source":"studio","level":"info",
            "message":"模型".repeat(200),
            "taskId":"task-a","requestId":"request-a","nodeId":"node-a","jobId":"job-a",
            "context":{"model":"音频".repeat(100),"command":"infer","status":"running"}})
        .to_string();
        let output = terminal_output(&input, false, false);
        assert_eq!(output.lines().count(), 1);
        assert!(output.contains('…'));
        assert!(output.contains("taskId=task-a"));
        assert!(output.contains("nodeId=node-a"));
        assert!(!output.contains("requestId="));
        assert!(!output.contains("jobId="));
        assert!(!output.contains('\u{1b}'));
        assert!(terminal_output(&input, true, false).contains("\x1b[94m[Studio]"));
    }

    #[test]
    fn compact_output_keeps_unstructured_and_unknown_schema_logs() {
        let legacy = "external diagnostic line\n";
        assert_eq!(terminal_output(legacy, false, false), legacy);
        let future = "{\"schemaVersion\":2,\"message\":\"external\"}\n";
        assert_eq!(terminal_output(future, false, false), future);
    }

    #[test]
    fn raw_stderr_and_explicit_details_are_never_abbreviated() {
        let traceback = format!(
            "Traceback:\n  File \"{}\", line 42\nRuntimeError: failed",
            "很长的路径/".repeat(60)
        );
        for level in [
            serde_json::Value::Null,
            serde_json::json!("info"),
            serde_json::json!("debug"),
        ] {
            let input = serde_json::json!({"schemaVersion":1,"source":"third_party","level":level,
                "logger":"stderr","message":"Unstructured exception","detail":traceback});
            let output = terminal_output(&input.to_string(), false, false);
            assert!(output.contains(&"很长的路径/".repeat(60)));
            assert!(output.contains("    RuntimeError: failed\n"));
        }
        let raw = serde_json::json!({"schemaVersion":1,"source":"third_party","level":null,
            "message":format!("File {}: RuntimeError: failed", "path/".repeat(60))});
        let output = terminal_output(&raw.to_string(), false, false);
        assert!(output.contains(&"path/".repeat(60)));
        assert!(output.contains(": RuntimeError: failed"));
    }

    #[test]
    fn unsuccessful_worker_exits_retain_status_and_duration_without_an_error_level() {
        let input = serde_json::json!({"schemaVersion":1,"source":"studio","level":"info",
            "event":"worker.exit","message":"worker.exit",
            "context":{"command":"audio_metadata","status":"exit code: 1","durationMs":"2450"}});
        let output = terminal_output(&input.to_string(), false, false);
        assert!(output.contains("command=audio_metadata status=exit code: 1 durationMs=2450"));
        assert_eq!(output.lines().count(), 1);
    }

    #[test]
    fn compact_progress_keeps_stage_and_counts_visible() {
        let input = serde_json::json!({"schemaVersion":1,"source":"worker","level":"debug",
            "event":"runtime_install_stage","message":"Preparing runtime","taskId":"runtime-a",
            "context":{"command":"install_runtime","stage":"download","progress":35,
                "completed":2,"total":5,"_summary":{"limits":{"errorChars":600}}}});
        let output = terminal_output(&input.to_string(), false, false);
        for field in [
            "taskId=runtime-a",
            "stage=download",
            "progress=35",
            "completed=2",
            "total=5",
        ] {
            assert!(output.contains(field), "missing {field}: {output}");
        }
        assert!(!output.contains("_summary"));
        assert_eq!(output.lines().count(), 1);
    }

    #[test]
    fn explicit_color_preferences_cover_forwarded_output_and_no_color_wins() {
        use std::ffi::OsStr;
        assert_eq!(color_override(false, None, false), None);
        assert_eq!(color_override(false, None, true), Some(true));
        assert_eq!(color_override(true, None, true), Some(false));
        assert_eq!(
            color_override(false, Some(OsStr::new("0")), true),
            Some(false)
        );
        for value in ["", "1", "2", "3"] {
            assert_eq!(
                color_override(false, Some(OsStr::new(value)), false),
                Some(true)
            );
            assert_eq!(
                color_override(true, Some(OsStr::new(value)), true),
                Some(false)
            );
        }
    }

    #[test]
    fn colored_severity_and_messages_remain_readable_without_ansi() {
        for (level, color) in [("info", "32"), ("warn", "1;33"), ("error", "1;31")] {
            let input = serde_json::json!({"schemaVersion":1,"source":"studio","level":level,
                "message":"Task status","context":{"command":"infer"}})
            .to_string();
            let styled = terminal_output(&input, true, false);
            assert!(styled.contains(&format!("\x1b[{color}m{}", level.to_uppercase())));
            assert!(styled.contains("\x1b[94m[Studio]"));
            match level {
                "warn" => assert!(styled.contains("\x1b[33mTask status\x1b[0m")),
                "error" => assert!(styled.contains("\x1b[1;31mTask status\x1b[0m")),
                _ => assert!(styled.contains("Task status")),
            }
            let plain = terminal_output(&input, false, false);
            assert!(!plain.contains('\u{1b}'));
            assert!(plain.contains("Task status"));
            assert!(plain.contains("command=infer"));
        }
    }
}
