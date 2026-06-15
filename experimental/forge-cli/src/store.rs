use std::{
    fs,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, anyhow};
use directories::ProjectDirs;
use uuid::Uuid;

use crate::model::{Goal, HarnessState, Project, ProviderProfile, Run};

pub struct Store {
    path: PathBuf,
    pub state: HarnessState,
}

impl Store {
    pub fn open() -> Result<Self> {
        if let Some(path) = std::env::var_os("FORGE_STATE_PATH") {
            return Self::open_path(path.into());
        }
        let dirs = ProjectDirs::from("org", "forge-harness", "forge")
            .ok_or_else(|| anyhow!("could not resolve application data directory"))?;
        Self::open_path(dirs.data_local_dir().join("state.json"))
    }

    fn open_path(path: PathBuf) -> Result<Self> {
        let state = if path.exists() {
            parse_state(&fs::read(&path).context("read local harness state")?)?
        } else {
            HarnessState::default()
        };
        Ok(Self { path, state })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn save(&self) -> Result<()> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let temporary = self.path.with_extension("json.tmp");
        fs::write(&temporary, serde_json::to_vec_pretty(&self.state)?)?;
        fs::rename(temporary, &self.path)?;
        Ok(())
    }

    pub fn require_project(&self, id: Uuid) -> Result<&Project> {
        self.state
            .projects
            .iter()
            .find(|item| item.id == id)
            .ok_or_else(|| anyhow!("project {id} not found"))
    }

    pub fn require_goal(&self, id: Uuid) -> Result<&Goal> {
        self.state
            .goals
            .iter()
            .find(|item| item.id == id)
            .ok_or_else(|| anyhow!("goal {id} not found"))
    }

    pub fn require_run(&self, id: Uuid) -> Result<&Run> {
        self.state
            .runs
            .iter()
            .find(|item| item.id == id)
            .ok_or_else(|| anyhow!("run {id} not found"))
    }

    pub fn require_run_mut(&mut self, id: Uuid) -> Result<&mut Run> {
        self.state
            .runs
            .iter_mut()
            .find(|item| item.id == id)
            .ok_or_else(|| anyhow!("run {id} not found"))
    }

    pub fn require_provider(&self, id: Uuid) -> Result<&ProviderProfile> {
        self.state
            .providers
            .iter()
            .find(|item| item.id == id)
            .ok_or_else(|| anyhow!("provider {id} not found"))
    }
}

fn parse_state(bytes: &[u8]) -> Result<HarnessState> {
    match serde_json::from_slice(bytes) {
        Ok(state) => Ok(state),
        Err(original_error) => {
            let mut value: serde_json::Value = serde_json::from_slice(bytes)
                .with_context(|| format!("parse local harness state: {original_error}"))?;
            let Some(providers) = value
                .get_mut("providers")
                .and_then(|item| item.as_array_mut())
            else {
                return Err(original_error).context("parse local harness state");
            };
            let mut removed_ids = Vec::new();
            providers.retain_mut(|provider| {
                let provider_id = provider
                    .get("id")
                    .and_then(|item| item.as_str())
                    .map(str::to_owned);
                let kind = provider
                    .get_mut("kind")
                    .and_then(|item| item.as_object_mut());
                let kind_type = kind
                    .as_ref()
                    .and_then(|item| item.get("type"))
                    .and_then(|value| value.as_str())
                    .map(str::to_owned);
                match kind_type.as_deref() {
                    Some("subscription_cli") => {
                        let is_codex = kind
                            .as_ref()
                            .and_then(|item| item.get("command"))
                            .and_then(|item| item.as_str())
                            == Some("codex");
                        if is_codex {
                            if let Some(kind) = kind {
                                kind.clear();
                                kind.insert("type".into(), "codex_subscription".into());
                            }
                            true
                        } else {
                            if let Some(id) = provider_id {
                                removed_ids.push(id);
                            }
                            false
                        }
                    }
                    _ => true,
                }
            });
            if value
                .get("default_provider_id")
                .and_then(|item| item.as_str())
                .is_some_and(|id| removed_ids.iter().any(|removed| removed == id))
            {
                value["default_provider_id"] = serde_json::Value::Null;
            }
            if let Some(runs) = value.get_mut("runs").and_then(|item| item.as_array_mut()) {
                for run in runs {
                    if run
                        .get("provider_id")
                        .and_then(|item| item.as_str())
                        .is_some_and(|id| removed_ids.iter().any(|removed| removed == id))
                    {
                        run["provider_id"] = serde_json::Value::Null;
                    }
                }
            }
            serde_json::from_value(value).context("migrate legacy provider state")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Project, Run, RunStatus};

    #[test]
    fn persists_state_without_partial_writes() {
        let directory = tempfile::tempdir().expect("temp directory");
        let path = directory.path().join("state.json");
        let mut store = Store::open_path(path.clone()).expect("open store");
        let project = Project::new("fixture".into(), directory.path().into());
        store.state.projects.push(project);
        store.save().expect("save state");

        let reopened = Store::open_path(path).expect("reopen store");
        assert_eq!(reopened.state.projects.len(), 1);
    }

    #[test]
    fn mutates_an_existing_run_status() {
        let directory = tempfile::tempdir().expect("temp directory");
        let mut store = Store::open_path(directory.path().join("state.json")).expect("open store");
        let project_id = Uuid::new_v4();
        let goal_id = Uuid::new_v4();
        let run = Run::new(project_id, goal_id, None);
        let run_id = run.id;
        store.state.runs.push(run);

        store
            .require_run_mut(run_id)
            .expect("run")
            .set_status(RunStatus::Paused);
        assert_eq!(
            store.require_run(run_id).expect("run").status,
            RunStatus::Paused
        );
    }

    #[test]
    fn removes_legacy_claude_profiles_and_repairs_default() {
        let claude_id = Uuid::new_v4();
        let fixture = serde_json::json!({
            "providers": [{
                "id": claude_id,
                "name": "claude",
                "model": "removed",
                "kind": { "type": "subscription_cli", "command": "claude" },
                "created_at": "2026-06-11T00:00:00Z"
            }],
            "default_provider_id": claude_id,
            "runs": [{
                "id": Uuid::new_v4(),
                "project_id": Uuid::new_v4(),
                "goal_id": Uuid::new_v4(),
                "provider_id": claude_id,
                "status": "queued",
                "current_node": "planner",
                "turn": 0,
                "max_turns": 90,
                "started_at": null,
                "updated_at": "2026-06-11T00:00:00Z"
            }]
        });
        let state = parse_state(&serde_json::to_vec(&fixture).unwrap()).expect("migrate state");
        assert!(state.providers.is_empty());
        assert_eq!(state.default_provider_id, None);
        assert_eq!(state.runs[0].provider_id, None);
    }

    #[test]
    fn migrates_legacy_codex_profile() {
        let fixture = serde_json::json!({
            "providers": [{
                "id": Uuid::new_v4(),
                "name": "codex",
                "model": "gpt",
                "kind": { "type": "subscription_cli", "command": "codex" },
                "created_at": "2026-06-11T00:00:00Z"
            }]
        });
        let state = parse_state(&serde_json::to_vec(&fixture).unwrap()).expect("migrate state");
        assert_eq!(state.providers.len(), 1);
    }
}
