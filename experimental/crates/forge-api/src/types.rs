use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// Identifiers are deliberately distinct so project-addressed stop requests
/// cannot accidentally receive a run id.
#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct ProjectId(pub String);

#[derive(Clone, Debug, Deserialize, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct RunId(pub String);

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RuntimeRunSnapshot {
    pub id: RunId,
    pub project_id: ProjectId,
    #[serde(default)]
    pub project_name: String,
    #[serde(default)]
    pub goal_id: Option<String>,
    #[serde(default)]
    pub goal_title: String,
    pub status: String,
    #[serde(default)]
    pub current_node: String,
    #[serde(default)]
    pub batch: u32,
    #[serde(default)]
    pub pid: Option<u32>,
    #[serde(default)]
    pub updated_at: Option<String>,
    #[serde(default)]
    pub log_tail: Vec<String>,
    #[serde(default)]
    pub model: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RuntimeProjectSnapshot {
    pub id: ProjectId,
    pub name: String,
    pub repo_path: String,
    #[serde(default)]
    pub goal_count: u32,
    #[serde(default)]
    pub active_goal: Option<String>,
    #[serde(default)]
    pub active_goal_status: Option<String>,
    #[serde(default)]
    pub task_total: u32,
    #[serde(default)]
    pub task_completed: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct DurableRun {
    pub id: RunId,
    pub project_id: ProjectId,
    pub goal_id: String,
    #[serde(default)]
    pub provider_id: Option<String>,
    pub status: String,
    pub current_node: String,
    pub turn: u32,
    pub max_turns: u32,
    #[serde(default)]
    pub lease_owner: Option<String>,
    #[serde(default)]
    pub lease_token: Option<String>,
    #[serde(default)]
    pub lease_expires_at: Option<String>,
    #[serde(default)]
    pub heartbeat_at: Option<String>,
    #[serde(default)]
    pub terminal_reason: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RunEvent {
    pub id: String,
    pub run_id: RunId,
    pub sequence: u64,
    pub event_type: String,
    pub actor: String,
    #[serde(default)]
    pub payload: serde_json::Value,
    pub created_at: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Approval {
    pub id: String,
    pub run_id: RunId,
    pub action_type: String,
    pub action_digest: String,
    #[serde(default)]
    pub action_preview: serde_json::Value,
    pub risk: String,
    pub status: String,
    pub requested_by: String,
    #[serde(default)]
    pub decided_by: Option<String>,
    #[serde(default)]
    pub decision_reason: Option<String>,
    pub expires_at: String,
    #[serde(default)]
    pub decided_at: Option<String>,
    #[serde(default)]
    pub consumed_at: Option<String>,
    pub created_at: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ProviderProfile {
    #[serde(default)]
    pub base_url: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub api_key: Option<String>,
    #[serde(default)]
    pub auth_mode: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ProviderList {
    pub version: u32,
    pub profiles: BTreeMap<String, ProviderProfile>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ProviderUpdate {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub base_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub api_key: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub auth_mode: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RuntimeRunStart {
    pub goal: String,
    #[serde(default)]
    pub description: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub project_id: Option<ProjectId>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RunStartResult {
    pub status: String,
    pub started: bool,
    pub run_id: RunId,
    #[serde(default)]
    pub pid: Option<u32>,
    #[serde(default)]
    pub log: Option<String>,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub note: Option<String>,
    #[serde(default)]
    pub already_running: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct StopRunResult {
    pub status: String,
    pub project_id: ProjectId,
    #[serde(default)]
    pub pid: Option<u32>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ApprovalDecision {
    pub actor: String,
    pub approved: bool,
    pub reason: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct OfflineRunManifest {
    pub run_id: RunId,
    pub project_id: ProjectId,
    pub status: String,
    #[serde(default)]
    pub batch: u32,
    #[serde(default)]
    pub pid: Option<u32>,
    #[serde(default)]
    pub updated_at: Option<String>,
    #[serde(default)]
    pub log: Option<String>,
}

pub type OfflineRunManifests = BTreeMap<String, OfflineRunManifest>;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ApiErrorBody {
    pub detail: String,
}
