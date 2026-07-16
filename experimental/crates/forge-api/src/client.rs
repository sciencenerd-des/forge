use std::time::Duration;

use reqwest::{Method, StatusCode};
use thiserror::Error;

use crate::{
    config::ForgeConfig,
    sse::{SseEvent, decode_sse},
    types::{
        ApiErrorBody, Approval, ApprovalDecision, DurableRun, ProjectId, ProviderList,
        ProviderProfile, ProviderUpdate, RunEvent, RunId, RunStartResult, RuntimeProjectSnapshot,
        RuntimeRunSnapshot, RuntimeRunStart, StopRunResult,
    },
};

#[derive(Clone)]
pub struct ForgeApi {
    client: reqwest::Client,
    base_url: reqwest::Url,
    token: String,
}

#[derive(Debug, Error)]
pub enum ApiError {
    #[error("request failed: {0}")]
    Transport(#[from] reqwest::Error),
    #[error("API returned {status}: {detail}")]
    Response { status: StatusCode, detail: String },
    #[error("API returned invalid JSON: {0}")]
    Decode(#[from] serde_json::Error),
    #[error("SSE stream failed: {0}")]
    Stream(String),
    #[error("A2A JSON-RPC error {code}: {message}")]
    Rpc { code: i64, message: String },
    #[error("invalid A2A JSON-RPC response: {0}")]
    RpcProtocol(String),
}

impl ForgeApi {
    pub fn new(config: &ForgeConfig) -> Result<Self, ApiError> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(20))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self {
            client,
            base_url: config.control_url.clone(),
            token: config.control_token.clone(),
        })
    }

    pub async fn health(&self) -> Result<serde_json::Value, ApiError> {
        self.get_json("health", false).await
    }

    /// Verify that the endpoint both speaks Forge's protected API and accepts
    /// this installation's token before the TUI trusts an existing listener.
    pub async fn authenticated_ready(&self) -> Result<(), ApiError> {
        self.runtime_projects().await.map(|_| ())
    }

    pub async fn runtime_runs(&self) -> Result<Vec<RuntimeRunSnapshot>, ApiError> {
        self.get_json("runtime/runs", true).await
    }

    pub async fn runtime_projects(&self) -> Result<Vec<RuntimeProjectSnapshot>, ApiError> {
        self.get_json("runtime/projects", true).await
    }

    pub async fn start_run(&self, body: &RuntimeRunStart) -> Result<RunStartResult, ApiError> {
        self.send_json(Method::POST, "runtime/runs/start", body)
            .await
    }

    pub async fn stop_run(&self, project_id: &ProjectId) -> Result<StopRunResult, ApiError> {
        self.send_empty(Method::POST, &format!("runtime/runs/{}/stop", project_id.0))
            .await
    }

    pub async fn run(&self, run_id: &RunId) -> Result<DurableRun, ApiError> {
        self.get_json(&format!("runs/{}", run_id.0), true).await
    }

    pub async fn events(&self, run_id: &RunId, after: u64) -> Result<Vec<RunEvent>, ApiError> {
        self.get_json(&format!("runs/{}/events?after={after}", run_id.0), true)
            .await
    }

    pub async fn approvals(&self, status: Option<&str>) -> Result<Vec<Approval>, ApiError> {
        let suffix = status.map_or_else(String::new, |status| format!("?status={status}"));
        self.get_json(&format!("approvals{suffix}"), true).await
    }

    pub async fn decide_approval(
        &self,
        id: &str,
        body: &ApprovalDecision,
    ) -> Result<Approval, ApiError> {
        self.send_json(Method::POST, &format!("approvals/{id}/decision"), body)
            .await
    }

    pub async fn providers(&self) -> Result<ProviderList, ApiError> {
        self.get_json("providers", true).await
    }

    pub async fn put_provider(
        &self,
        role: &str,
        body: &ProviderUpdate,
    ) -> Result<ProviderProfile, ApiError> {
        self.send_json(Method::PUT, &format!("providers/{role}"), body)
            .await
    }

    pub async fn remove_provider(&self, role: &str) -> Result<(), ApiError> {
        self.send_empty::<serde_json::Value>(Method::DELETE, &format!("providers/{role}"))
            .await
            .map(|_| ())
    }

    pub async fn test_provider(
        &self,
        role: &str,
        body: &ProviderUpdate,
    ) -> Result<serde_json::Value, ApiError> {
        self.send_json(Method::POST, &format!("providers/{role}/test"), body)
            .await
    }

    /// Establish a fresh authenticated event stream. Reconnect policy belongs
    /// to the caller so UIs can stop it immediately during shutdown.
    pub async fn event_stream(
        &self,
        run_id: &RunId,
        after: u64,
    ) -> Result<impl futures_util::Stream<Item = Result<SseEvent, ApiError>>, ApiError> {
        let response = self
            .request(
                Method::GET,
                &format!("runs/{}/events/stream?after={after}", run_id.0),
                true,
            )
            .send()
            .await?;
        let response = self.require_success(response).await?;
        Ok(decode_sse(response.bytes_stream()))
    }

    pub(crate) async fn get_json<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        authenticated: bool,
    ) -> Result<T, ApiError> {
        let response = self
            .request(Method::GET, path, authenticated)
            .send()
            .await?;
        let response = self.require_success(response).await?;
        Ok(response.json().await?)
    }

    pub(crate) async fn send_json<T: serde::Serialize, R: serde::de::DeserializeOwned>(
        &self,
        method: Method,
        path: &str,
        body: &T,
    ) -> Result<R, ApiError> {
        let response = self.request(method, path, true).json(body).send().await?;
        let response = self.require_success(response).await?;
        Ok(response.json().await?)
    }

    async fn send_empty<R: serde::de::DeserializeOwned>(
        &self,
        method: Method,
        path: &str,
    ) -> Result<R, ApiError> {
        let response = self.request(method, path, true).send().await?;
        let response = self.require_success(response).await?;
        if response.status() == StatusCode::NO_CONTENT {
            return serde_json::from_value(serde_json::Value::Null).map_err(ApiError::Decode);
        }
        Ok(response.json().await?)
    }

    fn request(&self, method: Method, path: &str, authenticated: bool) -> reqwest::RequestBuilder {
        let url = self
            .base_url
            .join(path)
            .expect("control-plane paths are static and valid");
        let request = self.client.request(method, url);
        if authenticated {
            request.bearer_auth(&self.token)
        } else {
            request
        }
    }

    async fn require_success(
        &self,
        response: reqwest::Response,
    ) -> Result<reqwest::Response, ApiError> {
        if response.status().is_success() {
            return Ok(response);
        }
        let status = response.status();
        let text = response.text().await.unwrap_or_default();
        let detail = serde_json::from_str::<ApiErrorBody>(&text)
            .map(|body| body.detail)
            .unwrap_or(text);
        Err(ApiError::Response { status, detail })
    }
}
