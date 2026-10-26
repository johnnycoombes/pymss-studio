use super::*;

fn update(id: u64, completed: u64, total: u64) -> Value {
    json!({"id":id,"event":"render_progress","payload":{"phase":"rendering","completed":completed,"total":total}})
}

#[test]
fn render_progress_is_delivered_before_the_terminal_response() {
    let mut frames = [
        update(7, 10, 100),
        update(7, 60, 100),
        json!({"id":7,"ok":true,"result":{"path":"mix.wav"}}),
    ]
    .into_iter();
    let mut progress = Vec::new();
    let result = receive_render_result(
        7,
        std::time::Instant::now(),
        Duration::from_secs(2),
        |_| Ok(frames.next().unwrap()),
        |event| {
            progress.push(event.completed);
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(progress, [10, 60]);
    assert_eq!(result["path"], "mix.wav");
    assert!(
        decode_response(update(7, 10, 100), 7).is_err(),
        "ordinary RPCs must not accept progress as success"
    );
}

#[test]
fn malformed_or_foreign_progress_is_rejected() {
    let mut cases = vec![update(8, 1, 100), update(7, 1, 0), update(7, 101, 100)];
    let mut mixed = update(7, 1, 100);
    mixed["ok"] = json!(true);
    cases.push(mixed);
    let mut unknown = update(7, 1, 100);
    unknown["event"] = json!("unknown");
    cases.push(unknown);
    let mut invalid = update(7, 1, 100);
    invalid["payload"]["phase"] = json!("unknown");
    cases.push(invalid);
    let mut invalid = update(7, 1, 100);
    invalid["payload"]["completed"] = json!(0.5);
    cases.push(invalid);
    for response in cases {
        assert!(matches!(
            decode_render_progress(&response, 7),
            Err(HostRequestError::Transport(_))
        ));
    }
}

#[test]
fn continuous_progress_cannot_extend_the_absolute_timeout() {
    let started = std::time::Instant::now();
    let result = receive_render_result(
        7,
        started,
        Duration::from_millis(40),
        |_| {
            std::thread::sleep(Duration::from_millis(3));
            Ok(update(7, 1, 100))
        },
        |_| Ok(()),
    );
    assert!(
        matches!(result, Err(HostRequestError::Transport(message)) if message.contains("timed out"))
    );
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[test]
fn cancellation_stops_consuming_progress_and_terminal_errors_are_not_success() {
    let mut reads = 0;
    let result = receive_render_result(
        7,
        std::time::Instant::now(),
        Duration::from_secs(2),
        |_| {
            reads += 1;
            Ok(update(7, 1, 100))
        },
        |_| Err(HostRequestError::Operation("Rendering cancelled".into())),
    );
    assert!(matches!(result, Err(HostRequestError::Operation(_))));
    assert_eq!(reads, 1);
    let error = receive_render_result(
        7,
        std::time::Instant::now(),
        Duration::from_secs(2),
        |_| Ok(json!({"id":7,"ok":false,"error":"Render failed"})),
        |_| Ok(()),
    );
    assert!(
        matches!(error, Err(HostRequestError::Operation(message)) if message == "Render failed")
    );
}
