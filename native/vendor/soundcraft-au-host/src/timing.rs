/// Preserve the unit's reported duration for graph validation. An absent optional property is
/// zero; invalid successful values stay visible as an over-limit sentinel.
pub(crate) fn seconds_to_samples(seconds: Option<f64>, sample_rate: f64) -> usize {
    let Some(seconds) = seconds else { return 0 };
    if !seconds.is_finite() || seconds < 0.0 || !sample_rate.is_finite() || sample_rate <= 0.0 {
        return usize::MAX;
    }
    let samples = seconds * sample_rate;
    if !samples.is_finite() {
        usize::MAX
    } else {
        samples.round() as usize
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_over_limit_latency_and_tail_for_graph_validation() {
        assert_eq!(seconds_to_samples(Some(11.0), 48_000.0), 528_000);
        assert_eq!(seconds_to_samples(Some(61.0), 48_000.0), 2_928_000);
        assert_eq!(seconds_to_samples(None, 48_000.0), 0);
        assert_eq!(seconds_to_samples(Some(0.0), 48_000.0), 0);
        assert_eq!(seconds_to_samples(Some(0.001), 48_000.0), 48);
    }

    #[test]
    fn invalid_successful_reports_are_not_mistaken_for_zero() {
        for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, -1.0, f64::MAX] {
            assert_eq!(seconds_to_samples(Some(value), 48_000.0), usize::MAX);
        }
        assert_eq!(seconds_to_samples(Some(1.0), f64::NAN), usize::MAX);
    }
}
