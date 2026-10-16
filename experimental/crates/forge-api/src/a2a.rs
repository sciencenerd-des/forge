use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};

use crate::{
    client::{ApiError, ForgeApi},
    types::ProjectId,
};

/// One A2A task as returned by every task-shaped RPC result
/// (`message/send`, `tasks/get`, `tasks/cancel`).
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct A2aTask {
    pub id: String,
    #[serde(rename = "contextId")]
    pub context_id: ProjectId,
    pub status: A2aTaskStatus,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct A2aTaskStatus {
    pub state: String,
}

#[derive(Debug, Deserialize)]
struct RpcEnvelope {
    jsonrpc: String,
    id: u64,
    #[serde(default)]
    result: Option<serde_json::Value>,
    #[serde(default)]
    error: Option<RpcError>,
}

static NEXT_REQUEST_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
}

impl ForgeApi {
    /// Public discovery document; the only unauthenticated A2A surface.
    pub async fn agent_card(&self) -> Result<serde_json::Value, ApiError> {
        self.get_json(".well-known/agent-card.json", false).await
    }

    pub async fn a2a_send(&self, project_id: &ProjectId, text: &str) -> Result<A2aTask, ApiError> {
        self.a2a_rpc(
            "message/send",
            serde_json::json!({
                "metadata": {"project_id": project_id.0},
                "message": {"parts": [{"text": text}]},
            }),
        )
        .await
    }

    pub async fn a2a_get(&self, task_id: &str) -> Result<A2aTask, ApiError> {
        self.a2a_rpc("tasks/get", serde_json::json!({"id": task_id}))
            .await
    }

    pub async fn a2a_cancel(&self, task_id: &str) -> Result<A2aTask, ApiError> {
        self.a2a_rpc("tasks/cancel", serde_json::json!({"id": task_id}))
            .await
    }

    async fn a2a_rpc<T: serde::de::DeserializeOwned>(
        &self,
        method: &str,
        params: serde_json::Value,
    ) -> Result<T, ApiError> {
        let id = NEXT_REQUEST_ID.fetch_add(1, Ordering::Relaxed);
        let envelope: RpcEnvelope = self
            .send_json(
                reqwest::Method::POST,
                "a2a",
                &serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "method": method,
                    "params": params,
                }),
            )
            .await?;
        if envelope.jsonrpc != "2.0" {
            return Err(ApiError::RpcProtocol(format!(
                "expected jsonrpc 2.0, received {}",
                envelope.jsonrpc
            )));
        }
        if envelope.id != id {
            return Err(ApiError::RpcProtocol(format!(
                "response id {} did not match request id {id}",
                envelope.id
            )));
        }
        if envelope.result.is_some() == envelope.error.is_some() {
            return Err(ApiError::RpcProtocol(
                "response must contain exactly one of result or error".into(),
            ));
        }
        if let Some(error) = envelope.error {
            return Err(ApiError::Rpc {
                code: error.code,
                message: error.message,
            });
        }
        let result = envelope.result.ok_or_else(|| ApiError::Rpc {
            code: -32603,
            message: "JSON-RPC response carried neither result nor error".into(),
        })?;
        Ok(serde_json::from_value(result)?)
    }
}
