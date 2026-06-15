mod model;
mod store;
mod tui;

use std::{path::PathBuf, process::Command as ProcessCommand};

use anyhow::{Context, Result};
use clap::{Args, Parser, Subcommand};
use model::{Event, Goal, Project, ProviderKind, ProviderProfile, Run, RunStatus};
use store::Store;
use url::{Host, Url};
use uuid::Uuid;

#[derive(Parser)]
#[command(
    name = "forge",
    version,
    about = "Autonomous coding harness operator CLI"
)]
struct Cli {
    #[arg(long, global = true, help = "Print command results as JSON")]
    json: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Init,
    Project {
        #[command(subcommand)]
        command: ProjectCommand,
    },
    Goal {
        #[command(subcommand)]
        command: GoalCommand,
    },
    Run {
        #[command(subcommand)]
        command: RunCommand,
    },
    Provider {
        #[command(subcommand)]
        command: ProviderCommand,
    },
    Tui,
}

#[derive(Subcommand)]
enum ProviderCommand {
    AddLocal(ProviderLocal),
    AddCloud(ProviderCloud),
    AddSubscription(ProviderSubscription),
    List,
    SetDefault { provider: Uuid },
    Login { provider: Uuid },
}

#[derive(Args)]
struct ProviderLocal {
    #[arg(long)]
    name: String,
    #[arg(long)]
    model: String,
    #[arg(long, default_value = "http://localhost:1234/v1")]
    base_url: String,
}

#[derive(Args)]
struct ProviderCloud {
    #[arg(long)]
    name: String,
    #[arg(long)]
    model: String,
    #[arg(long)]
    base_url: String,
    #[arg(long, help = "Environment variable containing the API key")]
    api_key_env: String,
}

#[derive(Args)]
struct ProviderSubscription {
    #[arg(long)]
    name: String,
    #[arg(long)]
    model: String,
}

#[derive(Subcommand)]
enum ProjectCommand {
    Add(ProjectAdd),
    List,
}

#[derive(Args)]
struct ProjectAdd {
    path: PathBuf,
    #[arg(long)]
    name: Option<String>,
}

#[derive(Subcommand)]
enum GoalCommand {
    Create(GoalCreate),
    List {
        #[arg(long)]
        project: Option<Uuid>,
    },
}

#[derive(Args)]
struct GoalCreate {
    #[arg(long)]
    project: Uuid,
    #[arg(long)]
    title: String,
    #[arg(long, default_value = "")]
    description: String,
}

#[derive(Subcommand)]
enum RunCommand {
    Start {
        goal: Uuid,
        #[arg(long)]
        provider: Option<Uuid>,
    },
    List,
    Inspect {
        run: Uuid,
    },
    Pause {
        run: Uuid,
    },
    Resume {
        run: Uuid,
    },
    Cancel {
        run: Uuid,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let mut store = Store::open()?;

    match cli.command {
        Command::Init => {
            store.save()?;
            print_value(
                cli.json,
                &serde_json::json!({
                    "status": "initialized",
                    "store": store.path(),
                }),
            )?;
        }
        Command::Project { command } => match command {
            ProjectCommand::Add(args) => {
                let path = args.path.canonicalize().with_context(|| {
                    format!("project path does not exist: {}", args.path.display())
                })?;
                let name = args.name.unwrap_or_else(|| {
                    path.file_name()
                        .and_then(|v| v.to_str())
                        .unwrap_or("project")
                        .to_owned()
                });
                let project = Project::new(name, path);
                store.state.projects.push(project.clone());
                store.save()?;
                print_value(cli.json, &project)?;
            }
            ProjectCommand::List => print_value(cli.json, &store.state.projects)?,
        },
        Command::Goal { command } => match command {
            GoalCommand::Create(args) => {
                store.require_project(args.project)?;
                let goal = Goal::new(args.project, args.title, args.description);
                store.state.goals.push(goal.clone());
                store.save()?;
                print_value(cli.json, &goal)?;
            }
            GoalCommand::List { project } => {
                let goals: Vec<_> = store
                    .state
                    .goals
                    .iter()
                    .filter(|goal| project.is_none_or(|id| goal.project_id == id))
                    .collect();
                print_value(cli.json, &goals)?;
            }
        },
        Command::Run { command } => match command {
            RunCommand::Start { goal, provider } => {
                let goal_record = store.require_goal(goal)?.clone();
                let provider_id = provider.or(store.state.default_provider_id);
                if let Some(id) = provider_id {
                    store.require_provider(id)?;
                }
                let run = Run::new(goal_record.project_id, goal, provider_id);
                store
                    .state
                    .events
                    .push(Event::created(run.id, "runtime", "Run queued"));
                store.state.runs.push(run.clone());
                store.save()?;
                print_value(cli.json, &run)?;
            }
            RunCommand::List => print_value(cli.json, &store.state.runs)?,
            RunCommand::Inspect { run } => {
                let record = store.require_run(run)?;
                let events: Vec<_> = store
                    .state
                    .events
                    .iter()
                    .filter(|e| e.run_id == run)
                    .collect();
                print_value(
                    cli.json,
                    &serde_json::json!({"run": record, "events": events}),
                )?;
            }
            RunCommand::Pause { run } => {
                update_run(&mut store, run, RunStatus::Paused, "Run paused", cli.json)?
            }
            RunCommand::Resume { run } => update_run(
                &mut store,
                run,
                RunStatus::Queued,
                "Run re-queued",
                cli.json,
            )?,
            RunCommand::Cancel { run } => update_run(
                &mut store,
                run,
                RunStatus::Cancelled,
                "Run cancelled",
                cli.json,
            )?,
        },
        Command::Provider { command } => match command {
            ProviderCommand::AddLocal(args) => add_provider(
                &mut store,
                ProviderProfile::new(
                    validate_profile_text("provider name", &args.name, 64)?,
                    validate_profile_text("model ID", &args.model, 256)?,
                    ProviderKind::LocalOpenAi {
                        base_url: validate_local_url(&args.base_url)?,
                    },
                ),
                cli.json,
            )?,
            ProviderCommand::AddCloud(args) => add_provider(
                &mut store,
                ProviderProfile::new(
                    validate_profile_text("provider name", &args.name, 64)?,
                    validate_profile_text("model ID", &args.model, 256)?,
                    ProviderKind::CloudApi {
                        base_url: validate_cloud_url(&args.base_url)?,
                        api_key_env: validate_env_name(&args.api_key_env)?,
                    },
                ),
                cli.json,
            )?,
            ProviderCommand::AddSubscription(args) => add_provider(
                &mut store,
                ProviderProfile::new(
                    validate_profile_text("provider name", &args.name, 64)?,
                    validate_profile_text("model ID", &args.model, 256)?,
                    ProviderKind::CodexSubscription,
                ),
                cli.json,
            )?,
            ProviderCommand::List => print_value(
                cli.json,
                &serde_json::json!({
                    "default_provider_id": store.state.default_provider_id,
                    "providers": store.state.providers,
                }),
            )?,
            ProviderCommand::SetDefault { provider } => {
                store.require_provider(provider)?;
                store.state.default_provider_id = Some(provider);
                store.save()?;
                print_value(
                    cli.json,
                    &serde_json::json!({ "default_provider_id": provider }),
                )?;
            }
            ProviderCommand::Login { provider } => login_provider(&store, provider)?,
        },
        Command::Tui => tui::run(&store.state)?,
    }
    Ok(())
}

fn add_provider(store: &mut Store, provider: ProviderProfile, json: bool) -> Result<()> {
    if store
        .state
        .providers
        .iter()
        .any(|item| item.name.eq_ignore_ascii_case(&provider.name))
    {
        anyhow::bail!("provider name '{}' already exists", provider.name);
    }
    if store.state.default_provider_id.is_none() {
        store.state.default_provider_id = Some(provider.id);
    }
    store.state.providers.push(provider.clone());
    store.save()?;
    print_value(json, &provider)
}

fn login_provider(store: &Store, provider_id: Uuid) -> Result<()> {
    let provider = store.require_provider(provider_id)?;
    let ProviderKind::CodexSubscription = &provider.kind else {
        anyhow::bail!(
            "provider '{}' does not use subscription login",
            provider.name
        );
    };
    let status = ProcessCommand::new("codex")
        .arg("login")
        .status()
        .context("could not launch 'codex'. Install the official Codex CLI first")?;
    if !status.success() {
        anyhow::bail!("codex login exited with {status}");
    }
    Ok(())
}

fn validate_cloud_url(value: &str) -> Result<String> {
    let url = parse_provider_url(value)?;
    if url.scheme() != "https" {
        anyhow::bail!("cloud provider URLs must use HTTPS");
    }
    Ok(normalize_url(url))
}

fn validate_local_url(value: &str) -> Result<String> {
    let url = parse_provider_url(value)?;
    if !matches!(url.scheme(), "http" | "https") {
        anyhow::bail!("local provider URLs must use HTTP or HTTPS");
    }
    let is_loopback = match url.host() {
        Some(Host::Domain(host)) => host.eq_ignore_ascii_case("localhost"),
        Some(Host::Ipv4(address)) => address.is_loopback(),
        Some(Host::Ipv6(address)) => address.is_loopback(),
        None => false,
    };
    if !is_loopback {
        anyhow::bail!("local provider URLs must use localhost or a loopback IP");
    }
    Ok(normalize_url(url))
}

fn parse_provider_url(value: &str) -> Result<Url> {
    let url = Url::parse(value).context("provider base URL is invalid")?;
    if !url.username().is_empty() || url.password().is_some() {
        anyhow::bail!("provider URLs must not contain credentials");
    }
    if url.query().is_some() || url.fragment().is_some() {
        anyhow::bail!("provider URLs must not contain query strings or fragments");
    }
    Ok(url)
}

fn normalize_url(mut url: Url) -> String {
    let path = url.path().trim_end_matches('/').to_owned();
    url.set_path(if path.is_empty() { "/" } else { &path });
    url.to_string().trim_end_matches('/').to_owned()
}

fn validate_env_name(value: &str) -> Result<String> {
    let mut characters = value.chars();
    let valid_start = characters
        .next()
        .is_some_and(|character| character == '_' || character.is_ascii_uppercase());
    if !valid_start
        || !characters.all(|character| {
            character == '_' || character.is_ascii_uppercase() || character.is_ascii_digit()
        })
    {
        anyhow::bail!("API key environment variable must match [A-Z_][A-Z0-9_]*");
    }
    Ok(value.to_owned())
}

fn validate_profile_text(label: &str, value: &str, max_length: usize) -> Result<String> {
    if value.chars().any(char::is_control) {
        anyhow::bail!("{label} must not contain control characters");
    }
    let value = value.trim();
    if value.is_empty() {
        anyhow::bail!("{label} must not be empty");
    }
    if value.len() > max_length {
        anyhow::bail!("{label} must be at most {max_length} bytes");
    }
    Ok(value.to_owned())
}

fn update_run(
    store: &mut Store,
    run_id: Uuid,
    status: RunStatus,
    message: &str,
    json: bool,
) -> Result<()> {
    let snapshot = {
        let run = store.require_run_mut(run_id)?;
        run.set_status(status);
        run.clone()
    };
    store
        .state
        .events
        .push(Event::created(run_id, "operator", message));
    store.save()?;
    print_value(json, &snapshot)
}

fn print_value<T: serde::Serialize + std::fmt::Debug>(json: bool, value: &T) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(value)?);
    } else {
        println!("{value:#?}");
    }
    Ok(())
}

#[cfg(test)]
mod security_tests {
    use super::*;

    #[test]
    fn accepts_documented_local_endpoints() {
        assert_eq!(
            validate_local_url("http://localhost:1234/v1").unwrap(),
            "http://localhost:1234/v1"
        );
        assert_eq!(
            validate_local_url("http://127.0.0.1:11434/v1/").unwrap(),
            "http://127.0.0.1:11434/v1"
        );
        assert!(validate_local_url("http://[::1]:11434/v1").is_ok());
    }

    #[test]
    fn rejects_remote_or_credentialed_local_endpoints() {
        assert!(validate_local_url("http://192.168.1.10:1234/v1").is_err());
        assert!(validate_local_url("http://attacker.example/v1").is_err());
        assert!(validate_local_url("http://token@localhost:1234/v1").is_err());
    }

    #[test]
    fn cloud_endpoints_require_clean_https_urls() {
        assert!(validate_cloud_url("https://api.openai.com/v1").is_ok());
        assert!(validate_cloud_url("http://api.openai.com/v1").is_err());
        assert!(validate_cloud_url("https://key@api.example/v1").is_err());
        assert!(validate_cloud_url("https://api.example/v1?token=secret").is_err());
    }

    #[test]
    fn environment_variable_names_cannot_inject_shell_syntax() {
        assert!(validate_env_name("OPENAI_API_KEY").is_ok());
        assert!(validate_env_name("OPENAI_API_KEY;rm -rf").is_err());
        assert!(validate_env_name("$OPENAI_API_KEY").is_err());
        assert!(validate_env_name("lowercase").is_err());
    }

    #[test]
    fn profile_text_rejects_terminal_escape_sequences() {
        assert!(validate_profile_text("name", "safe-provider", 64).is_ok());
        assert!(validate_profile_text("name", "\u{1b}[2Jspoofed", 64).is_err());
        assert!(validate_profile_text("name", "\nspoofed", 64).is_err());
        assert!(validate_profile_text("name", "   ", 64).is_err());
    }
}
