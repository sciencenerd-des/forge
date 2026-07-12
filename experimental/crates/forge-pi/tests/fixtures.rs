use forge_pi::{JsonlDecoder, PiIncoming};

#[test]
fn pinned_pi_fixture_decodes() {
    let mut decoder = JsonlDecoder::default();
    let events = decoder
        .push(include_bytes!("fixtures/get-state-0.80.2.jsonl"))
        .expect("frame fixture");
    let response: PiIncoming =
        serde_json::from_value(events.into_iter().next().expect("record")).expect("decode fixture");
    assert!(matches!(
        response,
        PiIncoming::Response { success: true, .. }
    ));
}
