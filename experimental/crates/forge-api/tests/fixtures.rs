use forge_api::{
    ApiErrorBody, Approval, DurableRun, ProviderList, RunEvent, RunStartResult, RuntimeRunSnapshot,
    StopRunResult,
};

fn fixture(name: &str) -> &'static str {
    match name {
        "runtime-runs" => include_str!("../../../../contracts/fixtures/runtime-runs.json"),
        "durable-run" => include_str!("../../../../contracts/fixtures/durable-run.json"),
        "events" => include_str!("../../../../contracts/fixtures/events.json"),
        "approvals" => include_str!("../../../../contracts/fixtures/approvals.json"),
        "providers" => include_str!("../../../../contracts/fixtures/providers.json"),
        "run-start" => include_str!("../../../../contracts/fixtures/run-start.json"),
        "run-stop" => include_str!("../../../../contracts/fixtures/run-stop.json"),
        "errors" => include_str!("../../../../contracts/fixtures/errors.json"),
        _ => panic!("unknown fixture"),
    }
}

#[test]
fn control_plane_fixtures_decode_into_distinct_types() {
    let snapshots: Vec<RuntimeRunSnapshot> =
        serde_json::from_str(fixture("runtime-runs")).expect("runtime snapshots");
    let durable: DurableRun = serde_json::from_str(fixture("durable-run")).expect("durable run");
    let events: Vec<RunEvent> = serde_json::from_str(fixture("events")).expect("events");
    let approvals: Vec<Approval> = serde_json::from_str(fixture("approvals")).expect("approvals");
    let providers: ProviderList = serde_json::from_str(fixture("providers")).expect("providers");
    let start: RunStartResult = serde_json::from_str(fixture("run-start")).expect("start result");
    let stop: StopRunResult = serde_json::from_str(fixture("run-stop")).expect("stop result");
    let errors: serde_json::Value = serde_json::from_str(fixture("errors")).expect("error fixture");
    let unauthorized: ApiErrorBody =
        serde_json::from_value(errors["unauthorized"].clone()).expect("error body");

    assert_eq!(snapshots[0].id, durable.id);
    assert_eq!(events[0].run_id, durable.id);
    assert_eq!(approvals[0].run_id, durable.id);
    assert_eq!(start.run_id, durable.id);
    assert_eq!(stop.project_id, durable.project_id);
    assert!(providers.profiles.contains_key("executor"));
    assert_eq!(unauthorized.detail, "invalid control-plane credential");
}
