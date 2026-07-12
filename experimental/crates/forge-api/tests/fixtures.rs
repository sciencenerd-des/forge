use forge_api::{
    A2aTask, ApiErrorBody, Approval, DurableRun, ProviderList, RunEvent, RunStartResult,
    RuntimeProjectSnapshot, RuntimeRunSnapshot, StopRunResult,
};

fn fixture(name: &str) -> &'static str {
    match name {
        "runtime-runs" => include_str!("../../../../contracts/fixtures/runtime-runs.json"),
        "runtime-projects" => include_str!("../../../../contracts/fixtures/runtime-projects.json"),
        "durable-run" => include_str!("../../../../contracts/fixtures/durable-run.json"),
        "events" => include_str!("../../../../contracts/fixtures/events.json"),
        "approvals" => include_str!("../../../../contracts/fixtures/approvals.json"),
        "providers" => include_str!("../../../../contracts/fixtures/providers.json"),
        "run-start" => include_str!("../../../../contracts/fixtures/run-start.json"),
        "run-stop" => include_str!("../../../../contracts/fixtures/run-stop.json"),
        "errors" => include_str!("../../../../contracts/fixtures/errors.json"),
        "a2a-send" => include_str!("../../../../contracts/fixtures/a2a-send.json"),
        "a2a-get" => include_str!("../../../../contracts/fixtures/a2a-get.json"),
        "a2a-cancel" => include_str!("../../../../contracts/fixtures/a2a-cancel.json"),
        "a2a-errors" => include_str!("../../../../contracts/fixtures/a2a-errors.json"),
        "agent-card" => include_str!("../../../../contracts/fixtures/agent-card.json"),
        _ => panic!("unknown fixture"),
    }
}

#[test]
fn control_plane_fixtures_decode_into_distinct_types() {
    let snapshots: Vec<RuntimeRunSnapshot> =
        serde_json::from_str(fixture("runtime-runs")).expect("runtime snapshots");
    let projects: Vec<RuntimeProjectSnapshot> =
        serde_json::from_str(fixture("runtime-projects")).expect("runtime projects");
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
    assert_eq!(projects[0].id, durable.project_id);
    assert_eq!(events[0].run_id, durable.id);
    assert_eq!(approvals[0].run_id, durable.id);
    assert_eq!(start.run_id, durable.id);
    assert_eq!(stop.project_id, durable.project_id);
    assert!(providers.profiles.contains_key("executor"));
    assert_eq!(unauthorized.detail, "invalid control-plane credential");
}

#[test]
fn a2a_fixtures_decode_into_task_results() {
    for name in ["a2a-send", "a2a-get", "a2a-cancel"] {
        let envelope: serde_json::Value = serde_json::from_str(fixture(name)).expect(name);
        let task: A2aTask =
            serde_json::from_value(envelope["result"].clone()).expect("task result");
        assert_eq!(task.id, "66666666-6666-6666-6666-666666666666");
        assert_eq!(task.context_id.0, "22222222-2222-2222-2222-222222222222");
        assert!(!task.status.state.is_empty());
    }
    let cancel: serde_json::Value = serde_json::from_str(fixture("a2a-cancel")).expect("cancel");
    assert_eq!(cancel["result"]["status"]["state"], "canceled");
    let errors: serde_json::Value = serde_json::from_str(fixture("a2a-errors")).expect("errors");
    for case in ["task_not_found", "missing_project", "unknown_method"] {
        assert!(errors[case]["error"]["code"].is_i64(), "case: {case}");
        assert!(errors[case]["error"]["message"].is_string(), "case: {case}");
    }
    let card: serde_json::Value = serde_json::from_str(fixture("agent-card")).expect("card");
    assert!(
        card["supportedInterfaces"][0]["url"]
            .as_str()
            .is_some_and(|url| url.ends_with("/a2a"))
    );
}
