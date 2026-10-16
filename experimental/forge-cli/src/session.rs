use std::{future::Future, pin::Pin};

use anyhow::{Context, Result};
use forge_pi::{PiClient, PiCommand, PiError, PiIncoming, SpawnOptions};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    sync::broadcast,
};

use crate::SessionArgs;

/// The CLI owns a single engine seam. Pi is the sole implementation today;
/// keeping the trait prevents the UI from coupling to a child-process type.
pub trait SessionEngine {
    fn events(&self) -> broadcast::Receiver<PiIncoming>;
    fn prompt(
        &self,
        message: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), PiError>> + Send + '_>>;
    fn abort(&self) -> Pin<Box<dyn Future<Output = Result<(), PiError>> + Send + '_>>;
}

struct PiSession {
    client: PiClient,
}

impl SessionEngine for PiSession {
    fn events(&self) -> broadcast::Receiver<PiIncoming> {
        self.client.events()
    }
    fn prompt(
        &self,
        message: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), PiError>> + Send + '_>> {
        Box::pin(async move {
            self.client
                .send(PiCommand::Prompt {
                    id: None,
                    message,
                    streaming_behavior: None,
                })
                .await
                .map(|_| ())
        })
    }
    fn abort(&self) -> Pin<Box<dyn Future<Output = Result<(), PiError>> + Send + '_>> {
        Box::pin(async move {
            self.client
                .send(PiCommand::Abort { id: None })
                .await
                .map(|_| ())
        })
    }
}

pub async fn run(args: SessionArgs) -> Result<()> {
    if args.resume {
        anyhow::bail!(
            "--resume opens Pi's interactive selector before RPC starts; use --session PATH_OR_ID or --continue"
        );
    }
    let session = PiSession {
        client: PiClient::spawn_with_options(&SpawnOptions {
            provider: args.provider,
            model: args.model,
            session: args.session,
            fork: args.fork,
            continue_recent: args.continue_recent,
            no_session: args.no_session,
            session_dir: args.session_dir,
        })
        .await?,
    };
    let mut events = session.events();
    println!("Pi session ready. Type a prompt; /abort aborts; /quit exits.");
    let mut input = BufReader::new(tokio::io::stdin()).lines();
    loop {
        tokio::select! {
            line = input.next_line() => match line.context("read terminal input")? { Some(line) if line == "/quit" => break, Some(line) if line == "/abort" => session.abort().await?, Some(line) if !line.trim().is_empty() => session.prompt(line).await?, Some(_) => {}, None => break },
            event = events.recv() => match event {
                Ok(PiIncoming::MessageUpdate { assistant_message_event, .. }) => {
                    if assistant_message_event.get("type").and_then(|value| value.as_str()) == Some("text_delta")
                        && let Some(delta) = assistant_message_event.get("delta").and_then(|value| value.as_str())
                    {
                        print!("{delta}");
                    }
                }
                Ok(PiIncoming::AgentEnd { .. }) => println!(),
                Ok(PiIncoming::ExtensionUiRequest { id, method, .. }) => {
                    eprintln!("\nPi extension requested {method}; cancelling it in CLI mode.");
                    session
                        .client
                        .send_untracked(PiCommand::ExtensionUiResponse {
                            id,
                            confirmed: None,
                            value: None,
                            cancelled: Some(true),
                        })
                        .await?;
                }
                Ok(_) => {},
                Err(broadcast::error::RecvError::Lagged(count)) => eprintln!("session event backlog dropped {count} events"),
                Err(broadcast::error::RecvError::Closed) => break,
            },
        }
    }
    session.client.shutdown().await;
    Ok(())
}
