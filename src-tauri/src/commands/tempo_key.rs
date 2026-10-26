//! BPM & key analysis tool, run in-process with the vendored `rba` engine.
//!
//! Events mirror the Python audio tool protocol (`audio_tool_progress`,
//! `audio_tool_result`, `error`, `task_cancelled`) so the frontend runtime
//! treats this tool like any other.

use crate::error::{AppError, AppResult};
use crate::python::protocol::WorkerEnvelope;
use crate::python::worker::{bundled_bin_dirs, log_worker_event};
use crate::rba;
use crate::session_log;
use crate::state::AppState;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::{AppHandle, Emitter, Manager, State};

const OPERATION: &str = "tempo_key";

fn bundled_tool(app: &AppHandle, name: &str) -> PathBuf {
    let file = if cfg!(windows) { format!("{name}.exe") } else { name.to_string() };
    bundled_bin_dirs(app)
        .unwrap_or_default()
        .into_iter()
        .map(|dir| dir.join(&file))
        .find(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from(file))
}

fn analyze(app: &AppHandle, path: &Path, cancelled: &AtomicBool, include_key: bool) -> Result<rba::Analysis, String> {
    if !path.is_file() {
        return Err(format!("Input audio does not exist: {}", path.display()));
    }
    rba::analyze_file(
        &bundled_tool(app, "ffmpeg"),
        &bundled_tool(app, "ffprobe"),
        path,
        cancelled,
        include_key,
    )
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.to_string_lossy().into_owned())
}

/// Round to two decimals, matching the precision rba prints.
fn round2(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}

fn bpm_result(analysis: &rba::Analysis, path: &Path) -> Result<Value, String> {
    if !analysis.bpm.is_finite() || analysis.bpm <= 0.0 {
        return Err("No rhythmic onsets were detected in the audio".into());
    }
    Ok(json!({
        "operation": "midi", "action": "detect_bpm", "succeeded": 1,
        "inputPath": path.to_string_lossy(), "bpm": round2(analysis.bpm),
        "roundedBpm": analysis.bpm.round() as i64,
    }))
}

fn native_envelope(event_type: &str, request_id: &str, payload: Value) -> WorkerEnvelope {
    WorkerEnvelope {
        event_type: event_type.to_string(),
        request_id: Some(request_id.to_string()),
        task_id: Some(request_id.to_string()),
        timestamp: Some(chrono::Utc::now().to_rfc3339()),
        payload,
    }
}

fn emit(app: &AppHandle, event_type: &str, request_id: &str, payload: Value) {
    let mut envelope = native_envelope(event_type, request_id, payload);
    log_worker_event(app, "audio_tools", &mut envelope);
    let _ = app.emit("pymss://worker-event", &envelope);
}

fn input_paths(payload: &Value) -> Vec<PathBuf> {
    let mut seen = std::collections::HashSet::new();
    payload
        .get("inputs")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::trim)
        .filter(|path| !path.is_empty() && seen.insert(path.to_string()))
        .map(PathBuf::from)
        .collect()
}

fn run(app: &AppHandle, request_id: &str, inputs: &[PathBuf], cancelled: &AtomicBool, bpm_only: bool) -> Option<Value> {
    let operation = if bpm_only { "midi" } else { OPERATION };
    let total = inputs.len();
    let mut items = Vec::new();
    let mut failed = Vec::new();
    for (index, path) in inputs.iter().enumerate() {
        if cancelled.load(Ordering::SeqCst) {
            return None;
        }
        emit(app, "audio_tool_progress", request_id, json!({
            "operation": operation, "completed": index, "total": total,
            "current": file_name(path), "phase": if bpm_only { "calculating" } else { "analyzing_tempo_key" },
        }));
        match analyze(app, path, cancelled, !bpm_only) {
            Ok(analysis) if bpm_only => match bpm_result(&analysis, path) {
                Ok(result) => return Some(result),
                Err(message) => failed.push(json!({ "path": path.to_string_lossy(), "message": message })),
            },
            Ok(analysis) => items.push(json!({
                "path": path.to_string_lossy(),
                "fileName": file_name(path),
                "bpm": round2(analysis.bpm),
                "key": analysis.key,
                "duration": round2(analysis.duration),
            })),
            Err(message) => failed.push(json!({ "path": path.to_string_lossy(), "message": message })),
        }
    }
    if cancelled.load(Ordering::SeqCst) {
        return None;
    }
    Some(json!({
        "operation": operation,
        "items": items,
        "succeeded": items.len(),
        "failed": failed,
    }))
}

#[tauri::command]
pub async fn start_tempo_key_analysis(
    app: AppHandle,
    state: State<'_, AppState>,
    payload: Value,
) -> AppResult<Value> {
    let operation_id = session_log::record_operation_request(&app, "audio_tools", &payload, None);
    let result = start_native_analysis(&app, &state, payload);
    if let Err(error) = &result {
        session_log::record_operation_failure(&app, "audio_tools", &operation_id, &error.to_string());
    }
    result
}

fn start_native_analysis(app: &AppHandle, state: &AppState, payload: Value) -> AppResult<Value> {
    let application_access = crate::python::worker::acquire_application_operation()?;
    let request_id = payload
        .get("requestId")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| AppError::Worker("missing audio tool request id".into()))?
        .to_string();
    let inputs = input_paths(&payload);
    if inputs.is_empty() {
        return Err(AppError::Worker("No input audio files were provided".into()));
    }
    let bpm_only = match payload.get("operation").and_then(Value::as_str) {
        Some("tempo_key") => false,
        Some("midi") if payload.get("action").and_then(Value::as_str) == Some("detect_bpm") => true,
        _ => return Err(AppError::Worker("Unsupported native audio tool operation".into())),
    };
    if bpm_only && inputs.len() != 1 {
        return Err(AppError::Worker("BPM detection requires exactly one input audio file".into()));
    }
    let cancelled = Arc::new(AtomicBool::new(false));
    {
        let mut tasks = state
            .native_tasks
            .lock()
            .map_err(|_| AppError::Worker("task registry lock poisoned".into()))?;
        if tasks.contains_key(&request_id) {
            return Err(AppError::Worker(format!("task already exists: {request_id}")));
        }
        tasks.insert(request_id.clone(), cancelled.clone());
    }
    let handle = app.clone();
    let task_id = request_id.clone();
    std::thread::spawn(move || {
        let _application_access = application_access;
        let result = run(&handle, &task_id, &inputs, &cancelled, bpm_only);
        let state = handle.state::<AppState>();
        let Ok(mut tasks) = state.native_tasks.lock() else {
            return;
        };
        // Serialize the terminal event with cancellation before unregistering.
        let result = if cancelled.load(Ordering::SeqCst) {
            None
        } else {
            result
        };
        match result {
            Some(result) if result["succeeded"].as_u64().is_some_and(|count| count > 0) => {
                emit(&handle, "audio_tool_result", &task_id, result)
            }
            Some(result) => {
                let message = result["failed"][0]["message"].as_str().unwrap_or("Analysis failed").to_string();
                emit(&handle, "error", &task_id, json!({
                    "code": "TEMPO_KEY_FAILED", "message": message,
                    "recoverable": true, "operation": if bpm_only { "midi" } else { OPERATION },
                }))
            }
            None => emit(&handle, "task_cancelled", &task_id, json!({ "message": "Cancelled" })),
        }
        tasks.remove(&task_id);
    });
    Ok(json!({ "taskId": request_id, "started": true }))
}

/// Flag a running analysis as cancelled. Returns `false` when `task_id` is not
/// an in-process task so callers can fall back to worker cancellation.
pub fn cancel_native_task(state: &AppState, task_id: &str) -> bool {
    let Ok(tasks) = state.native_tasks.lock() else {
        return false;
    };
    let Some(flag) = tasks.get(task_id) else {
        return false;
    };
    flag.store(true, Ordering::SeqCst);
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_analysis_envelope_retains_worker_event_contract() {
        let payload = json!({ "operation": "tempo_key", "succeeded": 1, "items": [] });
        let envelope = native_envelope("audio_tool_result", "analysis-request", payload.clone());
        let serialized = serde_json::to_value(envelope).unwrap();
        assert_eq!(serialized["type"], "audio_tool_result");
        assert_eq!(serialized["requestId"], "analysis-request");
        assert_eq!(serialized["taskId"], "analysis-request");
        assert_eq!(serialized["payload"], payload);
        assert!(chrono::DateTime::parse_from_rfc3339(serialized["timestamp"].as_str().unwrap()).is_ok());
    }

    #[test]
    fn native_midi_envelope_preserves_operation_and_action() {
        let payload = json!({ "operation": "midi", "action": "detect_bpm", "bpm": 128.0 });
        let envelope = native_envelope("audio_tool_result", "midi-request", payload.clone());
        assert_eq!(envelope.event_type, "audio_tool_result");
        assert_eq!(envelope.request_id.as_deref(), Some("midi-request"));
        assert_eq!(envelope.task_id.as_deref(), Some("midi-request"));
        assert_eq!(envelope.payload, payload);
    }

    #[test]
    fn bpm_detection_uses_midi_result_contract_and_rejects_missing_tempo() {
        let mut analysis = rba::Analysis { bpm: 128.4, key: String::new(), duration: 40.0 };
        let result = bpm_result(&analysis, Path::new("audio.wav")).unwrap();
        assert_eq!(result["operation"], "midi");
        assert_eq!(result["action"], "detect_bpm");
        assert_eq!(result["roundedBpm"], 128);
        assert!(result.get("key").is_none());
        for bpm in [0.0, f64::NAN, f64::INFINITY] {
            analysis.bpm = bpm;
            assert!(bpm_result(&analysis, Path::new("audio.wav")).is_err());
        }
    }

    #[test]
    fn cancelling_native_analysis_keeps_it_registered_until_worker_finishes() {
        let state = AppState::new();
        let flag = Arc::new(AtomicBool::new(false));
        state.native_tasks.lock().unwrap().insert("bpm-request".into(), flag.clone());
        assert!(cancel_native_task(&state, "bpm-request"));
        assert!(flag.load(Ordering::SeqCst));
        assert!(state.native_tasks.lock().unwrap().contains_key("bpm-request"));
        assert!(cancel_native_task(&state, "bpm-request"));
        assert!(!cancel_native_task(&state, "missing"));
    }
}
