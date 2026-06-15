use std::{fs, path::PathBuf, time::Duration};

use anyhow::Result;
use crossterm::event::{self, Event as InputEvent, KeyCode};
use ratatui::{
    DefaultTerminal, Frame,
    layout::{Constraint, Layout},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, List, ListItem, Paragraph},
};

use crate::model::HarnessState;

#[derive(Clone, Debug, serde::Deserialize)]
struct RuntimeRun {
    run_id: String,
    project_id: String,
    status: String,
    #[serde(default)]
    batch: u32,
    #[serde(default)]
    pid: Option<u32>,
    #[serde(default)]
    updated_at: String,
    #[serde(default)]
    log: Option<PathBuf>,
}

pub fn run(state: &HarnessState) -> Result<()> {
    let mut terminal = ratatui::init();
    let result = event_loop(&mut terminal, state);
    ratatui::restore();
    result
}

fn event_loop(terminal: &mut DefaultTerminal, state: &HarnessState) -> Result<()> {
    loop {
        let runtime_runs = load_runtime_runs();
        terminal.draw(|frame| draw(frame, state, &runtime_runs))?;
        if event::poll(Duration::from_millis(250))?
            && let InputEvent::Key(key) = event::read()?
            && matches!(key.code, KeyCode::Char('q') | KeyCode::Esc)
        {
            return Ok(());
        }
    }
}

fn draw(frame: &mut Frame, state: &HarnessState, runtime_runs: &[RuntimeRun]) {
    let [header, body, footer] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(10),
        Constraint::Length(3),
    ])
    .areas(frame.area());
    let [runs, detail] =
        Layout::horizontal([Constraint::Percentage(32), Constraint::Percentage(68)]).areas(body);

    let title = Paragraph::new(Line::from(vec![
        Span::styled(
            " FORGE ",
            Style::default()
                .fg(Color::Black)
                .bg(Color::Yellow)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw("  autonomous coding harness"),
    ]))
    .block(Block::default().borders(Borders::BOTTOM));
    frame.render_widget(title, header);

    let items = if !runtime_runs.is_empty() {
        runtime_runs
            .iter()
            .map(|run| {
                ListItem::new(format!(
                    "{}  {}  batch {}",
                    run.status,
                    &run.run_id[..run.run_id.len().min(8)],
                    run.batch
                ))
            })
            .collect()
    } else if state.runs.is_empty() {
        vec![ListItem::new("No runs. Use `forge run start <goal>`.")]
    } else {
        state
            .runs
            .iter()
            .rev()
            .map(|run| {
                ListItem::new(format!(
                    "{:?}  {}  {}/{}",
                    run.status,
                    &run.id.to_string()[..8],
                    run.turn,
                    run.max_turns
                ))
            })
            .collect()
    };
    frame.render_widget(
        List::new(items).block(Block::default().title(" Runs ").borders(Borders::ALL)),
        runs,
    );

    let detail_text = runtime_runs
        .first()
        .map(|run| {
            let mut lines = vec![
                Line::from(format!("Run       {}", run.run_id)),
                Line::from(format!("Project   {}", run.project_id)),
                Line::from(format!("Status    {}", run.status)),
                Line::from(format!("Batch     {}", run.batch)),
                Line::from(format!(
                    "PID       {}",
                    run.pid
                        .map(|p| p.to_string())
                        .unwrap_or_else(|| "stopped".into())
                )),
                Line::from(format!("Updated   {}", run.updated_at)),
                Line::from(""),
            ];
            lines.extend(log_tail(run.log.as_ref()).into_iter().map(Line::from));
            lines
        })
        .or_else(|| {
            state.runs.last().map(|run| {
                vec![
                    Line::from(format!("Run       {}", run.id)),
                    Line::from(format!("Status    {:?}", run.status)),
                    Line::from(format!("Node      {}", run.current_node)),
                    Line::from(format!("Turn      {} / {}", run.turn, run.max_turns)),
                    Line::from(""),
                    Line::styled(
                        "Planner -> Executor -> Evaluator -> Auditor",
                        Style::default().fg(Color::Yellow),
                    ),
                ]
            })
        })
        .unwrap_or_else(|| vec![Line::from("Select or create a run to inspect execution.")]);
    frame.render_widget(
        Paragraph::new(detail_text)
            .block(Block::default().title(" Execution ").borders(Borders::ALL)),
        detail,
    );
    frame.render_widget(
        Paragraph::new(" q / esc quit   live PGE state refreshes every 250ms ")
            .block(Block::default().borders(Borders::TOP)),
        footer,
    );
}

fn forge_home() -> Option<PathBuf> {
    // Mirrors forge_config.home(): FORGE_HOME, else ~/.forge.
    if let Some(h) = std::env::var_os("FORGE_HOME") {
        return Some(PathBuf::from(h));
    }
    std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".forge"))
}

fn load_runtime_runs() -> Vec<RuntimeRun> {
    let Some(home) = forge_home() else {
        return Vec::new();
    };
    let path = home.join("logs/pge_runs/runs.json");
    let Ok(raw) = fs::read_to_string(path) else {
        return Vec::new();
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return Vec::new();
    };
    let Some(map) = value.as_object() else {
        return Vec::new();
    };
    let mut runs: Vec<RuntimeRun> = map
        .values()
        .filter_map(|item| serde_json::from_value(item.clone()).ok())
        .collect();
    runs.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
    runs
}

fn log_tail(path: Option<&PathBuf>) -> Vec<String> {
    let Some(path) = path else { return Vec::new() };
    fs::read_to_string(path)
        .map(|text| {
            let mut lines: Vec<_> = text.lines().rev().take(10).map(str::to_owned).collect();
            lines.reverse();
            lines
        })
        .unwrap_or_default()
}
