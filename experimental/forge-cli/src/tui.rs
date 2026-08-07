use std::{
    collections::VecDeque,
    fs,
    io::{BufRead, BufReader, Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use anyhow::{Context, Result, bail};
use crossterm::event::{self, Event as InputEvent, KeyCode, KeyEvent, KeyModifiers};
use forge_api::{
    Approval, ApprovalDecision, ForgeApi, ForgeConfig, OfflineRunManifests, ProjectId, RunEvent,
    RuntimeRunSnapshot, RuntimeRunStart,
};
use forge_pi::{PiClient, PiCommand, PiIncoming, SpawnOptions};
use futures_util::StreamExt;
use ratatui::{
    Frame,
    layout::{Constraint, Layout},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, List, ListItem, ListState, Paragraph},
};
use tokio::{
    process::{Child, Command},
    sync::{mpsc, watch},
    time::Instant,
};
use tui_textarea::{Input as EditorInput, Key as EditorKey, TextArea};

const MAX_SCROLLBACK: usize = 10_000;
const CONTROL_PLANE_STARTUP_TIMEOUT: Duration = Duration::from_secs(8);
const CONTROL_PLANE_RETRY_INTERVAL: Duration = Duration::from_millis(200);

/// A local Python control plane launched by this TUI. We retain the child so
/// closing the operator UI does not leave an untracked server behind.
struct ManagedControlPlane {
    child: Child,
    log_path: PathBuf,
}

#[derive(Clone, Copy)]
struct Theme {
    amber: Color,
    verdigris: Color,
    danger: Color,
    dim: Color,
}
const THEME: Theme = Theme {
    amber: Color::Rgb(232, 163, 61),
    verdigris: Color::Rgb(127, 180, 162),
    danger: Color::Rgb(212, 112, 105),
    dim: Color::Rgb(154, 151, 143),
};

enum Priority {
    Key(KeyEvent),
    Quit,
}
enum Bulk {
    Live(Vec<RuntimeRunSnapshot>, Vec<Approval>),
    Stream(Vec<RunEvent>),
    Pi(PiIncoming),
    SessionStats(String),
    Offline(OfflineRunManifests),
    Error(String),
}

enum PickerUpdate {
    Loaded(Vec<SessionEntry>),
    Failed(String),
}

enum InputMode {
    StartGoal(String),
    StopConfirm(ProjectId),
    ApprovalReason {
        id: String,
        approved: bool,
        reason: String,
    },
    Promote {
        goal: String,
        context: String,
        projects: Vec<PromoteProject>,
        selected: usize,
        stage: PromoteStage,
    },
}

#[derive(Clone, Copy, PartialEq)]
enum PromoteStage {
    Goal,
    Project,
    Confirm,
}

#[derive(Clone)]
struct PromoteProject {
    id: Option<ProjectId>,
    name: String,
    active: bool,
}

struct SessionEntry {
    path: String,
    name: String,
    modified: String,
    entries: String,
}

struct SessionPicker {
    entries: Vec<SessionEntry>,
    selected: usize,
}

enum UiEvent {
    Quit,
    ToggleMode,
    Action(Action),
}

enum Action {
    Start(String),
    Stop(ProjectId),
    Decide {
        id: String,
        approved: bool,
        reason: String,
    },
    PiPrompt(String),
    PiAbort,
    PiDialogConfirm {
        id: String,
        confirmed: bool,
    },
    PiDialogValue {
        id: String,
        value: String,
    },
    PiDialogCancel {
        id: String,
    },
    BeginPromote {
        context: String,
    },
    Promote {
        goal: String,
        context: String,
        project: Option<ProjectId>,
    },
    PiSteer(String),
    PiFollowUp(String),
    PiSwitchSession(String),
    PiForkSession(String),
    LoadSessionPicker,
    LoadProviders,
    PiControl {
        command: PiCommand,
        label: &'static str,
    },
}

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Operator,
    Session,
}

struct App {
    runs: Vec<RuntimeRunSnapshot>,
    offline: OfflineRunManifests,
    approvals: Vec<Approval>,
    events: VecDeque<String>,
    transcript: VecDeque<String>,
    editor: TextArea<'static>,
    pi_dialog: Option<PiDialog>,
    session_menu: bool,
    session_picker: Option<SessionPicker>,
    session_directory: Option<PathBuf>,
    session: Option<PiClient>,
    mode: Mode,
    selected: usize,
    offline_mode: bool,
    help: bool,
    input: Option<InputMode>,
    show_evidence: bool,
    dirty: bool,
    message: String,
    session_footer: String,
}

struct PiDialog {
    id: String,
    method: String,
    value: String,
    options: Vec<String>,
    deadline: Option<Instant>,
}

fn new_editor() -> TextArea<'static> {
    let mut editor = TextArea::default();
    editor.set_block(
        Block::default()
            .title(" Pi prompt · Tab focus · Enter send ")
            .borders(Borders::ALL),
    );
    editor
}

fn editor_input(event: KeyEvent) -> EditorInput {
    let key = match event.code {
        KeyCode::Char(character) => EditorKey::Char(character),
        KeyCode::Backspace => EditorKey::Backspace,
        KeyCode::Enter => EditorKey::Enter,
        KeyCode::Left => EditorKey::Left,
        KeyCode::Right => EditorKey::Right,
        KeyCode::Up => EditorKey::Up,
        KeyCode::Down => EditorKey::Down,
        KeyCode::Delete => EditorKey::Delete,
        KeyCode::Home => EditorKey::Home,
        KeyCode::End => EditorKey::End,
        KeyCode::PageUp => EditorKey::PageUp,
        KeyCode::PageDown => EditorKey::PageDown,
        KeyCode::Tab => EditorKey::Tab,
        _ => EditorKey::Null,
    };
    EditorInput {
        key,
        ctrl: event.modifiers.contains(KeyModifiers::CONTROL),
        alt: event.modifiers.contains(KeyModifiers::ALT),
        shift: event.modifiers.contains(KeyModifiers::SHIFT),
    }
}

impl App {
    #[cfg(test)]
    fn new() -> Self {
        Self::with_session_directory(default_session_directory())
    }

    fn with_session_directory(session_directory: Option<PathBuf>) -> Self {
        Self {
            runs: Vec::new(),
            offline: OfflineRunManifests::new(),
            approvals: Vec::new(),
            events: VecDeque::new(),
            transcript: VecDeque::new(),
            editor: new_editor(),
            pi_dialog: None,
            session_menu: false,
            session_picker: None,
            session_directory,
            session: None,
            mode: Mode::Operator,
            selected: 0,
            offline_mode: false,
            help: false,
            input: None,
            show_evidence: false,
            dirty: true,
            message: "Loading control plane…".into(),
            session_footer: "stats unavailable".into(),
        }
    }
    fn push_event(&mut self, event: String) {
        if self.events.len() == MAX_SCROLLBACK {
            self.events.pop_front();
        }
        self.events.push_back(event);
    }
    fn handle_key(&mut self, key: KeyCode) -> Option<UiEvent> {
        if key == KeyCode::Tab {
            self.mode = if self.mode == Mode::Operator {
                Mode::Session
            } else {
                Mode::Operator
            };
            self.dirty = true;
            return Some(UiEvent::ToggleMode);
        }
        if self.input.is_some() {
            return self.handle_input(key);
        }
        if self.mode == Mode::Session {
            return self.handle_session_key(KeyEvent::new(key, KeyModifiers::NONE));
        }
        match key {
            KeyCode::Char('q') | KeyCode::Esc => return Some(UiEvent::Quit),
            KeyCode::Char('?') => self.help = !self.help,
            KeyCode::Char('s') if !self.offline_mode => {
                self.input = Some(InputMode::StartGoal(String::new()))
            }
            KeyCode::Char('x') if !self.offline_mode => {
                if let Some(run) = self.runs.get(self.selected) {
                    self.input = Some(InputMode::StopConfirm(run.project_id.clone()));
                }
            }
            KeyCode::Char('a') => {
                self.message = format!(
                    "{} pending approval(s); y approve or n deny selected pending approval",
                    self.approvals.len()
                )
            }
            KeyCode::Char('y') | KeyCode::Char('n') if !self.approvals.is_empty() => {
                self.input = Some(InputMode::ApprovalReason {
                    id: self.approvals[0].id.clone(),
                    approved: key == KeyCode::Char('y'),
                    reason: String::new(),
                });
            }
            KeyCode::Char('p') => {
                return Some(UiEvent::Action(Action::LoadProviders));
            }
            KeyCode::Char('e') => {
                self.show_evidence = !self.show_evidence;
                self.message = if self.show_evidence {
                    "Showing selected-run evidence".into()
                } else {
                    "Showing selected-run summary".into()
                };
            }
            KeyCode::Char('j') | KeyCode::Down => {
                self.selected = self
                    .selected
                    .saturating_add(1)
                    .min(self.visible_len().saturating_sub(1))
            }
            KeyCode::Char('k') | KeyCode::Up => self.selected = self.selected.saturating_sub(1),
            _ => {}
        }
        self.dirty = true;
        None
    }

    fn handle_key_event(&mut self, event: KeyEvent) -> Option<UiEvent> {
        if self.mode == Mode::Session
            && self.input.is_none()
            && event.modifiers.contains(KeyModifiers::CONTROL)
            && event.code == KeyCode::Char('g')
        {
            let context = self
                .transcript
                .iter()
                .rev()
                .find(|line| line.starts_with('>'))
                .cloned()
                .unwrap_or_default();
            self.dirty = true;
            return Some(UiEvent::Action(Action::BeginPromote { context }));
        }
        if self.mode == Mode::Session && event.modifiers.contains(KeyModifiers::CONTROL) {
            let (command, label) = match event.code {
                KeyCode::Char('m') => (PiCommand::CycleModel { id: None }, "Model cycle"),
                KeyCode::Char('t') => (
                    PiCommand::CycleThinkingLevel { id: None },
                    "Thinking level cycle",
                ),
                KeyCode::Char('k') => (PiCommand::Compact { id: None }, "Context compaction"),
                KeyCode::Char('r') => {
                    self.message = "Loading saved Pi sessions…".into();
                    self.dirty = true;
                    return Some(UiEvent::Action(Action::LoadSessionPicker));
                }
                KeyCode::Char('q') => return Some(UiEvent::Quit),
                _ => return self.handle_key(event.code),
            };
            return Some(UiEvent::Action(Action::PiControl { command, label }));
        }
        if self.mode == Mode::Session {
            self.handle_session_key(event)
        } else {
            self.handle_key(event.code)
        }
    }

    fn handle_session_key(&mut self, event: KeyEvent) -> Option<UiEvent> {
        let key = event.code;
        if let Some(dialog) = self.pi_dialog.as_mut() {
            return match key {
                KeyCode::Esc => Some(UiEvent::Action(Action::PiDialogCancel {
                    id: dialog.id.clone(),
                })),
                KeyCode::Char('y') | KeyCode::Char('Y') if dialog.method == "confirm" => {
                    Some(UiEvent::Action(Action::PiDialogConfirm {
                        id: dialog.id.clone(),
                        confirmed: true,
                    }))
                }
                KeyCode::Char('n') | KeyCode::Char('N') if dialog.method == "confirm" => {
                    Some(UiEvent::Action(Action::PiDialogConfirm {
                        id: dialog.id.clone(),
                        confirmed: false,
                    }))
                }
                KeyCode::Backspace => {
                    dialog.value.pop();
                    self.dirty = true;
                    None
                }
                KeyCode::Char(character) if dialog.method != "confirm" => {
                    dialog.value.push(character);
                    self.dirty = true;
                    None
                }
                KeyCode::Enter if dialog.method != "confirm" && !dialog.value.trim().is_empty() => {
                    Some(UiEvent::Action(Action::PiDialogValue {
                        id: dialog.id.clone(),
                        value: dialog.value.trim().to_owned(),
                    }))
                }
                _ => None,
            };
        }
        if let Some(picker) = self.session_picker.as_mut() {
            self.dirty = true;
            return match key {
                KeyCode::Esc => {
                    self.session_picker = None;
                    None
                }
                KeyCode::Char('j') | KeyCode::Down => {
                    picker.selected = picker
                        .selected
                        .saturating_add(1)
                        .min(picker.entries.len().saturating_sub(1));
                    None
                }
                KeyCode::Char('k') | KeyCode::Up => {
                    picker.selected = picker.selected.saturating_sub(1);
                    None
                }
                KeyCode::Enter => {
                    let path = picker
                        .entries
                        .get(picker.selected)
                        .map(|entry| entry.path.clone());
                    self.session_picker = None;
                    path.map(|path| UiEvent::Action(Action::PiSwitchSession(path)))
                }
                KeyCode::Char('f') => {
                    let path = picker
                        .entries
                        .get(picker.selected)
                        .map(|entry| entry.path.clone());
                    self.session_picker = None;
                    path.map(|path| UiEvent::Action(Action::PiForkSession(path)))
                }
                _ => None,
            };
        }
        if self.session_menu {
            self.session_menu = false;
            self.dirty = true;
            return match key {
                KeyCode::Char('a') => Some(UiEvent::Action(Action::PiAbort)),
                KeyCode::Char('s') => {
                    let message = self.take_editor_text();
                    if message.trim().is_empty() {
                        self.message = "Type the steer message in the editor first".into();
                        None
                    } else {
                        self.transcript.push_back(format!("> [steer] {message}"));
                        Some(UiEvent::Action(Action::PiSteer(message)))
                    }
                }
                KeyCode::Char('f') => {
                    let message = self.take_editor_text();
                    if message.trim().is_empty() {
                        self.message = "Type the follow-up message in the editor first".into();
                        None
                    } else {
                        self.transcript
                            .push_back(format!("> [follow-up] {message}"));
                        Some(UiEvent::Action(Action::PiFollowUp(message)))
                    }
                }
                _ => None,
            };
        }
        match key {
            KeyCode::Esc => {
                self.session_menu = true;
                self.dirty = true;
                None
            }
            KeyCode::Enter if event.modifiers.contains(KeyModifiers::ALT) => {
                self.editor.input(editor_input(event));
                self.dirty = true;
                None
            }
            KeyCode::Enter if !self.editor_text().trim().is_empty() => {
                let prompt = self.take_editor_text();
                self.transcript.push_back(format!("> {prompt}"));
                self.dirty = true;
                Some(UiEvent::Action(Action::PiPrompt(prompt)))
            }
            _ => {
                if self.editor.input(editor_input(event)) {
                    self.dirty = true;
                }
                None
            }
        }
    }

    fn open_session_picker(&mut self, entries: Vec<SessionEntry>) {
        if entries.is_empty() {
            self.message = "No saved Pi sessions for this directory".into();
        }
        self.session_picker = Some(SessionPicker {
            entries,
            selected: 0,
        });
        self.dirty = true;
    }

    fn open_promote(&mut self, context: String, mut projects: Vec<PromoteProject>) {
        projects.insert(
            0,
            PromoteProject {
                id: None,
                name: "(default project)".into(),
                active: false,
            },
        );
        self.input = Some(InputMode::Promote {
            goal: String::new(),
            context,
            projects,
            selected: 0,
            stage: PromoteStage::Goal,
        });
        self.dirty = true;
    }

    fn editor_text(&self) -> String {
        self.editor.lines().join("\n")
    }

    fn take_editor_text(&mut self) -> String {
        let prompt = self.editor_text();
        self.editor = new_editor();
        prompt
    }

    fn handle_input(&mut self, key: KeyCode) -> Option<UiEvent> {
        let mode = self.input.as_mut().expect("input mode checked");
        match key {
            KeyCode::Esc => {
                self.input = None;
                self.message = "Action cancelled".into();
            }
            KeyCode::Backspace => match mode {
                InputMode::StartGoal(value) => {
                    value.pop();
                }
                InputMode::ApprovalReason { reason, .. } => {
                    reason.pop();
                }
                InputMode::Promote { goal, stage, .. } if *stage == PromoteStage::Goal => {
                    goal.pop();
                }
                InputMode::StopConfirm(_) | InputMode::Promote { .. } => {}
            },
            KeyCode::Char(character) => match mode {
                InputMode::StartGoal(value) => value.push(character),
                InputMode::ApprovalReason { reason, .. } => reason.push(character),
                InputMode::Promote { goal, stage, .. } if *stage == PromoteStage::Goal => {
                    goal.push(character)
                }
                InputMode::Promote {
                    projects,
                    selected,
                    stage,
                    ..
                } if *stage == PromoteStage::Project && (character == 'j' || character == 'k') => {
                    *selected = if character == 'j' {
                        selected
                            .saturating_add(1)
                            .min(projects.len().saturating_sub(1))
                    } else {
                        selected.saturating_sub(1)
                    };
                }
                InputMode::Promote {
                    goal,
                    context,
                    projects,
                    selected,
                    stage,
                } if *stage == PromoteStage::Confirm && (character == 'y' || character == 'Y') => {
                    let project = projects.get(*selected).and_then(|entry| entry.id.clone());
                    return Some(UiEvent::Action(Action::Promote {
                        goal: goal.clone(),
                        context: context.clone(),
                        project,
                    }));
                }
                InputMode::StopConfirm(project_id) if character == 'y' || character == 'Y' => {
                    return Some(UiEvent::Action(Action::Stop(project_id.clone())));
                }
                InputMode::StopConfirm(_) => {}
                InputMode::Promote { .. } => {}
            },
            KeyCode::Down | KeyCode::Up
                if matches!(
                    mode,
                    InputMode::Promote {
                        stage: PromoteStage::Project,
                        ..
                    }
                ) =>
            {
                if let InputMode::Promote {
                    projects, selected, ..
                } = mode
                {
                    *selected = if key == KeyCode::Down {
                        selected
                            .saturating_add(1)
                            .min(projects.len().saturating_sub(1))
                    } else {
                        selected.saturating_sub(1)
                    };
                }
            }
            KeyCode::Enter => match mode {
                InputMode::StartGoal(goal) if !goal.trim().is_empty() => {
                    return Some(UiEvent::Action(Action::Start(goal.trim().to_owned())));
                }
                InputMode::ApprovalReason {
                    id,
                    approved,
                    reason,
                } if !reason.trim().is_empty() => {
                    return Some(UiEvent::Action(Action::Decide {
                        id: id.clone(),
                        approved: *approved,
                        reason: reason.trim().to_owned(),
                    }));
                }
                InputMode::StopConfirm(_) => self.message = "Stop requires y confirmation".into(),
                InputMode::Promote { goal, stage, .. }
                    if *stage == PromoteStage::Goal && !goal.trim().is_empty() =>
                {
                    *stage = PromoteStage::Project;
                    self.message = "Choose the target project (j/k), then Enter to review".into();
                }
                InputMode::Promote {
                    projects,
                    selected,
                    stage,
                    ..
                } if *stage == PromoteStage::Project => {
                    let active = projects
                        .get(*selected)
                        .is_some_and(|project| project.active);
                    *stage = PromoteStage::Confirm;
                    self.message = if active {
                        "WARNING: this project already has an active run. Press y to start anyway, Esc to cancel".into()
                    } else {
                        "Review the promotion and press y to start PGE, or Esc to cancel".into()
                    };
                }
                InputMode::Promote { .. } => {
                    self.message = "Promotion requires an explicit y confirmation".into()
                }
                _ => self.message = "A goal or decision reason is required".into(),
            },
            _ => {}
        }
        self.dirty = true;
        None
    }
    fn visible_len(&self) -> usize {
        if self.offline_mode {
            self.offline_runs().len()
        } else {
            self.runs.len()
        }
    }

    /// Offline manifests are historical by nature. Rank live work first so an
    /// operator never lands on an unrelated old blocked run after opening TUI.
    fn offline_runs(&self) -> Vec<&forge_api::OfflineRunManifest> {
        let mut runs: Vec<_> = self.offline.values().collect();
        runs.sort_by(|left, right| {
            offline_status_rank(&left.status)
                .cmp(&offline_status_rank(&right.status))
                .then_with(|| right.updated_at.cmp(&left.updated_at))
        });
        runs
    }
    fn selected_run_id(&self) -> Option<forge_api::RunId> {
        if self.offline_mode {
            None
        } else {
            self.runs.get(self.selected).map(|run| run.id.clone())
        }
    }
    fn apply(&mut self, update: Bulk) {
        match update {
            Bulk::Live(runs, approvals) => {
                self.runs = runs;
                self.approvals = approvals;
                self.offline_mode = false;
                self.message = "Live control plane".into();
            }
            Bulk::Stream(events) => {
                for event in events {
                    self.push_event(format!("{}  {}", event.event_type, event.actor));
                }
            }
            Bulk::Pi(event) => match event {
                PiIncoming::MessageUpdate {
                    assistant_message_event,
                    ..
                } => {
                    let kind = assistant_message_event
                        .get("type")
                        .and_then(|value| value.as_str());
                    if kind == Some("text_delta")
                        && let Some(delta) = assistant_message_event
                            .get("delta")
                            .and_then(|value| value.as_str())
                    {
                        self.push_transcript_delta(delta);
                    } else if kind == Some("thinking_delta")
                        && let Some(delta) = assistant_message_event
                            .get("delta")
                            .and_then(|value| value.as_str())
                    {
                        self.push_thinking_delta(delta);
                    } else if matches!(
                        kind,
                        Some("toolcall_start" | "toolcall_delta" | "toolcall_end")
                    ) {
                        self.transcript
                            .push_back(format!("[tool] {}", kind.unwrap_or("toolcall")));
                    }
                }
                PiIncoming::AgentEnd { .. } => self.transcript.push_back("".into()),
                PiIncoming::ToolExecutionStart { tool_name, .. } => {
                    self.transcript.push_back(format!("[tool] {tool_name}…"));
                }
                PiIncoming::ToolExecutionEnd {
                    tool_name,
                    is_error,
                    ..
                } => {
                    let outcome = if is_error { "failed" } else { "done" };
                    self.transcript
                        .push_back(format!("[tool] {tool_name} {outcome}"));
                }
                PiIncoming::ExtensionUiRequest {
                    id,
                    method,
                    timeout,
                    details,
                } => {
                    if matches!(method.as_str(), "select" | "confirm" | "input" | "editor") {
                        let options = details
                            .get("options")
                            .and_then(|value| value.as_array())
                            .map(|values| {
                                values
                                    .iter()
                                    .filter_map(|value| value.as_str().map(str::to_owned))
                                    .collect()
                            })
                            .unwrap_or_default();
                        self.pi_dialog = Some(PiDialog {
                            id,
                            method: method.clone(),
                            value: String::new(),
                            options,
                            deadline: timeout.map(|milliseconds| {
                                Instant::now() + Duration::from_millis(milliseconds)
                            }),
                        });
                        self.transcript
                            .push_back(format!("[Pi extension dialog: {method}]"));
                    } else {
                        let detail = details
                            .get("message")
                            .or_else(|| details.get("title"))
                            .and_then(|value| value.as_str())
                            .unwrap_or(method.as_str());
                        self.message = format!("Pi: {detail}");
                        if method == "notify" {
                            self.transcript.push_back(format!("[Pi] {detail}"));
                        }
                    }
                }
                _ => {}
            },
            Bulk::SessionStats(stats) => self.session_footer = stats,
            Bulk::Offline(manifests) => {
                self.offline = manifests;
                self.offline_mode = true;
                self.message = "Control plane unavailable; showing read-only manifest".into();
            }
            Bulk::Error(error) => {
                self.message = error;
            }
        }
        self.selected = self.selected.min(self.visible_len().saturating_sub(1));
        self.dirty = true;
    }

    fn push_transcript_delta(&mut self, delta: &str) {
        if let Some(last) = self.transcript.back_mut()
            && !last.starts_with('>')
            && !last.starts_with('[')
        {
            last.push_str(delta);
            return;
        }
        if self.transcript.len() == MAX_SCROLLBACK {
            self.transcript.pop_front();
        }
        self.transcript.push_back(delta.to_owned());
    }

    fn push_thinking_delta(&mut self, delta: &str) {
        if let Some(last) = self.transcript.back_mut()
            && last.starts_with("[thinking] ")
        {
            last.push_str(delta);
            return;
        }
        if self.transcript.len() == MAX_SCROLLBACK {
            self.transcript.pop_front();
        }
        self.transcript.push_back(format!("[thinking] {delta}"));
    }

    fn dialog_deadline(&self) -> Option<Instant> {
        self.pi_dialog.as_ref().and_then(|dialog| dialog.deadline)
    }

    fn expire_dialog_if_due(&mut self, now: Instant) {
        if self
            .dialog_deadline()
            .is_some_and(|deadline| deadline <= now)
        {
            self.pi_dialog = None;
            self.message = "Pi extension dialog timed out".into();
            self.transcript
                .push_back("[Pi extension dialog timed out]".into());
            self.dirty = true;
        }
    }
}

/// Start the source-checkout control plane only for the default loopback
/// endpoint. Remote control planes are never started locally, and operators
/// can opt out with `FORGE_TUI_AUTOSTART_CONTROL_PLANE=0`.
async fn maybe_start_local_control_plane(
    api: &ForgeApi,
    config: &ForgeConfig,
) -> Result<Option<ManagedControlPlane>> {
    let disabled = matches!(
        std::env::var("FORGE_TUI_AUTOSTART_CONTROL_PLANE"),
        Ok(value) if value == "0" || value.eq_ignore_ascii_case("false")
    );
    let port = config.control_url.port_or_known_default();
    if !is_local_default_control_url(config.control_url.host_str(), port, disabled)
        || api.authenticated_ready().await.is_ok()
    {
        return Ok(None);
    }

    let root = forge_project_root().context(
        "automatic startup requires a Forge source checkout; start the configured control plane manually",
    )?;
    let log_path = config.home.join("logs/tui-control-plane.log");
    let parent = log_path
        .parent()
        .context("control-plane log path has no parent")?;
    fs::create_dir_all(parent).context("create control-plane log directory")?;
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .with_context(|| format!("open control-plane log {}", log_path.display()))?;
    let error_log = log.try_clone().context("clone control-plane log handle")?;
    let control_database = config.home.join("control-plane.db");
    let control_database_url = format!("sqlite:///{}", control_database.display());

    let mut child = Command::new("uv")
        .args(["run", "forge", "serve"])
        .current_dir(root)
        .env("FORGE_HOME", &config.home)
        .env("FORGE_CONTROL_TOKEN", &config.control_token)
        .env("FORGE_CONTROL_HOST", "127.0.0.1")
        .env("FORGE_CONTROL_PORT", port.unwrap_or(8787).to_string())
        // Keep control-plane approvals independent from a temporarily
        // unavailable engine Postgres. Runtime snapshots still read the
        // durable manifests and label any missing database enrichment.
        .env("FORGE_CONTROL_DATABASE_URL", control_database_url)
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(error_log))
        .spawn()
        .context("spawn `uv run forge serve`")?;

    let deadline = Instant::now() + CONTROL_PLANE_STARTUP_TIMEOUT;
    while Instant::now() < deadline {
        if api.authenticated_ready().await.is_ok() {
            return Ok(Some(ManagedControlPlane { child, log_path }));
        }
        if let Some(status) = child
            .try_wait()
            .context("check local control-plane process")?
        {
            bail!(
                "local control plane exited with {status}; inspect {}",
                log_path.display()
            );
        }
        tokio::time::sleep(CONTROL_PLANE_RETRY_INTERVAL).await;
    }

    let _ = child.kill().await;
    let _ = child.wait().await;
    bail!(
        "local control plane did not become healthy within {} seconds; inspect {}",
        CONTROL_PLANE_STARTUP_TIMEOUT.as_secs(),
        log_path.display()
    );
}

fn is_local_default_control_url(host: Option<&str>, port: Option<u16>, disabled: bool) -> bool {
    !disabled && matches!(host, Some("127.0.0.1" | "localhost" | "::1")) && port == Some(8787)
}

fn forge_project_root() -> Option<PathBuf> {
    let mut starting_points = Vec::new();
    if let Ok(current) = std::env::current_dir() {
        starting_points.push(current);
    }
    if let Ok(executable) = std::env::current_exe()
        && let Some(parent) = executable.parent()
    {
        starting_points.push(parent.to_path_buf());
    }
    starting_points.into_iter().find_map(|start| {
        start
            .ancestors()
            .find(|directory| {
                directory.join("pyproject.toml").is_file()
                    && directory.join("control_plane").is_dir()
            })
            .map(Path::to_path_buf)
    })
}

pub async fn run(api: ForgeApi, config: ForgeConfig, session_options: SpawnOptions) -> Result<()> {
    install_terminal_panic_hook();
    let (managed_control_plane, startup_message) =
        match maybe_start_local_control_plane(&api, &config).await {
            Ok(Some(managed)) => (
                Some(managed),
                Some(
                    "Started local control plane for this TUI; loading live operator state…".into(),
                ),
            ),
            Ok(None) => (None, None),
            Err(error) => (
                None,
                Some(format!("Could not start local control plane: {error}")),
            ),
        };
    let (priority_tx, mut priority_rx) = mpsc::channel(64);
    let (bulk_tx, mut bulk_rx) = mpsc::channel(1);
    let (pi_tx, mut pi_rx) = mpsc::channel(256);
    let (picker_tx, mut picker_rx) = mpsc::channel(1);
    let (stream_tx, mut stream_rx) = mpsc::channel(64);
    let (selection_tx, selection_rx) = watch::channel(None);
    spawn_input(priority_tx);
    let poll_api = api.clone();
    let poll_bulk = bulk_tx.clone();
    tokio::spawn(async move {
        loop {
            let update = match (
                poll_api.runtime_runs().await,
                poll_api.approvals(Some("pending")).await,
            ) {
                (Ok(runs), Ok(approvals)) => Bulk::Live(runs, approvals),
                (Err(error), _) => match load_offline(&config) {
                    Ok(manifests) => Bulk::Offline(manifests),
                    Err(_) => Bulk::Error(format!("Control plane unavailable: {error}")),
                },
                (_, Err(error)) => Bulk::Error(format!("Could not load approvals: {error}")),
            };
            let _ = poll_bulk.try_send(update); // a newer view supersedes a stale one
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    });
    spawn_selected_stream(api.clone(), selection_rx, stream_tx, bulk_tx.clone());
    let mut terminal = ratatui::init();
    let result = event_loop(
        &mut terminal,
        &api,
        &selection_tx,
        session_options,
        startup_message,
        LoopChannels {
            bulk_tx: bulk_tx.clone(),
            pi_tx,
            picker_tx,
            stream: &mut stream_rx,
            priority: &mut priority_rx,
            bulk: &mut bulk_rx,
            pi: &mut pi_rx,
            picker: &mut picker_rx,
        },
    )
    .await;
    ratatui::restore();
    if let Some(mut managed) = managed_control_plane {
        tracing::info!(log = %managed.log_path.display(), "stopping TUI-managed control plane");
        let _ = managed.child.kill().await;
        let _ = managed.child.wait().await;
    }
    result
}

fn install_terminal_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |panic| {
        ratatui::restore();
        previous(panic);
    }));
}

fn spawn_input(sender: mpsc::Sender<Priority>) {
    std::thread::spawn(move || {
        loop {
            match event::poll(Duration::from_millis(50)) {
                Ok(true) => match event::read() {
                    Ok(InputEvent::Key(key)) => {
                        if sender.blocking_send(Priority::Key(key)).is_err() {
                            return;
                        }
                    }
                    Ok(_) => {}
                    Err(_) => {
                        let _ = sender.blocking_send(Priority::Quit);
                        return;
                    }
                },
                Ok(false) => {}
                Err(_) => {
                    let _ = sender.blocking_send(Priority::Quit);
                    return;
                }
            }
        }
    });
}

/// The channel bundle the event loop selects over; grouping it keeps the
/// loop signature stable as feeds are added.
struct LoopChannels<'a> {
    bulk_tx: mpsc::Sender<Bulk>,
    pi_tx: mpsc::Sender<PiIncoming>,
    picker_tx: mpsc::Sender<PickerUpdate>,
    stream: &'a mut mpsc::Receiver<Vec<RunEvent>>,
    priority: &'a mut mpsc::Receiver<Priority>,
    bulk: &'a mut mpsc::Receiver<Bulk>,
    pi: &'a mut mpsc::Receiver<PiIncoming>,
    picker: &'a mut mpsc::Receiver<PickerUpdate>,
}

async fn event_loop<B: ratatui::backend::Backend>(
    terminal: &mut ratatui::Terminal<B>,
    api: &ForgeApi,
    selection: &watch::Sender<Option<forge_api::RunId>>,
    session_options: SpawnOptions,
    startup_message: Option<String>,
    channels: LoopChannels<'_>,
) -> Result<()> {
    let LoopChannels {
        bulk_tx,
        pi_tx,
        picker_tx,
        stream,
        priority,
        bulk,
        pi,
        picker,
    } = channels;
    let session_directory = session_options
        .session_dir
        .clone()
        .and_then(session_directory_for_root)
        .or_else(default_session_directory);
    let mut app = App::with_session_directory(session_directory);
    if let Some(message) = startup_message {
        app.message = message;
    }
    loop {
        if app.dirty {
            terminal.draw(|frame| draw(frame, &app))?;
            app.dirty = false;
        }
        let dialog_timeout = async {
            match app.dialog_deadline() {
                Some(deadline) => tokio::time::sleep_until(deadline).await,
                None => futures_util::future::pending::<()>().await,
            }
        };
        tokio::select! { biased;
            Some(event) = priority.recv() => match event {
                Priority::Quit => return Ok(()),
                Priority::Key(key) => match app.handle_key_event(key) {
                    Some(UiEvent::Quit) => return Ok(()),
                    Some(UiEvent::ToggleMode) => {
                        if app.mode == Mode::Session && app.session.is_none() {
                            match PiClient::spawn_with_options(&session_options).await {
                                Ok(client) => {
                                    spawn_pi_events(client.clone(), pi_tx.clone());
                                    spawn_session_stats(client.clone(), bulk_tx.clone());
                                    app.session = Some(client);
                                    app.transcript.push_back("Pi session ready.".into());
                                }
                                Err(error) => app.message = error.to_string(),
                            }
                        }
                        app.dirty = true;
                    }
                    Some(UiEvent::Action(Action::BeginPromote { context })) => {
                        let projects = match api.runtime_projects().await {
                            Ok(projects) => projects
                                .into_iter()
                                .map(|project| PromoteProject {
                                    active: project_has_active_run(&app.runs, &project.id),
                                    id: Some(project.id),
                                    name: project.name,
                                })
                                .collect(),
                            Err(error) => {
                                app.message = format!(
                                    "Project list unavailable ({error}); only the default project is offered"
                                );
                                Vec::new()
                            }
                        };
                        app.open_promote(context, projects);
                    }
                    Some(UiEvent::Action(Action::LoadSessionPicker)) => {
                        let directory = app.session_directory.clone();
                        let picker_tx = picker_tx.clone();
                        tokio::spawn(async move {
                            let update = tokio::task::spawn_blocking(move || {
                                list_pi_sessions(directory.as_deref())
                            })
                            .await
                            .unwrap_or_else(|error| Err(format!("session scan task failed: {error}")))
                            .map(PickerUpdate::Loaded)
                            .unwrap_or_else(PickerUpdate::Failed);
                            let _ = picker_tx.send(update).await;
                        });
                    }
                    Some(UiEvent::Action(Action::PiForkSession(path))) => {
                        // Forking a *different* session file is a spawn-time
                        // flag, not an RPC command: replace the child process.
                        if let Some(previous) = app.session.take() {
                            previous.shutdown().await;
                        }
                        let fork_options = SpawnOptions {
                            fork: Some(path.clone()),
                            ..session_options.clone()
                        };
                        match PiClient::spawn_with_options(&fork_options).await {
                            Ok(client) => {
                                spawn_pi_events(client.clone(), pi_tx.clone());
                                spawn_session_stats(client.clone(), bulk_tx.clone());
                                app.session = Some(client);
                                app.message = format!("Forked session {}", short_path(&path));
                                app.transcript.push_back(format!(
                                    "[forked from {}]",
                                    short_path(&path)
                                ));
                            }
                            Err(error) => app.message = format!("Fork failed: {error}"),
                        }
                        app.dirty = true;
                    }
                    Some(UiEvent::Action(action)) => {
                        app.input = None;
                        if matches!(&action, Action::PiDialogConfirm { .. } | Action::PiDialogValue { .. } | Action::PiDialogCancel { .. }) {
                            app.pi_dialog = None;
                        }
                        let refresh_stats = matches!(
                            &action,
                            Action::PiControl { .. } | Action::PiSwitchSession(_)
                        );
                        app.message = execute(api, app.session.as_ref(), action).await;
                        if refresh_stats && let Some(client) = app.session.clone() {
                            spawn_session_stats(client, bulk_tx.clone());
                        }
                        app.dirty = true;
                    }
                    None => {
                        let next = app.selected_run_id();
                        selection.send_if_modified(|current| {
                            if *current == next { false } else { *current = next; true }
                        });
                    }
                },
            },
            Some(update) = bulk.recv() => {
                app.apply(update);
                let next = app.selected_run_id();
                selection.send_if_modified(|current| {
                    if *current == next { false } else { *current = next; true }
                });
            },
            Some(event) = pi.recv() => {
                let refresh_stats = matches!(event, PiIncoming::AgentEnd { .. });
                app.apply(Bulk::Pi(event));
                if refresh_stats && let Some(client) = app.session.clone() {
                    spawn_session_stats(client, bulk_tx.clone());
                }
            },
            Some(update) = picker.recv() => match update {
                PickerUpdate::Loaded(entries) => app.open_session_picker(entries),
                PickerUpdate::Failed(error) => { app.message = format!("Could not load Pi sessions: {error}"); app.dirty = true; }
            },
            Some(events) = stream.recv() => { app.apply(Bulk::Stream(events)); },
            _ = dialog_timeout => app.expire_dialog_if_due(Instant::now()),
            else => return Ok(()),
        }
    }
}

fn spawn_selected_stream(
    api: ForgeApi,
    mut selection: watch::Receiver<Option<forge_api::RunId>>,
    stream_tx: mpsc::Sender<Vec<RunEvent>>,
    notice_tx: mpsc::Sender<Bulk>,
) {
    tokio::spawn(async move {
        loop {
            let Some(run_id) = selection.borrow_and_update().clone() else {
                if selection.changed().await.is_err() {
                    return;
                }
                continue;
            };
            let mut cursor = 0_u64;
            let mut attempt = 0_u32;
            'selected: loop {
                let stream = match api.event_stream(&run_id, cursor).await {
                    Ok(stream) => {
                        if attempt > 0 {
                            let _ =
                                notice_tx.try_send(Bulk::Error("Event stream reconnected".into()));
                        }
                        attempt = 0;
                        stream
                    }
                    Err(error) => {
                        tracing::warn!(%error, "selected run event stream failed");
                        let _ = notice_tx
                            .try_send(Bulk::Error(format!("Event stream reconnecting… ({error})")));
                        attempt += 1;
                        let delay = Duration::from_millis(250 * u64::from(attempt.min(5)));
                        tokio::select! {
                            changed = selection.changed() => {
                                if changed.is_err() { return; }
                                break 'selected;
                            }
                            _ = tokio::time::sleep(delay) => {}
                        }
                        continue;
                    }
                };
                let mut stream = Box::pin(stream);
                let mut batch = Vec::new();
                let mut flush = tokio::time::interval(Duration::from_millis(50));
                loop {
                    tokio::select! {
                        changed = selection.changed() => {
                            if changed.is_ok() { break 'selected; }
                            return;
                        }
                        _ = flush.tick() => {
                            if !batch.is_empty() { let _ = stream_tx.send(std::mem::take(&mut batch)).await; }
                        }
                        item = stream.next() => match item {
                            Some(Ok(event)) => {
                                if let Some(id) = event.id.as_deref().and_then(|id| id.parse().ok()) { cursor = id; }
                                if let Ok(record) = serde_json::from_str::<RunEvent>(&event.data) {
                                    batch.push(record);
                                    if batch.len() >= 64 { let _ = stream_tx.send(std::mem::take(&mut batch)).await; }
                                }
                            }
                            Some(Err(error)) => { tracing::warn!(%error, "selected run event stream failed"); break; }
                            None => break,
                        },
                    }
                }
                if !batch.is_empty() {
                    let _ = stream_tx.send(batch).await;
                }
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }
    });
}

fn spawn_pi_events(client: PiClient, pi: mpsc::Sender<PiIncoming>) {
    tokio::spawn(async move {
        let mut events = client.events();
        while let Ok(event) = events.recv().await {
            if pi.send(event).await.is_err() {
                return;
            }
        }
    });
}

fn spawn_session_stats(client: PiClient, bulk: mpsc::Sender<Bulk>) {
    tokio::spawn(async move {
        let result = tokio::try_join!(
            client.send(PiCommand::GetState { id: None }),
            client.send(PiCommand::GetSessionStats { id: None }),
        );
        let stats = match result {
            Ok((state, stats)) => {
                let model = state["data"]["model"]["name"]
                    .as_str()
                    .or_else(|| state["data"]["model"]["id"].as_str())
                    .unwrap_or("unknown model");
                let thinking = state["data"]["thinkingLevel"].as_str().unwrap_or("unknown");
                let context = stats["data"]["contextUsage"]["percent"]
                    .as_f64()
                    .map(|value| format!("{value:.0}%"))
                    .unwrap_or_else(|| "?%".into());
                let cost = stats["data"]["cost"]
                    .as_f64()
                    .map(|value| format!("${value:.2}"))
                    .unwrap_or_else(|| "$?".into());
                format!("{model} · {thinking} · ctx {context} · {cost}")
            }
            Err(error) => format!("stats unavailable: {error}"),
        };
        let _ = bulk.try_send(Bulk::SessionStats(stats));
    });
}

async fn execute(api: &ForgeApi, session: Option<&PiClient>, action: Action) -> String {
    match action {
        Action::Start(goal) => match api
            .start_run(&RuntimeRunStart {
                goal,
                description: String::new(),
                project_id: None,
            })
            .await
        {
            Ok(result) if result.started => format!("Run {} started", short(&result.run_id.0)),
            Ok(result) if result.already_running => format!(
                "Run not started: project already has an active run ({})",
                short(&result.run_id.0)
            ),
            Ok(result) => format!("Run not started (status: {})", result.status),
            Err(error) => format!("Start failed: {error}"),
        },
        Action::Stop(project_id) => match api.stop_run(&project_id).await {
            Ok(_) => format!("Stop requested for {}", project_id.0),
            Err(error) => format!("Stop failed: {error}"),
        },
        Action::Decide {
            id,
            approved,
            reason,
        } => match api
            .decide_approval(
                &id,
                &ApprovalDecision {
                    actor: "forge-tui".into(),
                    approved,
                    reason,
                },
            )
            .await
        {
            Ok(_) => format!("Approval {}", if approved { "approved" } else { "denied" }),
            Err(error) => format!("Approval decision failed: {error}"),
        },
        Action::PiPrompt(prompt) => match session {
            Some(client) => match client
                .send(PiCommand::Prompt {
                    id: None,
                    message: prompt,
                    streaming_behavior: None,
                })
                .await
            {
                Ok(_) => "Prompt sent".into(),
                Err(error) => format!("Pi prompt failed: {error}"),
            },
            None => "Pi is not available".into(),
        },
        Action::PiAbort => match session {
            Some(client) => match client.send(PiCommand::Abort { id: None }).await {
                Ok(_) => "Pi abort requested".into(),
                Err(error) => format!("Pi abort failed: {error}"),
            },
            None => "Pi is not available".into(),
        },
        Action::PiDialogConfirm { id, confirmed } => {
            pi_dialog_response(
                session,
                PiCommand::ExtensionUiResponse {
                    id,
                    confirmed: Some(confirmed),
                    value: None,
                    cancelled: None,
                },
            )
            .await
        }
        Action::PiDialogValue { id, value } => {
            pi_dialog_response(
                session,
                PiCommand::ExtensionUiResponse {
                    id,
                    confirmed: None,
                    value: Some(value),
                    cancelled: None,
                },
            )
            .await
        }
        Action::PiDialogCancel { id } => {
            pi_dialog_response(
                session,
                PiCommand::ExtensionUiResponse {
                    id,
                    confirmed: None,
                    value: None,
                    cancelled: Some(true),
                },
            )
            .await
        }
        Action::PiSteer(message) => match session {
            Some(client) => match client.send(PiCommand::Steer { id: None, message }).await {
                Ok(_) => "Steer message queued for the current turn".into(),
                Err(error) => format!("Pi steer failed: {error}"),
            },
            None => "Pi is not available".into(),
        },
        Action::PiFollowUp(message) => match session {
            Some(client) => match client.send(PiCommand::FollowUp { id: None, message }).await {
                Ok(_) => "Follow-up queued for after the agent settles".into(),
                Err(error) => format!("Pi follow-up failed: {error}"),
            },
            None => "Pi is not available".into(),
        },
        Action::PiSwitchSession(path) => match session {
            Some(client) => match client
                .send(PiCommand::SwitchSession {
                    id: None,
                    session_path: path.clone(),
                })
                .await
            {
                Ok(_) => format!("Switched to session {}", short_path(&path)),
                Err(error) => format!("Session switch failed: {error}"),
            },
            None => "Pi is not available".into(),
        },
        Action::BeginPromote { .. } | Action::PiForkSession(_) | Action::LoadSessionPicker => {
            unreachable!("handled by the event loop before execute")
        }
        Action::Promote {
            goal,
            context,
            project,
        } => match api
            .start_run(&RuntimeRunStart {
                goal,
                description: context,
                project_id: project,
            })
            .await
        {
            Ok(result) if result.started => {
                format!("Promoted session to PGE run {}", short(&result.run_id.0))
            }
            Ok(result) if result.already_running => format!(
                "Promotion not started: project already has an active run ({})",
                short(&result.run_id.0)
            ),
            Ok(result) => format!("Promotion not started (status: {})", result.status),
            Err(error) => format!("Promotion failed: {error}"),
        },
        Action::LoadProviders => match api.providers().await {
            Ok(providers) if providers.profiles.is_empty() => "No configured providers".into(),
            Ok(providers) => providers
                .profiles
                .into_iter()
                .map(|(role, profile)| {
                    format!(
                        "{role}: {} / {}",
                        profile.model.unwrap_or_else(|| "(no model)".into()),
                        profile.base_url.unwrap_or_else(|| "(no URL)".into())
                    )
                })
                .collect::<Vec<_>>()
                .join(" | "),
            Err(error) => format!("Provider panel failed: {error}"),
        },
        Action::PiControl { command, label } => match session {
            Some(client) => match client.send(command).await {
                Ok(_) => format!("{label} requested"),
                Err(error) => format!("{label} failed: {error}"),
            },
            None => "Pi is not available".into(),
        },
    }
}

async fn pi_dialog_response(session: Option<&PiClient>, response: PiCommand) -> String {
    match session {
        Some(client) => match client.send_untracked(response).await {
            Ok(()) => "Pi extension dialog answered".into(),
            Err(error) => format!("Pi dialog response failed: {error}"),
        },
        None => "Pi is not available".into(),
    }
}

fn load_offline(config: &ForgeConfig) -> Result<OfflineRunManifests> {
    Ok(serde_json::from_slice(&fs::read(config.manifest_path())?)?)
}

fn project_has_active_run(runs: &[RuntimeRunSnapshot], project_id: &ProjectId) -> bool {
    runs.iter().any(|run| {
        run.project_id == *project_id
            && matches!(run.status.as_str(), "running" | "starting" | "launching")
    })
}

/// Pi stores sessions under `<root>/<sanitized cwd>/`, where the root respects
/// the same CLI option and environment variable as the spawned Pi process.
fn default_session_directory() -> Option<PathBuf> {
    let root = std::env::var_os("PI_CODING_AGENT_SESSION_DIR")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".pi/agent/sessions"))
        })?;
    session_directory_for_root(root)
}

fn session_directory_for_root(root: PathBuf) -> Option<PathBuf> {
    let cwd = std::env::current_dir().ok()?;
    let sanitized = format!("-{}--", cwd.display().to_string().replace('/', "-"));
    Some(root.join(sanitized))
}

fn list_pi_sessions(directory: Option<&Path>) -> Result<Vec<SessionEntry>, String> {
    let Some(directory) = directory else {
        return Ok(Vec::new());
    };
    let reader = match fs::read_dir(directory) {
        Ok(reader) => reader,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error.to_string()),
    };
    let mut entries: Vec<SessionEntry> = reader
        .filter_map(|entry| entry.ok())
        .filter(|entry| {
            entry
                .path()
                .extension()
                .is_some_and(|extension| extension == "jsonl")
        })
        .map(|entry| {
            let path = entry.path();
            let fallback_name = path
                .file_stem()
                .and_then(|stem| stem.to_str())
                .unwrap_or("session")
                .to_owned();
            let (name, entries) = session_summary(&path, fallback_name);
            let modified = entry
                .metadata()
                .ok()
                .and_then(|metadata| metadata.modified().ok())
                .and_then(|time| time.elapsed().ok())
                .map(|age| format!("{}m ago", age.as_secs() / 60))
                .unwrap_or_else(|| "unknown age".into());
            SessionEntry {
                path: path.display().to_string(),
                name,
                modified,
                entries,
            }
        })
        .collect();
    entries.sort_by(|a, b| b.name.cmp(&a.name)); // timestamped names: newest first
    Ok(entries)
}

/// Read at most 2 MiB so a corrupt or exceptionally large transcript cannot
/// monopolize the TUI's background scanner.
fn session_summary(path: &Path, fallback_name: String) -> (String, String) {
    let Ok(file) = fs::File::open(path) else {
        return (fallback_name, "unknown entries".into());
    };
    let mut reader = BufReader::new(file).take(2 * 1024 * 1024);
    let mut first = String::new();
    let first_read = reader.read_line(&mut first).ok().unwrap_or_default();
    let name = serde_json::from_str::<serde_json::Value>(&first)
        .ok()
        .and_then(|value| {
            value
                .get("name")
                .and_then(|name| name.as_str())
                .map(str::to_owned)
        })
        .unwrap_or(fallback_name);
    let count = usize::from(first_read > 0) + reader.lines().map_while(Result::ok).count();
    let capped = path
        .metadata()
        .map(|metadata| metadata.len() > 2 * 1024 * 1024)
        .unwrap_or(false);
    let entries = if capped {
        format!("{count}+ entries")
    } else {
        format!("{count} entries")
    };
    (name, entries)
}

fn short_path(path: &str) -> &str {
    std::path::Path::new(path)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(path)
}

fn draw(frame: &mut Frame, app: &App) {
    // Pi is deliberately part of the operator surface rather than a separate
    // screen. Tab only changes keyboard focus, so run state and the evidence
    // behind it remain visible while an operator steers a session.
    // Preserve enough vertical space for the selected-run audit on normal
    // laptop terminals. The activity strip appears only when it can do so
    // without hiding the next action and evidence in the detail pane.
    let show_pi_activity = frame.area().height >= 32;
    let [header, summary, body, pi_activity, editor, footer] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Length(3),
        Constraint::Min(10),
        Constraint::Length(if show_pi_activity { 3 } else { 0 }),
        Constraint::Length(3),
        Constraint::Length(2),
    ])
    .areas(frame.area());
    let compact = frame.area().width < 100;
    let [runs, detail] = if compact {
        Layout::vertical([Constraint::Percentage(42), Constraint::Percentage(58)]).areas(body)
    } else {
        Layout::horizontal([Constraint::Percentage(42), Constraint::Percentage(58)]).areas(body)
    };
    let banner = if app.offline_mode {
        Span::styled(
            " OFFLINE ",
            Style::default()
                .fg(Color::Black)
                .bg(THEME.danger)
                .add_modifier(Modifier::BOLD),
        )
    } else {
        Span::styled(
            " LIVE ",
            Style::default()
                .fg(Color::Black)
                .bg(THEME.verdigris)
                .add_modifier(Modifier::BOLD),
        )
    };
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                " FORGE ",
                Style::default()
                    .fg(Color::Black)
                    .bg(THEME.amber)
                    .add_modifier(Modifier::BOLD),
            ),
            banner,
            Span::raw(if app.offline_mode {
                format!("  operator · {}", offline_summary(app))
            } else {
                "  operator".into()
            }),
            Span::styled(
                if app.mode == Mode::Session {
                    "  ·  PI PROMPT FOCUS"
                } else {
                    "  ·  RUN LIST FOCUS"
                },
                Style::default().fg(THEME.dim),
            ),
        ]))
        .block(Block::default().borders(Borders::BOTTOM)),
        header,
    );
    let summary_text = if app.offline_mode {
        let counts = offline_counts(app);
        format!(
            "  RUNS  [ACTIVE {}]  [ATTENTION {}]  [COMPLETE {}]   |   select with j/k · inspect with e",
            counts.active, counts.attention, counts.completed
        )
    } else {
        format!(
            "  LIVE CONTROL PLANE  [RUNS {}]  [PENDING APPROVALS {}]",
            app.runs.len(),
            app.approvals.len()
        )
    };
    frame.render_widget(
        Paragraph::new(summary_text)
            .style(Style::default().add_modifier(Modifier::BOLD))
            .block(Block::default().title(" Status ").borders(Borders::ALL)),
        summary,
    );
    let items: Vec<ListItem> = if app.offline_mode {
        app.offline_runs()
            .into_iter()
            .map(|run| {
                ListItem::new(format!(
                    "{}  {}  {}",
                    status_label(&run.status),
                    short(&run.run_id.0),
                    offline_goal(run).unwrap_or("(goal unavailable)")
                ))
            })
            .collect()
    } else {
        app.runs
            .iter()
            .map(|run| {
                ListItem::new(format!(
                    "{}  {}  {}",
                    status_label(&run.status),
                    short(&run.id.0),
                    run.current_node
                ))
            })
            .collect()
    };
    let has_runs = !items.is_empty();
    let items = if items.is_empty() {
        vec![ListItem::new("No runs.")]
    } else {
        items
    };
    let runs_title = if app.offline_mode {
        format!(" Runs · {} ", offline_summary(app))
    } else {
        " Runs ".into()
    };
    let mut run_list_state = ListState::default();
    run_list_state.select(has_runs.then_some(app.selected));
    frame.render_stateful_widget(
        List::new(items)
            .block(Block::default().title(runs_title).borders(Borders::ALL))
            .highlight_style(
                Style::default()
                    .bg(Color::DarkGray)
                    .add_modifier(Modifier::BOLD),
            )
            .highlight_symbol("▶ "),
        runs,
        &mut run_list_state,
    );
    let mut lines = operator_detail_lines(app);
    if let Some(approval) = app.approvals.first() {
        lines.push(Line::from(""));
        lines.push(Line::styled(
            format!(
                "APPROVAL PENDING: {} ({})",
                approval.action_type, approval.risk
            ),
            Style::default()
                .fg(THEME.amber)
                .add_modifier(Modifier::BOLD),
        ));
    }
    frame.render_widget(
        Paragraph::new(lines).block(
            Block::default()
                .title(if app.show_evidence {
                    " Audit trail · selected run · e hide evidence "
                } else {
                    " Audit trail · selected run · e show evidence "
                })
                .borders(Borders::ALL),
        ),
        detail,
    );
    if show_pi_activity {
        frame.render_widget(
            Paragraph::new(pi_activity_lines(app)).block(
                Block::default()
                    .title(" Pi activity · recent session evidence ")
                    .borders(Borders::ALL),
            ),
            pi_activity,
        );
    }
    frame.render_widget(&app.editor, editor);
    let footer_text = if app.mode == Mode::Session {
        format!(
            " PI PROMPT FOCUS · {} · Esc actions · Ctrl+M model · Ctrl+T thinking · Ctrl+K compact · Ctrl+R sessions · Ctrl+G promote · Tab run list · Ctrl+Q quit ",
            app.session_footer
        )
    } else if app.offline_mode {
        " OFFLINE READ-ONLY · RUN LIST FOCUS · j/k select · e evidence · Tab Pi prompt · q quit "
            .into()
    } else if app.help {
        " RUN LIST FOCUS · j/k select · q quit · ? close help · s start · x stop · a approvals · y/n decide · Tab Pi prompt ".into()
    } else {
        " RUN LIST FOCUS · ? help · q quit · j/k select · s start · x stop · a approvals · Tab Pi prompt ".into()
    };
    frame.render_widget(
        Paragraph::new(footer_text).block(Block::default().borders(Borders::TOP)),
        footer,
    );
    draw_session_overlays(frame, app);
}

/// Only the latest session observation is shown in the dashboard.
/// The full Pi transcript remains available through the saved session, while
/// this bounded excerpt makes model/tool activity auditable without crowding
/// the run evidence or letting an unbounded stream dominate the render loop.
fn pi_activity_lines(app: &App) -> Vec<Line<'static>> {
    if app.transcript.is_empty() {
        return vec![Line::styled(
            "No Pi session activity yet. Press Tab to focus this prompt; Pi starts on first focus.",
            Style::default().fg(THEME.dim),
        )];
    }
    app.transcript
        .iter()
        .rev()
        .take(1)
        .rev()
        .map(|line| Line::styled(line.clone(), Style::default().fg(THEME.dim)))
        .collect()
}

#[derive(Default)]
struct OfflineCounts {
    active: usize,
    attention: usize,
    completed: usize,
}

fn offline_counts(app: &App) -> OfflineCounts {
    let mut counts = OfflineCounts::default();
    for run in app.offline.values() {
        match run.status.as_str() {
            "running" | "starting" | "launching" => counts.active += 1,
            "failed" | "blocked" => counts.attention += 1,
            "completed" => counts.completed += 1,
            _ => {}
        }
    }
    counts
}

/// Status is always textually explicit. Color is a secondary affordance so
/// monochrome terminals and screen readers retain the same meaning.
fn status_label(status: &str) -> String {
    match status {
        "running" | "starting" | "launching" => "[RUN]".into(),
        "completed" => "[OK]".into(),
        "blocked" => "[BLOCKED]".into(),
        "failed" => "[FAILED]".into(),
        "stopped" => "[STOPPED]".into(),
        other => format!("[{}]", other.to_uppercase()),
    }
}

/// Show the evidence behind the selected run, including offline log output.
/// Offline mode deliberately remains read-only, but it must not be opaque: an
/// operator needs enough provenance to decide whether to restart, investigate,
/// or wait for the control plane to return.
fn operator_detail_lines(app: &App) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(app.message.clone()), Line::from("")];
    if app.offline_mode {
        if let Some(run) = app.offline_runs().get(app.selected).copied() {
            lines.push(Line::styled(
                "audit source durable offline manifest · evidence is read-only",
                Style::default().fg(THEME.dim),
            ));
            lines.push(Line::styled(
                format!(
                    "run {} · project {}",
                    short(&run.run_id.0),
                    short(&run.project_id.0)
                ),
                Style::default()
                    .fg(THEME.verdigris)
                    .add_modifier(Modifier::BOLD),
            ));
            lines.push(Line::from(format!(
                "status {} · batch {} · pid {}",
                run.status,
                run.batch,
                run.pid
                    .map_or_else(|| "unknown".into(), |pid| pid.to_string()),
            )));
            if let Some(decision) = &run.final_decision {
                lines.push(Line::styled(
                    format!(
                        "outcome {decision} · exit {}",
                        run.exit_code
                            .map_or_else(|| "unknown".into(), |code| code.to_string())
                    ),
                    if decision == "complete" {
                        Style::default().fg(THEME.verdigris)
                    } else {
                        Style::default().fg(THEME.danger)
                    },
                ));
            }
            if let Some(finished_at) = &run.finished_at {
                lines.push(Line::styled(
                    format!("finished {finished_at}"),
                    Style::default().fg(THEME.dim),
                ));
            }
            if let Some(goal) = offline_goal(run) {
                lines.push(Line::styled(
                    format!("goal {goal}"),
                    Style::default().fg(THEME.amber),
                ));
            }
            if let Some(source) = &run.source {
                lines.push(Line::styled(
                    format!("source {source}"),
                    Style::default().fg(THEME.dim),
                ));
            }
            if let Some(heartbeat) = &run.heartbeat_at {
                lines.push(Line::styled(
                    format!("heartbeat {heartbeat}"),
                    Style::default().fg(THEME.dim),
                ));
            }
            if let Some(reason) = &run.terminal_reason {
                lines.push(Line::styled(
                    format!("terminal reason {reason}"),
                    Style::default().fg(THEME.danger),
                ));
            }
            lines.push(Line::from(""));
            lines.push(Line::styled(
                format!("next action: {}", offline_next_action(run)),
                Style::default()
                    .fg(THEME.verdigris)
                    .add_modifier(Modifier::BOLD),
            ));
            if let Some(updated_at) = &run.updated_at {
                lines.push(Line::styled(
                    format!("updated {updated_at}"),
                    Style::default().fg(THEME.dim),
                ));
            }
            if app.show_evidence
                && let Some(log) = &run.log
            {
                lines.push(Line::styled(
                    format!("log {log}"),
                    Style::default().fg(THEME.dim),
                ));
                let tail = bounded_log_tail(Path::new(log), 8);
                if !tail.is_empty() {
                    lines.push(Line::from(""));
                    lines.push(Line::styled(
                        "recent evidence",
                        Style::default().fg(THEME.amber),
                    ));
                    lines.extend(
                        tail.into_iter()
                            .map(|line| Line::styled(line, Style::default().fg(THEME.dim))),
                    );
                }
            }
        } else {
            lines.push(Line::styled(
                "No selected manifest.",
                Style::default().fg(THEME.dim),
            ));
        }
    } else {
        if let Some(run) = app.runs.get(app.selected) {
            lines.push(Line::styled(
                "audit source control-plane snapshot · recent event stream",
                Style::default().fg(THEME.dim),
            ));
            lines.push(Line::styled(
                format!("{} · {}", run.project_name, run.goal_title),
                Style::default()
                    .fg(THEME.verdigris)
                    .add_modifier(Modifier::BOLD),
            ));
            lines.push(Line::from(format!(
                "node {} · batch {} · pid {:?}",
                run.current_node, run.batch, run.pid
            )));
            lines.extend(
                run.log_tail
                    .iter()
                    .rev()
                    .take(8)
                    .rev()
                    .cloned()
                    .map(|line| Line::styled(line, Style::default().fg(THEME.dim))),
            );
        }
        lines.extend(
            app.events
                .iter()
                .rev()
                .take(8)
                .rev()
                .map(|line| Line::styled(line.clone(), Style::default().fg(THEME.dim))),
        );
    }
    lines
}

fn offline_status_rank(status: &str) -> u8 {
    match status {
        "running" | "starting" | "launching" => 0,
        "failed" | "blocked" => 1,
        _ => 2,
    }
}

fn offline_summary(app: &App) -> String {
    let counts = offline_counts(app);
    format!(
        "{} active · {} attention · {} complete",
        counts.active, counts.attention, counts.completed
    )
}

fn offline_goal(run: &forge_api::OfflineRunManifest) -> Option<&str> {
    run.invocation.get("goal")?.as_str()
}

/// A deterministic operator recommendation. This is intentionally derived
/// only from durable status and terminal metadata; the model never decides
/// whether an operator should restart, wait, or inspect an incident.
fn offline_next_action(run: &forge_api::OfflineRunManifest) -> &'static str {
    match run.status.as_str() {
        "running" | "starting" | "launching" => {
            "observe recent evidence; wait for a terminal result before starting another run"
        }
        "completed" => "review the generated workspace and start the next goal when ready",
        "blocked" => "inspect the terminal reason and log evidence before retrying",
        "failed" => "inspect the failure evidence, repair the cause, then retry deliberately",
        "stopped" => "confirm why it was stopped before restarting",
        _ => "inspect the audit trail before taking action",
    }
}

/// Read only the final 64 KiB of a log so a corrupt/unbounded run log cannot
/// stall the render loop. Errors intentionally render as no evidence rather
/// than making the operator surface fail.
fn bounded_log_tail(path: &Path, limit: usize) -> Vec<String> {
    let Ok(mut file) = fs::File::open(path) else {
        return Vec::new();
    };
    let offset = file
        .metadata()
        .map(|meta| meta.len().saturating_sub(64 * 1024))
        .unwrap_or(0);
    if file.seek(SeekFrom::Start(offset)).is_err() {
        return Vec::new();
    }
    let mut text = String::new();
    if file.read_to_string(&mut text).is_err() {
        return Vec::new();
    }
    let lines: Vec<_> = text.lines().collect();
    let first = lines.len().saturating_sub(limit);
    lines[first..]
        .iter()
        .map(|line| (*line).to_owned())
        .collect()
}

fn draw_session_overlays(frame: &mut Frame, app: &App) {
    if app.session_menu {
        let area = overlay_area(frame, 5);
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(vec![
                Line::from("a  abort the current turn"),
                Line::from("s  steer with the editor text (delivered mid-turn)"),
                Line::from("f  follow-up with the editor text (after settle)"),
            ])
            .block(
                Block::default()
                    .title(" Session menu ")
                    .borders(Borders::ALL),
            ),
            area,
        );
    }
    if let Some(picker) = &app.session_picker {
        let area = overlay_area(frame, 12);
        frame.render_widget(Clear, area);
        let items: Vec<ListItem> = if picker.entries.is_empty() {
            vec![ListItem::new("No saved sessions for this directory.")]
        } else {
            picker
                .entries
                .iter()
                .enumerate()
                .map(|(index, entry)| {
                    let marker = if index == picker.selected {
                        "› "
                    } else {
                        "  "
                    };
                    ListItem::new(format!(
                        "{marker}{}  ({} · {})",
                        entry.name, entry.modified, entry.entries
                    ))
                })
                .collect()
        };
        frame.render_widget(
            List::new(items).block(
                Block::default()
                    .title(" Sessions — enter switch · f fork · esc close ")
                    .borders(Borders::ALL),
            ),
            area,
        );
    }
    if let Some(InputMode::Promote {
        goal,
        context,
        projects,
        selected,
        stage,
    }) = &app.input
    {
        let area = overlay_area(frame, 12);
        frame.render_widget(Clear, area);
        let mut lines = vec![
            Line::from(format!("Goal: {goal}")),
            Line::from(Span::styled(
                format!("Context: {context}"),
                Style::default().fg(THEME.dim),
            )),
            Line::from(""),
        ];
        match stage {
            PromoteStage::Goal => lines.push(Line::from("Type the goal, then press Enter.")),
            PromoteStage::Project | PromoteStage::Confirm => {
                for (index, project) in projects.iter().enumerate() {
                    let marker = if index == *selected { "› " } else { "  " };
                    let active = if project.active {
                        "  ● active run"
                    } else {
                        ""
                    };
                    let line = format!("{marker}{}{active}", project.name);
                    lines.push(if project.active {
                        Line::styled(line, Style::default().fg(THEME.danger))
                    } else {
                        Line::from(line)
                    });
                }
                lines.push(Line::from(""));
                lines.push(Line::from(if *stage == PromoteStage::Confirm {
                    "Press y to start the durable PGE run, Esc to cancel."
                } else {
                    "j/k select the project, Enter to review."
                }));
            }
        }
        frame.render_widget(
            Paragraph::new(lines).block(
                Block::default()
                    .title(" Promote to PGE goal ")
                    .borders(Borders::ALL),
            ),
            area,
        );
    }
    if let Some(dialog) = &app.pi_dialog {
        let area = ratatui::layout::Rect {
            x: frame.area().width / 8,
            y: frame.area().height / 3,
            width: frame.area().width.saturating_mul(3) / 4,
            height: 5,
        };
        frame.render_widget(Clear, area);
        let prompt = if dialog.method == "confirm" {
            "Pi asks for confirmation. Press y or n.".to_owned()
        } else {
            let options = if dialog.options.is_empty() {
                String::new()
            } else {
                format!(" Options: {}", dialog.options.join(" | "))
            };
            format!(
                "Pi requests {}. Enter a value: {}{}",
                dialog.method, dialog.value, options
            )
        };
        frame.render_widget(
            Paragraph::new(prompt)
                .block(Block::default().title(" Pi dialog ").borders(Borders::ALL)),
            area,
        );
    }
}

fn short(value: &str) -> &str {
    &value[..value.len().min(8)]
}

fn overlay_area(frame: &Frame, height: u16) -> ratatui::layout::Rect {
    ratatui::layout::Rect {
        x: frame.area().width / 8,
        y: frame.area().height / 4,
        width: frame.area().width.saturating_mul(3) / 4,
        height: height.min(frame.area().height.saturating_sub(2)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    #[test]
    fn offline_view_is_unmistakable_and_scrollback_is_capped() {
        let mut app = App::new();
        for index in 0..=MAX_SCROLLBACK {
            app.push_event(format!("event {index}"));
        }
        assert_eq!(app.events.len(), MAX_SCROLLBACK);
        app.apply(Bulk::Offline(OfflineRunManifests::new()));
        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).expect("terminal");
        terminal.draw(|frame| draw(frame, &app)).expect("draw");
        let rendered: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(rendered.contains("OFFLINE"));
        assert!(rendered.contains("READ-ONLY"));
        assert!(rendered.contains("No runs."));
        assert!(rendered.contains("Pi prompt"));
        assert!(rendered.contains("RUN LIST FOCUS"));
    }

    #[test]
    fn offline_audit_trail_shows_selected_manifest_and_bounded_log_evidence() {
        let directory = tempfile::tempdir().expect("temporary log directory");
        let log = directory.path().join("run.log");
        fs::write(
            &log,
            "planner started\nexecutor wrote main.py\nverification passed\n",
        )
        .expect("write fixture log");
        let mut app = App::new();
        app.offline.insert(
            "project-1".into(),
            forge_api::OfflineRunManifest {
                run_id: forge_api::RunId("run-12345678".into()),
                project_id: ProjectId("project-12345678".into()),
                status: "blocked".into(),
                batch: 2,
                pid: Some(42),
                updated_at: Some("2026-07-13T18:00:00Z".into()),
                log: Some(log.display().to_string()),
                source: Some("eval-tui".into()),
                heartbeat_at: Some("2026-07-13T18:00:01Z".into()),
                terminal_reason: Some("stagnant_durable_state".into()),
                final_decision: Some("complete".into()),
                exit_code: Some(0),
                finished_at: Some("2026-07-13T18:01:00Z".into()),
                invocation: serde_json::json!({"goal":"Build a Snake game"}),
            },
        );
        app.offline_mode = true;
        app.show_evidence = true;
        let backend = TestBackend::new(100, 36);
        let mut terminal = Terminal::new(backend).expect("terminal");
        terminal.draw(|frame| draw(frame, &app)).expect("draw");
        let rendered: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(rendered.contains("Audit trail"));
        assert!(rendered.contains("▶"));
        assert!(rendered.contains("0 active · 1 attention · 0 complete"));
        assert!(rendered.contains("project project"));
        assert!(rendered.contains("Build a Snake game"));
        assert!(rendered.contains("next action: inspect the terminal reason"));
        assert!(rendered.contains("executor wrote main.py"));
    }

    #[test]
    fn evidence_toggle_is_explicit_and_does_not_change_selected_run() {
        let mut app = App::new();
        assert!(!app.show_evidence);
        assert!(app.handle_key(KeyCode::Char('e')).is_none());
        assert!(app.show_evidence);
        assert!(app.message.contains("Showing selected-run evidence"));
        assert!(app.handle_key(KeyCode::Char('e')).is_none());
        assert!(!app.show_evidence);
    }

    #[test]
    fn status_labels_are_understandable_without_color() {
        assert_eq!(status_label("running"), "[RUN]");
        assert_eq!(status_label("completed"), "[OK]");
        assert_eq!(status_label("blocked"), "[BLOCKED]");
        assert_eq!(status_label("failed"), "[FAILED]");
    }

    #[test]
    fn local_control_plane_autostart_is_limited_to_default_loopback() {
        assert!(is_local_default_control_url(
            Some("127.0.0.1"),
            Some(8787),
            false
        ));
        assert!(is_local_default_control_url(
            Some("localhost"),
            Some(8787),
            false
        ));
        assert!(!is_local_default_control_url(
            Some("example.test"),
            Some(8787),
            false
        ));
        assert!(!is_local_default_control_url(
            Some("127.0.0.1"),
            Some(9999),
            false
        ));
        assert!(!is_local_default_control_url(
            Some("127.0.0.1"),
            Some(8787),
            true
        ));
    }

    #[test]
    fn offline_runs_prioritize_active_work_over_historical_manifests() {
        let mut app = App::new();
        for (key, status) in [("old", "blocked"), ("active", "running")] {
            app.offline.insert(
                key.into(),
                forge_api::OfflineRunManifest {
                    run_id: forge_api::RunId(format!("run-{key}")),
                    project_id: ProjectId(format!("project-{key}")),
                    status: status.into(),
                    batch: 1,
                    pid: None,
                    updated_at: Some("2026-07-13T18:00:00Z".into()),
                    log: None,
                    source: None,
                    heartbeat_at: None,
                    terminal_reason: None,
                    final_decision: None,
                    exit_code: None,
                    finished_at: None,
                    invocation: serde_json::json!({"goal": key}),
                },
            );
        }
        assert_eq!(app.offline_runs()[0].status, "running");
    }

    #[test]
    fn approval_decision_requires_a_reason_before_emitting_side_effect() {
        let mut app = App::new();
        app.approvals.push(Approval {
            id: "approval-1".into(),
            run_id: forge_api::RunId("run-1".into()),
            action_type: "publish".into(),
            action_digest: "digest".into(),
            action_preview: serde_json::json!({}),
            risk: "high".into(),
            status: "pending".into(),
            requested_by: "planner".into(),
            decided_by: None,
            decision_reason: None,
            expires_at: "2026-01-01T00:00:00Z".into(),
            decided_at: None,
            consumed_at: None,
            created_at: "2026-01-01T00:00:00Z".into(),
        });
        assert!(app.handle_key(KeyCode::Char('y')).is_none());
        assert!(app.handle_key(KeyCode::Enter).is_none());
        for character in "reviewed".chars() {
            assert!(app.handle_key(KeyCode::Char(character)).is_none());
        }
        assert!(matches!(
            app.handle_key(KeyCode::Enter),
            Some(UiEvent::Action(Action::Decide { approved: true, .. }))
        ));
    }

    #[test]
    fn pi_prompt_stays_visible_with_operator_audit_and_emits_prompt() {
        let mut app = App::new();
        assert!(matches!(
            app.handle_key(KeyCode::Tab),
            Some(UiEvent::ToggleMode)
        ));
        for character in "hello Pi".chars() {
            assert!(app.handle_key(KeyCode::Char(character)).is_none());
        }
        assert!(
            matches!(app.handle_key(KeyCode::Enter), Some(UiEvent::Action(Action::PiPrompt(prompt))) if prompt == "hello Pi")
        );
        let backend = TestBackend::new(80, 36);
        let mut terminal = Terminal::new(backend).expect("terminal");
        terminal.draw(|frame| draw(frame, &app)).expect("draw");
        let rendered: String = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(rendered.contains("Pi prompt"));
        assert!(rendered.contains("Audit trail"));
        assert!(rendered.contains("PI PROMPT FOCUS"));
        assert!(rendered.contains("> hello Pi"));
    }

    #[test]
    fn session_prompt_accepts_q_and_uses_ctrl_q_to_quit() {
        let mut app = App::new();
        app.mode = Mode::Session;
        assert!(app.handle_key(KeyCode::Char('q')).is_none());
        assert_eq!(app.editor_text(), "q");
        assert!(matches!(
            app.handle_key_event(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)),
            Some(UiEvent::Quit)
        ));
    }

    #[test]
    fn session_editor_supports_alt_enter_without_submitting() {
        let mut app = App::new();
        app.mode = Mode::Session;
        for character in "first".chars() {
            app.handle_key(KeyCode::Char(character));
        }
        assert!(
            app.handle_key_event(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT))
                .is_none()
        );
        for character in "second".chars() {
            app.handle_key(KeyCode::Char(character));
        }
        assert!(matches!(
            app.handle_key(KeyCode::Enter),
            Some(UiEvent::Action(Action::PiPrompt(prompt))) if prompt == "first\nsecond"
        ));
    }

    #[test]
    fn pi_confirm_dialog_requires_an_explicit_answer() {
        let mut app = App::new();
        app.mode = Mode::Session;
        app.apply(Bulk::Pi(PiIncoming::ExtensionUiRequest {
            id: "dialog-1".into(),
            method: "confirm".into(),
            timeout: None,
            details: serde_json::json!({"title":"Deploy?"}),
        }));
        assert!(matches!(
            app.handle_key(KeyCode::Char('n')),
            Some(UiEvent::Action(Action::PiDialogConfirm {
                confirmed: false,
                ..
            }))
        ));
    }

    #[test]
    fn promote_to_goal_requires_goal_project_then_confirmation() {
        let mut app = App::new();
        app.mode = Mode::Session;
        app.transcript
            .push_back("> investigate the failing build".into());
        // Ctrl+G asks the event loop to fetch projects before opening the editor.
        assert!(matches!(
            app.handle_key_event(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::CONTROL)),
            Some(UiEvent::Action(Action::BeginPromote { context })) if context.contains("failing build")
        ));
        app.open_promote(
            "> investigate the failing build".into(),
            vec![PromoteProject {
                id: Some(ProjectId("project-busy".into())),
                name: "busy-project".into(),
                active: true,
            }],
        );
        for character in "Fix the build".chars() {
            assert!(app.handle_key(KeyCode::Char(character)).is_none());
        }
        // Goal → project selection stage.
        assert!(app.handle_key(KeyCode::Enter).is_none());
        // Select the busy project and confirm the review warns about it.
        assert!(app.handle_key(KeyCode::Char('j')).is_none());
        assert!(app.handle_key(KeyCode::Enter).is_none());
        assert!(app.message.contains("active run"));
        assert!(matches!(
            app.handle_key(KeyCode::Char('y')),
            Some(UiEvent::Action(Action::Promote { goal, project: Some(project), .. }))
                if goal == "Fix the build" && project.0 == "project-busy"
        ));
    }

    #[test]
    fn esc_opens_the_session_menu_and_steer_takes_editor_text() {
        let mut app = App::new();
        app.mode = Mode::Session;
        for character in "focus on tests".chars() {
            assert!(app.handle_key(KeyCode::Char(character)).is_none());
        }
        // Esc no longer aborts blindly; it opens the menu.
        assert!(app.handle_key(KeyCode::Esc).is_none());
        assert!(app.session_menu);
        assert!(matches!(
            app.handle_key(KeyCode::Char('s')),
            Some(UiEvent::Action(Action::PiSteer(message))) if message == "focus on tests"
        ));
        // Abort is still one keystroke away.
        assert!(app.handle_key(KeyCode::Esc).is_none());
        assert!(matches!(
            app.handle_key(KeyCode::Char('a')),
            Some(UiEvent::Action(Action::PiAbort))
        ));
    }

    #[test]
    fn session_picker_switches_and_forks_by_path() {
        let mut app = App::new();
        app.mode = Mode::Session;
        app.session_picker = Some(SessionPicker {
            entries: vec![
                SessionEntry {
                    path: "/tmp/newer.jsonl".into(),
                    name: "newer".into(),
                    modified: "1m ago".into(),
                    entries: "2 entries".into(),
                },
                SessionEntry {
                    path: "/tmp/older.jsonl".into(),
                    name: "older".into(),
                    modified: "9m ago".into(),
                    entries: "1 entry".into(),
                },
            ],
            selected: 0,
        });
        assert!(app.handle_key(KeyCode::Char('j')).is_none());
        assert!(matches!(
            app.handle_key(KeyCode::Enter),
            Some(UiEvent::Action(Action::PiSwitchSession(path))) if path == "/tmp/older.jsonl"
        ));
        app.session_picker = Some(SessionPicker {
            entries: vec![SessionEntry {
                path: "/tmp/newer.jsonl".into(),
                name: "newer".into(),
                modified: "1m ago".into(),
                entries: "2 entries".into(),
            }],
            selected: 0,
        });
        assert!(matches!(
            app.handle_key(KeyCode::Char('f')),
            Some(UiEvent::Action(Action::PiForkSession(path))) if path == "/tmp/newer.jsonl"
        ));
    }

    #[test]
    fn session_scanner_uses_the_supplied_directory_and_tolerates_bad_jsonl() {
        let directory = tempfile::tempdir().expect("temporary session directory");
        fs::write(
            directory.path().join("named.jsonl"),
            "{\"type\":\"session\",\"name\":\"Useful session\"}\nnot json\n",
        )
        .expect("session fixture");
        let entries = list_pi_sessions(Some(directory.path())).expect("scan sessions");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "Useful session");
        assert_eq!(entries[0].entries, "2 entries");
    }

    #[test]
    fn session_shortcuts_emit_explicit_pi_controls() {
        let mut app = App::new();
        app.mode = Mode::Session;
        assert!(matches!(
            app.handle_key_event(KeyEvent::new(KeyCode::Char('m'), KeyModifiers::CONTROL)),
            Some(UiEvent::Action(Action::PiControl {
                label: "Model cycle",
                ..
            }))
        ));
        assert!(matches!(
            app.handle_key_event(KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL)),
            Some(UiEvent::Action(Action::PiControl {
                label: "Thinking level cycle",
                ..
            }))
        ));
        assert!(matches!(
            app.handle_key_event(KeyEvent::new(KeyCode::Char('k'), KeyModifiers::CONTROL)),
            Some(UiEvent::Action(Action::PiControl {
                label: "Context compaction",
                ..
            }))
        ));
    }

    #[tokio::test]
    async fn real_event_loop_stays_responsive_under_stream_flood() {
        use std::time::Instant;

        let config = forge_api::ForgeConfig {
            home: std::env::temp_dir(),
            // A port that refuses immediately: the loop never blocks on it here.
            control_url: "http://127.0.0.1:1/".parse().expect("url"),
            control_token: "fixture-token".into(),
        };
        let api = ForgeApi::new(&config).expect("api");
        let (priority_tx, mut priority_rx) = mpsc::channel(64);
        let (_bulk_tx, mut bulk_rx) = mpsc::channel::<Bulk>(1);
        let bulk_for_loop = _bulk_tx.clone();
        let (pi_tx, mut pi_rx) = mpsc::channel(8);
        let (picker_tx, mut picker_rx) = mpsc::channel(1);
        let (stream_tx, mut stream_rx) = mpsc::channel(64);
        let (selection_tx, _selection_rx) = watch::channel(None);

        // Flood the real stream channel with 64-event batches as fast as the
        // loop will take them, for the whole test.
        let flood = tokio::spawn(async move {
            let mut sequence = 0_u64;
            loop {
                let batch: Vec<RunEvent> = (0..64)
                    .map(|offset| RunEvent {
                        id: format!("event-{sequence}-{offset}"),
                        run_id: forge_api::RunId("run-flood".into()),
                        sequence: sequence + offset,
                        event_type: "run.flood".into(),
                        actor: "flood-test".into(),
                        payload: serde_json::json!({}),
                        created_at: "2099-01-01T00:00:00Z".into(),
                    })
                    .collect();
                sequence += 64;
                if stream_tx.send(batch).await.is_err() {
                    return;
                }
            }
        });

        // After the flood is established, time how long a quit keypress takes
        // to travel through the real loop (input → handler → return).
        let (sent_tx, sent_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(500)).await;
            let sent = Instant::now();
            let _ = priority_tx
                .send(Priority::Key(KeyEvent::new(
                    KeyCode::Char('q'),
                    KeyModifiers::NONE,
                )))
                .await;
            let _ = sent_tx.send(sent);
        });

        let backend = TestBackend::new(80, 24);
        let mut terminal = Terminal::new(backend).expect("terminal");
        tokio::time::timeout(
            Duration::from_secs(10),
            event_loop(
                &mut terminal,
                &api,
                &selection_tx,
                SpawnOptions::default(),
                None,
                LoopChannels {
                    bulk_tx: bulk_for_loop,
                    pi_tx,
                    picker_tx,
                    stream: &mut stream_rx,
                    priority: &mut priority_rx,
                    bulk: &mut bulk_rx,
                    pi: &mut pi_rx,
                    picker: &mut picker_rx,
                },
            ),
        )
        .await
        .expect("event loop exits under flood")
        .expect("event loop result");
        let latency = sent_rx.await.expect("send instant").elapsed();
        flood.abort();
        assert!(
            latency < Duration::from_millis(50),
            "quit keypress took {latency:?} under stream flood"
        );
    }

    #[test]
    fn session_transcript_retains_thinking_and_tool_activity() {
        let mut app = App::new();
        app.apply(Bulk::Pi(PiIncoming::MessageUpdate {
            message: serde_json::json!({}),
            assistant_message_event: serde_json::json!({"type":"thinking_delta", "delta":"checking"}),
        }));
        app.apply(Bulk::Pi(PiIncoming::MessageUpdate {
            message: serde_json::json!({}),
            assistant_message_event: serde_json::json!({"type":"toolcall_start"}),
        }));
        assert!(
            app.transcript
                .iter()
                .any(|line| line.contains("[thinking] checking"))
        );
        assert!(
            app.transcript
                .iter()
                .any(|line| line.contains("[tool] toolcall_start"))
        );
    }

    #[test]
    fn session_text_deltas_are_coalesced_into_one_transcript_line() {
        let mut app = App::new();
        for delta in ["hello ", "world"] {
            app.apply(Bulk::Pi(PiIncoming::MessageUpdate {
                message: serde_json::json!({}),
                assistant_message_event: serde_json::json!({"type":"text_delta", "delta": delta}),
            }));
        }
        assert!(app.transcript.iter().any(|line| line == "hello world"));
        assert_eq!(
            app.transcript
                .iter()
                .filter(|line| *line == "hello world")
                .count(),
            1
        );
    }

    #[test]
    fn fire_and_forget_extension_requests_do_not_open_a_dialog() {
        let mut app = App::new();
        app.apply(Bulk::Pi(PiIncoming::ExtensionUiRequest {
            id: "notify-1".into(),
            method: "notify".into(),
            timeout: None,
            details: serde_json::json!({"message":"saved"}),
        }));
        assert!(app.pi_dialog.is_none());
        assert!(app.message.contains("saved"));
    }

    #[test]
    fn timed_dialog_is_dismissed_locally_without_a_second_pi_response() {
        let mut app = App::new();
        app.apply(Bulk::Pi(PiIncoming::ExtensionUiRequest {
            id: "dialog-1".into(),
            method: "confirm".into(),
            timeout: Some(1),
            details: serde_json::json!({"title":"Deploy?"}),
        }));
        app.expire_dialog_if_due(Instant::now() + Duration::from_millis(2));
        assert!(app.pi_dialog.is_none());
        assert!(app.message.contains("timed out"));
    }

    #[tokio::test]
    async fn priority_input_wins_under_bulk_flood() {
        use std::time::Instant;

        let (priority_tx, mut priority_rx) = mpsc::channel::<Priority>(8);
        let (bulk_tx, mut bulk_rx) = mpsc::channel::<Bulk>(1);
        for _ in 0..1_000 {
            let _ = bulk_tx.try_send(Bulk::Error("flood".into()));
        }
        priority_tx
            .send(Priority::Key(KeyEvent::new(
                KeyCode::Char('q'),
                KeyModifiers::NONE,
            )))
            .await
            .expect("priority send");
        let started = Instant::now();
        tokio::select! { biased;
            Some(Priority::Key(KeyEvent { code: KeyCode::Char('q'), .. })) = priority_rx.recv() => {},
            Some(_) = bulk_rx.recv() => panic!("bulk traffic starved input"),
            _ = tokio::time::sleep(Duration::from_millis(50)) => panic!("input latency exceeded 50ms"),
            else => panic!("queues closed"),
        }
        assert!(started.elapsed() < Duration::from_millis(50));
    }
}
