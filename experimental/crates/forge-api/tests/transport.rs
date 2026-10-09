use axum::{
    Json, Router,
    body::Body,
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use forge_api::{ApiError, ForgeApi, ForgeConfig, ProjectId};
use futures_util::StreamExt;
use serde_json::Value;
use std::sync::Arc;

#[derive(Clone, Copy)]
enum A2aMode {
    Valid,
    MismatchedId,
}

async fn stop(Path(project_id): Path<String>, headers: HeaderMap) -> impl IntoResponse {
    if headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        != Some("Bearer fixture-token")
    {
        return (
            StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({"detail": "invalid control-plane credential"})),
        )
            .into_response();
    }
    if project_id == "busy" {
        return (
            StatusCode::CONFLICT,
            Json(serde_json::json!({"detail": "already stopping"})),
        )
            .into_response();
    }
    (
        StatusCode::OK,
        Json(serde_json::json!({"status": "stopped", "project_id": project_id, "pid": 17})),
    )
        .into_response()
}

async fn events(headers: HeaderMap) -> Response {
    if headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        != Some("Bearer fixture-token")
    {
        return (
            StatusCode::UNAUTHORIZED,
            Json(serde_json::json!({"detail":"invalid control-plane credential"})),
        )
            .into_response();
    }
    let payload = "id: 7\r\nevent: run.running\r\ndata: {\"sequence\":7}\r\n\r\n";
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "text/event-stream")
        .body(Body::from(payload))
        .expect("SSE response")
}

async fn a2a(
    State(mode): State<Arc<A2aMode>>,
    headers: HeaderMap,
    Json(request): Json<Value>,
) -> Response {
    if headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        != Some("Bearer fixture-token")
    {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    let id = request["id"].as_u64().expect("numeric request id");
    let response_id = match *mode {
        A2aMode::Valid => id,
        A2aMode::MismatchedId => id + 1,
    };
    Json(serde_json::json!({
        "jsonrpc": "2.0",
        "id": response_id,
        "result": {"id":"task-1", "contextId":"project-a", "status":{"state":"working"}}
    }))
    .into_response()
}

async fn server() -> (String, tokio::task::JoinHandle<()>) {
    server_with_a2a(A2aMode::Valid).await
}

async fn server_with_a2a(mode: A2aMode) -> (String, tokio::task::JoinHandle<()>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let task = tokio::spawn(async move {
        axum::serve(
            listener,
            Router::new()
                .route("/runtime/runs/{project_id}/stop", post(stop))
                .route("/runs/{run_id}/events/stream", get(events))
                .route("/a2a", post(a2a))
                .with_state(Arc::new(mode)),
        )
        .await
        .expect("serve");
    });
    (format!("http://{address}/"), task)
}

#[tokio::test]
async fn a2a_requires_bearer_auth_and_matching_json_rpc_id() {
    let (url, task) = server().await;
    let config = ForgeConfig {
        home: std::env::temp_dir(),
        control_url: url.parse().expect("url"),
        control_token: "fixture-token".into(),
    };
    let api = ForgeApi::new(&config).expect("api");
    let result = api
        .a2a_send(&ProjectId("project-a".into()), "do the work")
        .await
        .expect("valid A2A result");
    assert_eq!(result.id, "task-1");
    task.abort();

    let (url, task) = server_with_a2a(A2aMode::MismatchedId).await;
    let config = ForgeConfig {
        home: std::env::temp_dir(),
        control_url: url.parse().expect("url"),
        control_token: "fixture-token".into(),
    };
    let api = ForgeApi::new(&config).expect("api");
    assert!(matches!(
        api.a2a_get("task-1").await,
        Err(ApiError::RpcProtocol(message)) if message.contains("did not match")
    ));
    task.abort();
}

#[tokio::test]
async fn sends_bearer_auth_and_preserves_typed_conflicts() {
    let (url, task) = server().await;
    let config = ForgeConfig {
        home: std::env::temp_dir(),
        control_url: url.parse().expect("url"),
        control_token: "fixture-token".into(),
    };
    let api = ForgeApi::new(&config).expect("api");
    let stopped = api
        .stop_run(&ProjectId("project-a".into()))
        .await
        .expect("stop result");
    assert_eq!(stopped.status, "stopped");
    match api
        .stop_run(&ProjectId("busy".into()))
        .await
        .expect_err("conflict")
    {
        ApiError::Response { status, detail } => {
            assert_eq!(status, StatusCode::CONFLICT);
            assert_eq!(detail, "already stopping");
        }
        error => panic!("unexpected error: {error}"),
    }
    task.abort();
}

#[tokio::test]
async fn parses_authenticated_crlf_sse_events() {
    let (url, task) = server().await;
    let config = ForgeConfig {
        home: std::env::temp_dir(),
        control_url: url.parse().expect("url"),
        control_token: "fixture-token".into(),
    };
    let api = ForgeApi::new(&config).expect("api");
    let run_id = forge_api::RunId("run-1".into());
    let mut events = Box::pin(api.event_stream(&run_id, 0).await.expect("event stream"));
    let event = events
        .next()
        .await
        .expect("event")
        .expect("valid SSE event");
    assert_eq!(event.id.as_deref(), Some("7"));
    assert_eq!(event.event, "run.running");
    assert_eq!(event.data, "{\"sequence\":7}");
    task.abort();
}
