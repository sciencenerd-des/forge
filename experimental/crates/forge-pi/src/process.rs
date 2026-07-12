use std::{
    collections::HashMap,
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
        let timeout_duration = match &command {
            PiCommand::Compact { .. } => Duration::from_secs(60),
            PiCommand::SwitchSession { .. } | PiCommand::Fork { .. } => Duration::from_secs(30),
            _ => Duration::from_secs(15),
        };
        let mut command = serde_json::to_value(command)?;
        let id = self.0.next_id.fetch_add(1, Ordering::Relaxed).to_string();
        command["id"] = serde_json::Value::String(id.clone());
        let (sender, receiver) = oneshot::channel();
        self.0.pending.lock().await.insert(id.clone(), sender);
        if let Err(error) = self.write_value(command).await {
            self.0.pending.lock().await.remove(&id);
            return Err(error);
        }
        timeout(timeout_duration, receiver)
            .await
            .map_err(|_| PiError::Timeout)?
            .map_err(|_| PiError::Ended)
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
            "{\"type\":\"agent_settled\"}",
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
            PiIncoming::AgentSettled
        ));
        client.shutdown().await;
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
