use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::sync::atomic::{AtomicU64, Ordering};

static RECORD_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogLocation {
    pub file: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub line: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub function: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogRecord {
    pub schema_version: u8,
    pub id: String,
    pub timestamp: String,
    pub source: String,
    pub level: Option<String>,
    pub logger: String,
    pub event: String,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub location: Option<LogLocation>,
    #[serde(default)]
    pub context: Map<String, Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sequence: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
}

impl LogRecord {
    pub fn new(
        source: &str,
        level: Option<&str>,
        logger: &str,
        event: &str,
        message: &str,
    ) -> Self {
        Self {
            schema_version: 1,
            id: format!(
                "rust-{}-{}-{}",
                std::process::id(),
                chrono::Utc::now().timestamp_micros(),
                RECORD_COUNTER.fetch_add(1, Ordering::Relaxed)
            ),
            timestamp: timestamp(),
            source: normalize_source(source).to_string(),
            level: normalize_level(level),
            logger: logger.to_string(),
            event: event.to_string(),
            message: message.to_string(),
            detail: None,
            task_id: None,
            request_id: None,
            node_id: None,
            job_id: None,
            location: None,
            context: Map::new(),
            sequence: None,
            session_id: None,
        }
    }

    pub fn from_payload(payload: &Value) -> Option<Self> {
        if payload.get("schemaVersion")?.as_u64()? != 1 {
            return None;
        }
        let mut record: Self = serde_json::from_value(payload.clone()).ok()?;
        record.source = normalize_source(&record.source).to_string();
        record.level = normalize_level(record.level.as_deref());
        if record.id.is_empty() {
            record.id = Self::new("worker", None, "", "", "").id;
        }
        // Preserve the producer's event time while ensuring the stored contract stays ISO 8601.
        record.timestamp = chrono::DateTime::parse_from_rfc3339(&record.timestamp)
            .map(|time| {
                time.with_timezone(&chrono::Utc)
                    .to_rfc3339_opts(chrono::SecondsFormat::AutoSi, true)
            })
            .unwrap_or_else(|_| timestamp());
        record.sequence = None;
        record.session_id = None;
        Some(record)
    }
}

pub fn timestamp() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

pub fn normalize_level(level: Option<&str>) -> Option<String> {
    match level?.to_ascii_lowercase().as_str() {
        "trace" => Some("trace".into()),
        "debug" | "dbg" => Some("debug".into()),
        "info" | "inf" => Some("info".into()),
        "warn" | "warning" | "war" => Some("warn".into()),
        "error" | "err" => Some("error".into()),
        "fatal" | "critical" | "cri" => Some("fatal".into()),
        _ => None,
    }
}

fn normalize_source(source: &str) -> &str {
    match source {
        "studio" | "worker" | "pymss" | "third_party" => source,
        _ => "third_party",
    }
}

/// Adapt only recognized historical pymss logging prefixes. Unstructured stderr has no level.
pub fn legacy_stderr(line: &str) -> LogRecord {
    let clean = strip_ansi(line);
    let mut record = LogRecord::new("third_party", None, "stderr", "process.stderr", &clean);
    // pymss.logger.ColorFormatter: HH:MM:SS | INF | path/to/pymss/module.py:42 | text.
    let pipe_parts: Vec<&str> = clean.splitn(4, " | ").collect();
    if pipe_parts.len() == 4 {
        if let (Some(level), Some((file, line))) = (
            normalize_level(Some(pipe_parts[1])),
            pipe_parts[2].rsplit_once(':'),
        ) {
            if let (Ok(line), Ok(time)) = (
                line.parse::<u64>(),
                chrono::NaiveTime::parse_from_str(pipe_parts[0], "%H:%M:%S"),
            ) {
                let normalized_file = file.replace('\\', "/");
                record.source = if normalized_file.split('/').any(|part| part == "pymss") {
                    "pymss"
                } else {
                    "third_party"
                }
                .into();
                record.level = Some(level);
                record.logger = if record.source == "pymss" {
                    "pymss"
                } else {
                    "python"
                }
                .into();
                record.location = Some(LogLocation {
                    file: file.into(),
                    line: Some(line),
                    function: None,
                });
                record.message = pipe_parts[3].into();
                use chrono::TimeZone;
                let today = chrono::Local::now().date_naive().and_time(time);
                if let Some(time) = chrono::Local.from_local_datetime(&today).single() {
                    record.timestamp = time
                        .with_timezone(&chrono::Utc)
                        .to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
                }
            }
        }
    }
    // Standard Python format: 2026-10-19 20:35:00,123 - pymss.module - INFO - Message.
    let parts: Vec<&str> = clean.splitn(4, " - ").collect();
    if parts.len() == 4 && (parts[1] == "pymss" || parts[1].starts_with("pymss.")) {
        if let Some(level) = normalize_level(Some(parts[2])) {
            record.source = "pymss".into();
            record.level = Some(level);
            record.logger = parts[1].into();
            record.message = parts[3].into();
            if let Ok(time) =
                chrono::NaiveDateTime::parse_from_str(parts[0], "%Y-%m-%d %H:%M:%S,%f")
            {
                use chrono::TimeZone;
                if let Some(time) = chrono::Local.from_local_datetime(&time).single() {
                    record.timestamp = time
                        .with_timezone(&chrono::Utc)
                        .to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
                }
            }
        }
    }
    // pymss also emits rich-style [LEVEL] prefixes without a logger name.
    if record.level.is_none() {
        for level in [
            "TRACE", "DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL",
        ] {
            if let Some(message) = clean.strip_prefix(&format!("[{level}] ")) {
                record.level = normalize_level(Some(level));
                record.logger = "python".into();
                record.message = message.into();
                break;
            }
        }
    }
    record
}

pub(crate) fn strip_ansi(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    let mut chars = value.chars();
    while let Some(ch) = chars.next() {
        if ch == '\u{1b}' {
            match chars.next() {
                Some('[') => {
                    for ch in chars.by_ref() {
                        if ('@'..='~').contains(&ch) {
                            break;
                        }
                    }
                }
                Some(']') => {
                    while let Some(ch) = chars.next() {
                        if ch == '\u{7}' || (ch == '\u{1b}' && chars.next() == Some('\\')) {
                            break;
                        }
                    }
                }
                _ => {}
            }
        } else if ch != '\0' {
            output.push(ch);
        }
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn producer_fields_survive_protocol_ingestion() {
        let mut original = LogRecord::new(
            "pymss",
            Some("WARNING"),
            "pymss.graph",
            "model.load",
            "Loading model",
        );
        original.timestamp = "2026-10-19T20:35:00+08:00".into();
        original.detail = Some("Traceback\n  model.py:1".into());
        original.location = Some(LogLocation {
            file: "model.py".into(),
            line: Some(1),
            function: Some("load".into()),
        });
        let parsed = LogRecord::from_payload(&serde_json::to_value(&original).unwrap()).unwrap();
        assert_eq!(parsed.id, original.id);
        assert_eq!(parsed.source, "pymss");
        assert_eq!(parsed.level.as_deref(), Some("warn"));
        assert_eq!(parsed.timestamp, "2026-10-19T12:35:00Z");
        assert_eq!(parsed.detail, original.detail);
        assert_eq!(parsed.location.unwrap().line, Some(1));
    }

    #[test]
    fn stderr_only_uses_recognized_levels() {
        let record = legacy_stderr(
            "\u{1b}[31m2026-10-19 20:35:00,123 - pymss.graph - ERROR - Allocation failed\u{1b}[0m",
        );
        assert_eq!(record.source, "pymss");
        assert_eq!(record.level.as_deref(), Some("error"));
        assert_eq!(record.message, "Allocation failed");
        assert!(legacy_stderr("Downloading 50%...").level.is_none());
        assert!(legacy_stderr("this says ERROR but has no level")
            .level
            .is_none());
        let record = legacy_stderr(
            "20:35:00 | WAR | C:\\app\\site-packages\\pymss\\graph\\core.py:780 | Low memory",
        );
        assert_eq!(record.source, "pymss");
        assert_eq!(record.level.as_deref(), Some("warn"));
        assert_eq!(record.message, "Low memory");
        assert_eq!(record.location.unwrap().line, Some(780));
    }
}
