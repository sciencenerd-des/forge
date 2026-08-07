use std::{
    collections::HashMap,
    path::PathBuf,
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};

use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::{Child, ChildStdin, Command},
    sync::{Mutex, broadcast, oneshot},
    time::{Duration, timeout},
};

use crate::{
    JsonlDecoder,
    protocol::{PiCommand, PiIncoming},
};

#[derive(Debug, thiserror::Error)]
pub enum PiError {
    #[error(
        "Pi executable is unavailable. Install it with: npm i -g @earendil-works/pi-coding-agent"
    )]
    NotInstalled,
    #[error("could not start Pi: {0}")]
    Spawn(#[source] std::io::Error),
    #[error("Pi stdin is unavailable")]
    MissingStdin,
    #[error("Pi stdout is unavailable")]
    MissingStdout,
    #[error("Pi command serialization failed: {0}")]
    Serialize(#[from] serde_json::Error),
    #[error("Pi process ended before responding")]
    Ended,
    #[error("Pi protocol line was invalid: {0}")]
    Protocol(String),
    #[error("Pi command response timed out")]
    Timeout,
    #[error("invalid Pi session options: {0}")]
    InvalidSpawnOptions(String),
}

/// Session arguments accepted by Pi RPC mode. This keeps all callers on one
/// validated flag contract and prevents interactive `--resume` from being
/// passed before JSONL RPC starts.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct SpawnOptions {
    pub provider: Option<String>,
    pub model: Option<String>,
    pub session: Option<String>,
    pub fork: Option<String>,
    pub continue_recent: bool,
    pub no_session: bool,
    pub session_dir: Option<PathBuf>,
}

impl SpawnOptions {
    pub fn args(&self) -> Result<Vec<String>, PiError> {
        let session_mode_count = usize::from(self.session.is_some())
            + usize::from(self.fork.is_some())
            + usize::from(self.continue_recent)
            + usize::from(self.no_session);
        if session_mode_count > 1 {
            return Err(PiError::InvalidSpawnOptions(
                "choose only one of session, fork, continue, or no-session".into(),
            ));
        }
        let mut args = Vec::new();
        if let Some(provider) = &self.provider {
            args.extend(["--provider".into(), provider.clone()]);
        }
        if let Some(model) = &self.model {
            args.extend(["--model".into(), model.clone()]);
        }
        if let Some(session_dir) = &self.session_dir {
            args.extend(["--session-dir".into(), session_dir.display().to_string()]);
        }
        if let Some(session) = &self.session {
            args.extend(["--session".into(), session.clone()]);
        } else if let Some(fork) = &self.fork {
            args.extend(["--fork".into(), fork.clone()]);
        } else if self.continue_recent {
            args.push("--continue".into());
        } else if self.no_session {
            args.push("--no-session".into());
        }
        Ok(args)
    }
}

struct Inner {
    stdin: Mutex<ChildStdin>,
    child: Mutex<Child>,
    pending: Mutex<HashMap<String, oneshot::Sender<serde_json::Value>>>,
    next_id: AtomicU64,
    events: broadcast::Sender<PiIncoming>,
}

/// A managed Pi RPC process. The reader uses the shared byte-oriented decoder,
/// preserving Pi's strict LF-delimited JSONL framing across partial reads.
#[derive(Clone)]
pub struct PiClient(Arc<Inner>);

impl PiClient {
    pub async fn spawn_with_options(options: &SpawnOptions) -> Result<Self, PiError> {
        Self::spawn_pi(&options.args()?).await
    }

    pub async fn spawn_pi(args: &[String]) -> Result<Self, PiError> {
        let mut command = Command::new("pi");
        command.arg("--mode").arg("rpc").args(args);
        Self::spawn(command).await
    }

    pub async fn spawn(mut command: Command) -> Result<Self, PiError> {
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        let mut child = command.spawn().map_err(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                PiError::NotInstalled
            } else {
                PiError::Spawn(error)
            }
        })?;
        let stdin = child.stdin.take().ok_or(PiError::MissingStdin)?;
        let stdout = child.stdout.take().ok_or(PiError::MissingStdout)?;
        let (events, _) = broadcast::channel(1024);
        let inner = Arc::new(Inner {
            stdin: Mutex::new(stdin),
            child: Mutex::new(child),
            pending: Mutex::new(HashMap::new()),
            next_id: AtomicU64::new(1),
            events,
        });
        Self::spawn_reader(inner.clone(), stdout);
        Ok(Self(inner))
    }

    fn spawn_reader(inner: Arc<Inner>, stdout: tokio::process::ChildStdout) {
        tokio::spawn(async move {
            let mut reader = stdout;
            let mut decoder = JsonlDecoder::default();
            let mut buffer = [0_u8; 8192];
            loop {
                let read = match reader.read(&mut buffer).await {
                    Ok(0) => break,
                    Ok(read) => read,
                    Err(error) => {
                        tracing::warn!(%error, "Pi stdout read failed");
                        break;
                    }
                };
                match decoder.push(&buffer[..read]) {
                    Ok(values) => {
                        for value in values {
                            dispatch_value(&inner, value).await;
                        }
                    }
                    Err(error) => tracing::warn!(%error, "ignored malformed Pi JSONL record"),
                }
            }
            match decoder.finish() {
                Ok(Some(value)) => dispatch_value(&inner, value).await,
                Ok(None) => {}
                Err(error) => tracing::warn!(%error, "ignored incomplete Pi JSONL record"),
            }
            for (_, sender) in inner.pending.lock().await.drain() {
                drop(sender);
            }
        });
    }

    async fn write_value(&self, value: serde_json::Value) -> Result<(), PiError> {
        let serialized = serde_json::to_vec(&value)?;
        let mut stdin = self.0.stdin.lock().await;
        stdin.write_all(&serialized).await.map_err(PiError::Spawn)?;
        stdin.write_all(b"\n").await.map_err(PiError::Spawn)
    }

    pub fn events(&self) -> broadcast::Receiver<PiIncoming> {
        self.0.events.subscribe()
    }

    pub async fn send(&self, command: PiCommand) -> Result<serde_json::Value, PiError> {
        let timeout_duration = command.timeout();
        let mut command = serde_json::to_value(command)?;
        let id = self.0.next_id.fetch_add(1, Ordering::Relaxed).to_string();
        command["id"] = serde_json::Value::String(id.clone());
        let (sender, receiver) = oneshot::channel();
        self.0.pending.lock().await.insert(id.clone(), sender);
        if let Err(error) = self.write_value(command).await {
            self.0.pending.lock().await.remove(&id);
            return Err(error);
        }
        match timeout(timeout_duration, receiver).await {
            Ok(Ok(value)) => Ok(value),
            Ok(Err(_)) => {
                self.0.pending.lock().await.remove(&id);
                Err(PiError::Ended)
            }
            Err(_) => {
                self.0.pending.lock().await.remove(&id);
                Err(PiError::Timeout)
            }
        }
    }

    /// Send a fire-and-forget RPC command. Extension UI responses use this
    /// path because their `id` identifies the extension request, not a new
    /// command correlation id.
    pub async fn send_untracked(&self, command: PiCommand) -> Result<(), PiError> {
        self.write_value(serde_json::to_value(command)?).await
    }

    pub async fn shutdown(&self) {
        let _ = self.send(PiCommand::Abort { id: None }).await;
        let _ = self.0.stdin.lock().await.shutdown().await;
        let mut child = self.0.child.lock().await;
        if timeout(Duration::from_secs(2), child.wait()).await.is_err() {
            let _ = child.kill().await;
        }
    }
}

async fn dispatch_value(inner: &Arc<Inner>, value: serde_json::Value) {
    if value.get("type").and_then(|v| v.as_str()) == Some("response") {
        if let Some(id) = value.get("id").and_then(|v| v.as_str())
            && let Some(sender) = inner.pending.lock().await.remove(id)
        {
            let _ = sender.send(value);
        }
        return;
    }
    match serde_json::from_value::<PiIncoming>(value) {
        Ok(event) => {
            let _ = inner.events.send(event);
        }
        Err(error) => {
            let _ = inner.events.send(PiIncoming::Unknown);
            tracing::warn!(%error, "ignored malformed Pi event");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn correlates_responses_and_broadcasts_events() {
        // Pass the JSON records as positional args emitted with `printf '%s\n'`
        // so the fake never depends on how a shell interprets `\"` — dash keeps
        // the backslashes (invalid JSON) while bash strips them, which made this
        // test pass on macOS and fail under Ubuntu CI's dash.
        let mut command = Command::new("sh");
        command.args([
            "-c",
            "read line; printf '%s\\n' \"$1\" \"$2\"",
            "sh",
            "{\"type\":\"response\",\"id\":\"1\",\"command\":\"get_state\",\"success\":true}",
            "{\"type\":\"agent_end\",\"messages\":[]}",
        ]);
        let client = PiClient::spawn(command).await.expect("spawn fake Pi");
        let mut events = client.events();
        let response = client
            .send(PiCommand::GetState { id: None })
            .await
            .expect("response");
        assert_eq!(response["success"], true);
        assert!(matches!(
            events.recv().await.expect("event"),
            PiIncoming::AgentEnd { .. }
        ));
        client.shutdown().await;
    }

    #[test]
    fn spawn_options_are_validated_and_rendered_once() {
        let options = SpawnOptions {
            provider: Some("openai".into()),
            model: Some("gpt-test".into()),
            session: Some("session.jsonl".into()),
            session_dir: Some(PathBuf::from("/tmp/pi-sessions")),
            ..SpawnOptions::default()
        };
        assert_eq!(
            options.args().expect("args"),
            [
                "--provider",
                "openai",
                "--model",
                "gpt-test",
                "--session-dir",
                "/tmp/pi-sessions",
                "--session",
                "session.jsonl"
            ]
        );
        assert!(
            SpawnOptions {
                session: Some("one".into()),
                no_session: true,
                ..SpawnOptions::default()
            }
            .args()
            .is_err()
        );
    }

    #[tokio::test]
    #[ignore = "requires the pinned Pi executable on PATH"]
    async fn pinned_pi_accepts_every_supported_flag_combination() {
        let directory = tempfile::tempdir().expect("session dir");
        let combinations = [
            SpawnOptions {
                no_session: true,
                ..SpawnOptions::default()
            },
            SpawnOptions {
                no_session: true,
                provider: Some("openai".into()),
                model: Some("gpt-4o-mini".into()),
                ..SpawnOptions::default()
            },
            SpawnOptions {
                session_dir: Some(directory.path().to_path_buf()),
                ..SpawnOptions::default()
            },
        ];
        for options in combinations {
            let client = PiClient::spawn_with_options(&options)
                .await
                .unwrap_or_else(|error| panic!("spawn with {options:?}: {error}"));
            let response = client
                .send(PiCommand::GetState { id: None })
                .await
                .unwrap_or_else(|error| panic!("get_state with {options:?}: {error}"));
            assert_eq!(response["success"], true, "options: {options:?}");
            client.shutdown().await;
        }
    }

    #[tokio::test]
    #[ignore = "requires the pinned Pi executable on PATH"]
    async fn pinned_pi_accepts_get_state() {
        let client = PiClient::spawn_pi(&["--no-session".into()])
            .await
            .expect("spawn Pi");
        let response = client
            .send(PiCommand::GetState { id: None })
            .await
            .expect("Pi response");
        assert_eq!(response["command"], "get_state");
        assert_eq!(response["success"], true);
        client.shutdown().await;
    }
}
