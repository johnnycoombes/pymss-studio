//! Failed metadata probes are coalesced; explicit loading always gets a fresh attempt.

use std::collections::HashSet;

/// Apple's AUv2 bridge uses asynchronous, out-of-process instantiation for units
/// advertising RequiresAsyncInstantiation, including those without the IsV3 bit.
pub(crate) fn async_options(flags: u32) -> Option<u32> {
    (flags & (4 | 8) != 0).then_some(1)
}

#[derive(Clone, Copy)]
pub(crate) enum Attempt {
    Explicit,
    Metadata,
}

#[derive(Default)]
pub(crate) struct Attempts {
    failed: HashSet<String>,
}

impl Attempts {
    pub(crate) fn allowed(&self, id: &str, attempt: Attempt) -> bool {
        matches!(attempt, Attempt::Explicit) || !self.failed.contains(id)
    }
    pub(crate) fn completed(&mut self, id: &str, success: bool) {
        if success {
            self.failed.remove(id);
        } else {
            self.failed.insert(id.to_owned());
        }
    }
    pub(crate) fn clear(&mut self) {
        self.failed.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn asynchronous_components_use_isolation_independently_of_the_v3_identity_bit() {
        for flags in [4, 8, 12, 8 | 16] {
            assert_eq!(async_options(flags), Some(1));
        }
        for flags in [0, 1, 2, 16] {
            assert_eq!(async_options(flags), None);
        }
    }
    #[test]
    fn explicit_retry_recovers_after_failure_and_reenables_metadata() {
        let mut attempts = Attempts::default();
        let id = "au:aufx:dely:appl";
        assert!(attempts.allowed(id, Attempt::Metadata));
        attempts.completed(id, false);
        assert!(!attempts.allowed(id, Attempt::Metadata));
        assert!(attempts.allowed(id, Attempt::Explicit));
        attempts.completed(id, true);
        assert!(attempts.allowed(id, Attempt::Metadata));
        attempts.completed(id, false);
        attempts.clear();
        assert!(attempts.allowed(id, Attempt::Metadata));
    }
}
