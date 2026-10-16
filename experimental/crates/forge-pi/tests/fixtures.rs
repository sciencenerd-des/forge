use forge_pi::{JsonlDecoder, PiIncoming};

fn decode(fixture: &[u8]) -> Vec<PiIncoming> {
    let mut decoder = JsonlDecoder::default();
    decoder
        .push(fixture)
        .expect("frame fixture")
        .into_iter()
        .map(|value| serde_json::from_value(value).expect("decode fixture record"))
        .collect()
}

#[test]
fn pinned_pi_fixture_decodes() {
    let events = decode(include_bytes!("fixtures/get-state-0.80.2.jsonl"));
    assert!(matches!(
        events.first(),
        Some(PiIncoming::Response { success: true, .. })
    ));
}

#[test]
fn pinned_pi_session_stats_fixture_carries_footer_fields() {
    let events = decode(include_bytes!("fixtures/get-session-stats-0.80.2.jsonl"));
    let Some(PiIncoming::Response { success, data, .. }) = events.first() else {
        panic!("expected a response record");
    };
    assert!(success);
    // The exact fields the TUI session footer reads.
    assert!(data["contextUsage"]["percent"].is_number());
    assert!(data["cost"].is_number());
    assert!(data["tokens"]["total"].is_number());
}

#[test]
fn pinned_pi_steer_fixture_interleaves_queue_update_before_response() {
    let events = decode(include_bytes!("fixtures/steer-0.80.2.jsonl"));
    // Pi emits the queue_update event before acknowledging the command; the
    // client must tolerate events interleaved ahead of correlated responses.
    assert!(matches!(
        events.first(),
        Some(PiIncoming::QueueUpdate { .. })
    ));
    assert!(matches!(
        events.get(1),
        Some(PiIncoming::Response { success: true, .. })
    ));
}
