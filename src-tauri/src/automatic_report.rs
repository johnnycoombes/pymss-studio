use std::time::Duration;

const REPORT_INTERVAL_MS: u64 = 5_000;

#[derive(Clone)]
pub(crate) struct PendingReport {
    pub(crate) generation: u64,
    sequence: u64,
    pub(crate) trigger: String,
    pub(crate) line: String,
}

pub(crate) enum ReportAction {
    Stop,
    Wait(Duration),
    Write(PendingReport),
}

#[derive(Default)]
pub(crate) struct AutomaticReports {
    generation: u64,
    minimum_sequence: u64,
    last_started_ms: Option<u64>,
    running: bool,
    stopped: bool,
    pending: Option<PendingReport>,
    latest: Option<PendingReport>,
}

impl AutomaticReports {
    /// Return true only for the caller responsible for starting the single writer.
    pub(crate) fn enqueue(&mut self, sequence: u64, trigger: &str, line: &str) -> bool {
        if self.stopped
            || sequence < self.minimum_sequence
            || self
                .latest
                .as_ref()
                .is_some_and(|request| request.sequence >= sequence)
        {
            return false;
        }
        let request = PendingReport {
            generation: self.generation,
            sequence,
            trigger: trigger.into(),
            line: line.into(),
        };
        self.latest = Some(request.clone());
        self.pending = Some(request);
        if self.running {
            false
        } else {
            self.running = true;
            true
        }
    }

    pub(crate) fn next(&mut self, now_ms: u64) -> ReportAction {
        if self.stopped || self.pending.is_none() {
            self.running = false;
            return ReportAction::Stop;
        }
        if let Some(last) = self.last_started_ms {
            let elapsed = now_ms.saturating_sub(last);
            if elapsed < REPORT_INTERVAL_MS {
                return ReportAction::Wait(Duration::from_millis(REPORT_INTERVAL_MS - elapsed));
            }
        }
        self.last_started_ms = Some(now_ms);
        ReportAction::Write(self.pending.take().expect("pending report checked above"))
    }

    pub(crate) fn accepts(&self, request: &PendingReport, finishing: bool) -> bool {
        request.generation == self.generation && (!self.stopped || finishing)
    }

    pub(crate) fn start_failed(&mut self) {
        self.running = false;
    }

    pub(crate) fn reset(&mut self, minimum_sequence: u64) {
        self.generation = self.generation.wrapping_add(1);
        self.minimum_sequence = minimum_sequence;
        self.last_started_ms = None;
        self.pending = None;
        self.latest = None;
        self.stopped = false;
        // An existing writer observes the new generation on its next iteration. Keeping
        // its reservation avoids starting another writer while the old one is finishing.
    }

    pub(crate) fn finish(&mut self) -> Option<PendingReport> {
        let outstanding = self.running || self.pending.is_some();
        self.stopped = true;
        self.pending = None;
        if outstanding {
            self.latest.take()
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_terminal_exception_is_written_after_an_earlier_error_without_detail() {
        let mut reports = AutomaticReports::default();
        assert!(reports.enqueue(0, "worker_log", "Model loading failed"));
        let ReportAction::Write(first) = reports.next(10_000) else {
            panic!("first error was not written")
        };
        assert_eq!(first.trigger, "worker_log");
        assert!(!reports.enqueue(1, "error", "Traceback:\nRuntimeError: checkpoint failed"));
        assert!(matches!(reports.next(10_010), ReportAction::Wait(_)));
        let ReportAction::Write(last) = reports.next(15_000) else {
            panic!("terminal error was dropped")
        };
        assert_eq!(last.trigger, "error");
        assert!(last.line.contains("Traceback:"));
        assert!(matches!(reports.next(15_001), ReportAction::Stop));
    }

    #[test]
    fn a_burst_keeps_only_the_latest_error_and_one_writer() {
        let mut reports = AutomaticReports::default();
        assert!(reports.enqueue(0, "first", "first"));
        for index in 0..100 {
            assert!(!reports.enqueue(index + 1, "error", &format!("failure-{index}")));
        }
        let ReportAction::Write(request) = reports.next(0) else {
            panic!("no report")
        };
        assert_eq!(request.line, "failure-99");
        assert!(matches!(reports.next(1), ReportAction::Stop));
        assert!(reports.enqueue(101, "error", "next burst"));
        assert!(matches!(reports.next(1), ReportAction::Wait(_)));
    }

    #[test]
    fn shutdown_flushes_the_latest_error_without_waiting_for_the_interval() {
        let mut reports = AutomaticReports::default();
        reports.enqueue(0, "worker_log", "first error");
        let ReportAction::Write(first) = reports.next(10_000) else {
            panic!("no first report")
        };
        reports.enqueue(1, "error", "complete traceback");
        let final_report = reports.finish().expect("pending exception must be flushed");
        assert_eq!(final_report.line, "complete traceback");
        assert!(reports.accepts(&final_report, true));
        assert!(!reports.accepts(&first, false));
        assert!(!reports.enqueue(2, "error", "after shutdown"));
        assert!(matches!(reports.next(10_001), ReportAction::Stop));
    }

    #[test]
    fn clearing_logs_invalidates_pending_and_in_flight_reports() {
        let mut reports = AutomaticReports::default();
        reports.enqueue(0, "error", "old report");
        let ReportAction::Write(old) = reports.next(10_000) else {
            panic!("no old report")
        };
        reports.reset(2);
        assert!(!reports.accepts(&old, false));
        assert!(!reports.enqueue(1, "error", "old error queued after clear"));
        assert!(!reports.enqueue(2, "error", "new report"));
        let ReportAction::Write(new) = reports.next(10_001) else {
            panic!("new report delayed by old session")
        };
        assert_eq!(new.line, "new report");
        assert!(reports.accepts(&new, false));
    }

    #[test]
    fn concurrent_delivery_order_does_not_replace_a_newer_exception() {
        let mut reports = AutomaticReports::default();
        assert!(reports.enqueue(8, "error", "complete traceback"));
        assert!(!reports.enqueue(7, "worker_log", "older error arriving late"));
        let ReportAction::Write(request) = reports.next(0) else {
            panic!("no report")
        };
        assert_eq!(request.line, "complete traceback");
    }

    #[test]
    fn a_delayed_error_at_the_clear_counter_cannot_recreate_the_deleted_report() {
        let mut reports = AutomaticReports::default();
        assert!(reports.enqueue(39, "error", "old error"));
        let ReportAction::Write(old) = reports.next(0) else {
            panic!("no old report")
        };
        // Log sequence numbers are allocated with fetch_add(...)+1. At clear, 40 is
        // already an old record even if its error-report enqueue has been delayed.
        reports.reset(40_u64.saturating_add(1));
        assert!(!reports.enqueue(40, "error", "old error queued after clear"));
        assert!(!reports.enqueue(41, "error", "first new error"));
        assert!(!reports.accepts(&old, false));
        let ReportAction::Write(new) = reports.next(1) else {
            panic!("no new report")
        };
        assert_eq!(new.sequence, 41);
        assert_eq!(new.line, "first new error");
    }
}
