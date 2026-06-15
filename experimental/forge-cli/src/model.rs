use std::path::PathBuf;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Project {
    pub id: Uuid,
    pub name: String,
    pub path: PathBuf,
    pub created_at: DateTime<Utc>,
}

impl Project {
    pub fn new(name: String, path: PathBuf) -> Self {
        Self {
            id: Uuid::new_v4(),
            name,
            path,
            created_at: Utc::now(),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Goal {
    pub id: Uuid,
    pub project_id: Uuid,
    pub title: String,
    pub description: String,
    pub status: String,
    pub created_at: DateTime<Utc>,
}

impl Goal {
    pub fn new(project_id: Uuid, title: String, description: String) -> Self {
        Self {
            id: Uuid::new_v4(),
            project_id,
            title,
            description,
            status: "active".into(),
            created_at: Utc::now(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunStatus {
    Queued,
    Running,
    Paused,
    Blocked,
    Completed,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Run {
    pub id: Uuid,
    pub project_id: Uuid,
    pub goal_id: Uuid,
    #[serde(default)]
    pub provider_id: Option<Uuid>,
    pub status: RunStatus,
    pub current_node: String,
    pub turn: u32,
    pub max_turns: u32,
    pub started_at: Option<DateTime<Utc>>,
    pub updated_at: DateTime<Utc>,
}

impl Run {
    pub fn new(project_id: Uuid, goal_id: Uuid, provider_id: Option<Uuid>) -> Self {
        Self {
            id: Uuid::new_v4(),
            project_id,
            goal_id,
            provider_id,
            status: RunStatus::Queued,
            current_node: "planner".into(),
            turn: 0,
            max_turns: 90,
            started_at: None,
            updated_at: Utc::now(),
        }
    }

    pub fn set_status(&mut self, status: RunStatus) {
        self.status = status;
        self.updated_at = Utc::now();
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub id: Uuid,
    pub run_id: Uuid,
    pub source: String,
    pub message: String,
    pub created_at: DateTime<Utc>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ProviderKind {
    CodexSubscription,
    CloudApi {
        base_url: String,
        api_key_env: String,
    },
    LocalOpenAi {
        base_url: String,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ProviderProfile {
    pub id: Uuid,
    pub name: String,
    pub model: String,
    pub kind: ProviderKind,
    pub created_at: DateTime<Utc>,
}

impl ProviderProfile {
    pub fn new(name: String, model: String, kind: ProviderKind) -> Self {
        Self {
            id: Uuid::new_v4(),
            name,
            model,
            kind,
            created_at: Utc::now(),
        }
    }
}

impl Event {
    pub fn created(run_id: Uuid, source: &str, message: &str) -> Self {
        Self {
            id: Uuid::new_v4(),
            run_id,
            source: source.into(),
            message: message.into(),
            created_at: Utc::now(),
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct HarnessState {
    pub projects: Vec<Project>,
    pub goals: Vec<Goal>,
    pub runs: Vec<Run>,
    pub events: Vec<Event>,
    pub providers: Vec<ProviderProfile>,
    pub default_provider_id: Option<Uuid>,
}
