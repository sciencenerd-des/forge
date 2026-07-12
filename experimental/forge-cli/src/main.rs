mod session;
mod setup;
mod tui;

use std::time::Duration;

use anyhow::{Context, Result};
use clap::{Args, Parser, Subcommand};
use forge_api::{
    ApprovalDecision, ForgeApi, ForgeConfig, ProjectId, ProviderUpdate, RunId, RuntimeRunStart,
};
use futures_util::StreamExt;

#[derive(Parser)]
#[command(name = "forge", version, about = "Forge operator CLI")]
struct Cli {
    #[arg(long, global = true, help = "Print command results as stable JSON")]
    json: bool,
    #[arg(
        long,
        global = true,
        help = "Write debug-level details to the Forge CLI log"
    )]
    verbose: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Health,
    Config,
    Tui,
    Session(SessionArgs),
    Run {
        #[command(subcommand)]
        command: RunCommand,
    },
    Approvals {
        #[command(subcommand)]
        command: ApprovalCommand,
    },
    Provider {
        #[command(subcommand)]
        command: ProviderCommand,
    },
}

#[derive(Args)]
struct SessionArgs {
    #[arg(long)]
    provider: Option<String>,
    #[arg(long)]
    model: Option<String>,
    #[arg(long)]
    resume: bool,
    #[arg(long)]
    fork: Option<String>,
    #[arg(long)]
    no_session: bool,
}

#[derive(Subcommand)]
enum RunCommand {
    Start(RunStart),
    Stop {
        project_id: String,
    },
    List {
        #[arg(long)]
        watch: bool,
    },
    Events {
        run_id: String,
        #[arg(long)]
        follow: bool,
        #[arg(long, default_value_t = 0)]
        after: u64,
    },
    Get {
        run_id: String,
    },
}

#[derive(Args)]
struct RunStart {
    #[arg(long)]
    goal: String,
    #[arg(long, default_value = "")]
    description: String,
    #[arg(long)]
    project: Option<String>,
}

#[derive(Subcommand)]
enum ApprovalCommand {
    List {
        #[arg(long)]
        status: Option<String>,
    },
    Decide(ApprovalDecide),
}

#[derive(Args)]
struct ApprovalDecide {
    id: String,
    #[arg(long, conflicts_with = "deny")]
    approve: bool,
    #[arg(long, conflicts_with = "approve")]
    deny: bool,
    #[arg(long)]
    reason: String,
    #[arg(long, default_value = "forge-cli")]
    actor: String,
}

#[derive(Subcommand)]
enum ProviderCommand {
    List,
    Set(ProviderSet),
    Rm {
        role: String,
    },
    Test(ProviderSet),
    /// Offline-only provider profile writer retained for bootstrapping.
    Setup(ProviderSetup),
}

#[derive(Args)]
struct ProviderSet {
    role: String,
    #[arg(long)]
    base_url: Option<String>,
    #[arg(long)]
    model: Option<String>,
    #[arg(long)]
    api_key: Option<String>,
    #[arg(long)]
    auth_mode: Option<String>,
}

#[derive(Args)]
struct ProviderSetup {
    #[arg(long, default_value = "default")]
    role: String,
    #[arg(long, default_value = "http://localhost:1234/v1")]
    base_url: String,
    #[arg(long, default_value = "auto")]
    model: String,
    #[arg(long, default_value = "not-needed")]
    api_key: String,
    #[arg(long, default_value = "api_key")]
    auth_mode: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    let Cli {
        json,
        verbose,
        command,
    } = Cli::parse();
    let command = match command {
        Command::Provider {
            command: ProviderCommand::Setup(args),
        } => {
            let path = setup::save_profile(
                &args.role,
                &args.base_url,
                &args.model,
                &args.api_key,
                &args.auth_mode,
            )?;
            return print_value(json, &serde_json::json!({"status": "saved", "path": path}));
        }
        Command::Session(args) => return session::run(args).await,
        command => command,
    };

    let config = ForgeConfig::resolve().context("resolve Forge control-plane configuration")?;
    init_logging(&config, verbose)?;
    let api = ForgeApi::new(&config).context("create Forge control-plane client")?;
    match command {
        Command::Health => print_value(json, &api.health().await?),
        Command::Config => print_value(
            json,
            &serde_json::json!({
                "home": config.home,
                "control_url": config.control_url.to_string(),
                "control_token": mask_token(&config.control_token),
                "manifest": config.manifest_path(),
            }),
        ),
        Command::Tui => tui::run(api, config).await,
        Command::Session(_) => unreachable!("session starts before control-plane configuration"),
        Command::Run { command } => run_command(&api, &config, json, command).await,
        Command::Approvals { command } => approval_command(&api, json, command).await,
        Command::Provider { command } => provider_command(&api, json, command).await,
    }
}

fn init_logging(config: &ForgeConfig, verbose: bool) -> Result<()> {
    let path = config.home.join("logs/forge-cli.log");
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    let filter = if verbose { "forge=debug" } else { "forge=info" };
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_ansi(false)
        .with_writer(file)
        .try_init();
    tracing::debug!(control_url = %config.control_url, "Forge CLI logging initialized");
    Ok(())
}

async fn run_command(
    api: &ForgeApi,
    config: &ForgeConfig,
    json: bool,
    command: RunCommand,
) -> Result<()> {
    match command {
        RunCommand::Start(args) => print_value(
            json,
            &api.start_run(&RuntimeRunStart {
                goal: args.goal,
                description: args.description,
                project_id: args.project.map(ProjectId),
            })
            .await?,
        ),
        RunCommand::Stop { project_id } => {
            print_value(json, &api.stop_run(&ProjectId(project_id)).await?)
        }
        RunCommand::Get { run_id } => print_value(json, &api.run(&RunId(run_id)).await?),
        RunCommand::List { watch } => loop {
            match api.runtime_runs().await {
                Ok(runs) => print_value(
                    json,
                    &serde_json::json!({"source":"control-plane", "runs": runs}),
                )?,
                Err(error) => {
                    let manifests: forge_api::OfflineRunManifests =
                        std::fs::read(config.manifest_path())
                            .ok()
                            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
                            .unwrap_or_default();
                    print_value(
                        json,
                        &serde_json::json!({"source":"offline-manifest", "warning": error.to_string(), "runs": manifests}),
                    )?;
                }
            }
            if !watch {
                return Ok(());
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        },
        RunCommand::Events {
            run_id,
            follow,
            after,
        } => {
            let run_id = RunId(run_id);
            if !follow {
                return print_value(json, &api.events(&run_id, after).await?);
            }
            let mut cursor = after;
            loop {
                let mut stream = api.event_stream(&run_id, cursor).await?;
                while let Some(item) = stream.next().await {
                    let event = item?;
                    if let Some(id) = &event.id {
                        cursor = id.parse().unwrap_or(cursor);
                    }
                    if json {
                        println!(
                            "{}",
                            serde_json::json!({"id": event.id, "event": event.event, "data": event.data})
                        );
                    } else {
                        println!("{} {}", event.event, event.data);
                    }
                }
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }
    }
}

async fn approval_command(api: &ForgeApi, json: bool, command: ApprovalCommand) -> Result<()> {
    match command {
        ApprovalCommand::List { status } => {
            print_value(json, &api.approvals(status.as_deref()).await?)
        }
        ApprovalCommand::Decide(args) => {
            if args.approve == args.deny {
                anyhow::bail!("choose exactly one of --approve or --deny");
            }
            print_value(
                json,
                &api.decide_approval(
                    &args.id,
                    &ApprovalDecision {
                        actor: args.actor,
                        approved: args.approve,
                        reason: args.reason,
                    },
                )
                .await?,
            )
        }
    }
}

async fn provider_command(api: &ForgeApi, json: bool, command: ProviderCommand) -> Result<()> {
    match command {
        ProviderCommand::List => print_value(json, &api.providers().await?),
        ProviderCommand::Rm { role } => {
            api.remove_provider(&role).await?;
            print_value(
                json,
                &serde_json::json!({"status": "deleted", "role": role}),
            )
        }
        ProviderCommand::Set(args) => print_value(
            json,
            &api.put_provider(&args.role, &provider_update(&args))
                .await?,
        ),
        ProviderCommand::Test(args) => print_value(
            json,
            &api.test_provider(&args.role, &provider_update(&args))
                .await?,
        ),
        ProviderCommand::Setup(_) => {
            unreachable!("offline setup is handled before configuration resolution")
        }
    }
}

fn provider_update(args: &ProviderSet) -> ProviderUpdate {
    ProviderUpdate {
        base_url: args.base_url.clone(),
        model: args.model.clone(),
        api_key: args.api_key.clone(),
        auth_mode: args.auth_mode.clone(),
    }
}

fn mask_token(token: &str) -> String {
    if token.len() <= 8 {
        "••••".into()
    } else {
        format!("{}…{}", &token[..3], &token[token.len() - 4..])
    }
}

fn print_value<T: serde::Serialize>(json: bool, value: &T) -> Result<()> {
    let rendered = serde_json::to_value(value)?;
    if json {
        println!("{}", serde_json::to_string_pretty(&rendered)?);
    } else {
        print_human(&rendered);
    }
    Ok(())
}

fn print_human(value: &serde_json::Value) {
    match value {
        serde_json::Value::Array(items) => {
            if items.is_empty() {
                println!("No results.");
            }
            for item in items {
                println!(
                    "{}",
                    serde_json::to_string(item).unwrap_or_else(|_| "<unprintable>".into())
                );
            }
        }
        _ => println!(
            "{}",
            serde_json::to_string_pretty(value).unwrap_or_else(|_| "<unprintable>".into())
        ),
    }
}
