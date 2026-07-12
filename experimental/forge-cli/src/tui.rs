use std::{collections::VecDeque, fs, time::Duration};

use anyhow::Result;
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
    widgets::{Block, Borders, Clear, List, ListItem, Paragraph},
};
use tokio::{
    sync::{mpsc, watch},
    time::Instant,
};
use tui_textarea::{Input as EditorInput, Key as EditorKey, TextArea};

const MAX_SCROLLBACK: usize = 10_000;

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
    session: Option<PiClient>,
    mode: Mode,
    selected: usize,
    offline_mode: bool,
    help: bool,
    input: Option<InputMode>,
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
    editor.set_block(Block::default().title(" Prompt ").borders(Borders::ALL));
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
    fn new() -> Self {
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
            session: None,
            mode: Mode::Operator,
            selected: 0,
            offline_mode: false,
            help: false,
            input: None,
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
                    self.open_session_picker();
                    return None;
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

    fn open_session_picker(&mut self) {
        let entries = list_pi_sessions();
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
            self.offline.len()
        } else {
            self.runs.len()
        }
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

pub async fn run(api: ForgeApi, config: ForgeConfig, session_options: SpawnOptions) -> Result<()> {
    install_terminal_panic_hook();
    let (priority_tx, mut priority_rx) = mpsc::channel(64);
    let (bulk_tx, mut bulk_rx) = mpsc::channel(1);
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
        LoopChannels {
            bulk_tx: bulk_tx.clone(),
            stream: &mut stream_rx,
            priority: &mut priority_rx,
            bulk: &mut bulk_rx,
        },
    )
    .await;
    ratatui::restore();
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
    stream: &'a mut mpsc::Receiver<Vec<RunEvent>>,
    priority: &'a mut mpsc::Receiver<Priority>,
    bulk: &'a mut mpsc::Receiver<Bulk>,
}

async fn event_loop<B: ratatui::backend::Backend>(
    terminal: &mut ratatui::Terminal<B>,
    api: &ForgeApi,
    selection: &watch::Sender<Option<forge_api::RunId>>,
    session_options: SpawnOptions,
    channels: LoopChannels<'_>,
) -> Result<()> {
    let LoopChannels {
        bulk_tx,
        stream,
        priority,
        bulk,
    } = channels;
    let mut app = App::new();
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
                                    spawn_pi_events(client.clone(), bulk_tx.clone());
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
                                spawn_pi_events(client.clone(), bulk_tx.clone());
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
                    None => { let _ = selection.send(app.selected_run_id()); }
                },
            },
            Some(update) = bulk.recv() => {
                let refresh_stats = matches!(&update, Bulk::Pi(PiIncoming::AgentEnd { .. }));
                app.apply(update);
                if refresh_stats && let Some(client) = app.session.clone() {
                    spawn_session_stats(client, bulk_tx.clone());
                }
                let _ = selection.send(app.selected_run_id());
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
        while selection.changed().await.is_ok() {
            let Some(run_id) = selection.borrow().clone() else {
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
                        if attempt > 5 {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(250 * u64::from(attempt))).await;
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

fn spawn_pi_events(client: PiClient, bulk: mpsc::Sender<Bulk>) {
    tokio::spawn(async move {
        let mut events = client.events();
        while let Ok(event) = events.recv().await {
            let _ = bulk.try_send(Bulk::Pi(event));
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
            Ok(result) => format!("Run {} started", short(&result.run_id.0)),
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
        Action::BeginPromote { .. } | Action::PiForkSession(_) => {
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

/// Pi stores sessions under `~/.pi/agent/sessions/<sanitized cwd>/` where the
/// directory name is `-` + cwd with `/` replaced by `-` + `--`.
fn pi_session_dir() -> Option<std::path::PathBuf> {
    let home = std::env::var_os("HOME")?;
    let cwd = std::env::current_dir().ok()?;
    let sanitized = format!("-{}--", cwd.display().to_string().replace('/', "-"));
    Some(
        std::path::PathBuf::from(home)
            .join(".pi/agent/sessions")
            .join(sanitized),
    )
}

fn list_pi_sessions() -> Vec<SessionEntry> {
    let Some(directory) = pi_session_dir() else {
        return Vec::new();
    };
    let Ok(reader) = fs::read_dir(&directory) else {
        return Vec::new();
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
            let name = path
                .file_stem()
                .and_then(|stem| stem.to_str())
                .unwrap_or("session")
                .to_owned();
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
            }
        })
        .collect();
    entries.sort_by(|a, b| b.name.cmp(&a.name)); // timestamped names: newest first
    entries
}

fn short_path(path: &str) -> &str {
    std::path::Path::new(path)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(path)
}

fn draw(frame: &mut Frame, app: &App) {
    if app.mode == Mode::Session {
        draw_session(frame, app);
        return;
    }
    let [header, body, footer] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(10),
        Constraint::Length(3),
    ])
    .areas(frame.area());
    let [runs, detail] =
        Layout::horizontal([Constraint::Percentage(35), Constraint::Percentage(65)]).areas(body);
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
            Span::raw("  operator"),
        ]))
        .block(Block::default().borders(Borders::BOTTOM)),
        header,
    );
    let items: Vec<ListItem> = if app.offline_mode {
        app.offline
            .values()
            .map(|run| {
                ListItem::new(format!(
                    "{}  {}  batch {}",
                    run.status,
                    short(&run.run_id.0),
                    run.batch
                ))
            })
            .collect()
    } else {
        app.runs
            .iter()
            .map(|run| {
                ListItem::new(format!(
                    "{}  {}  {}",
                    run.status,
                    short(&run.id.0),
                    run.current_node
                ))
            })
            .collect()
    };
    let items = if items.is_empty() {
        vec![ListItem::new("No runs.")]
    } else {
        items
    };
    frame.render_widget(
        List::new(items)
            .block(Block::default().title(" Runs ").borders(Borders::ALL))
            .highlight_style(Style::default().bg(Color::DarkGray))
            .highlight_symbol("› "),
        runs,
    );
    let mut lines = vec![Line::from(app.message.as_str()), Line::from("")];
    lines.extend(
        app.events
            .iter()
            .rev()
            .take(12)
            .rev()
            .map(|line| Line::styled(line, Style::default().fg(THEME.dim))),
    );
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
        Paragraph::new(lines).block(Block::default().title(" Detail ").borders(Borders::ALL)),
        detail,
    );
    let footer_text = if app.help {
        " j/k select  q quit  ? close help  |  s start  x stop  a approvals  y/n decide "
    } else {
        " ? help  q quit  j/k select  s start  x stop  a approvals "
    };
    frame.render_widget(
        Paragraph::new(footer_text).block(Block::default().borders(Borders::TOP)),
        footer,
    );
}

fn draw_session(frame: &mut Frame, app: &App) {
    let [header, transcript, editor, footer] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(8),
        Constraint::Length(3),
        Constraint::Length(2),
    ])
    .areas(frame.area());
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                " FORGE ",
                Style::default()
                    .fg(Color::Black)
                    .bg(THEME.amber)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                " PI SESSION ",
                Style::default()
                    .fg(Color::Black)
                    .bg(THEME.verdigris)
                    .add_modifier(Modifier::BOLD),
            ),
        ]))
        .block(Block::default().borders(Borders::BOTTOM)),
        header,
    );
    let lines: Vec<Line> = if app.transcript.is_empty() {
        vec![Line::styled(
            "Pi session loading…",
            Style::default().fg(THEME.dim),
        )]
    } else {
        app.transcript
            .iter()
            .rev()
            .take(500)
            .rev()
            .map(|line| Line::from(line.as_str()))
            .collect()
    };
    frame.render_widget(
        Paragraph::new(lines).block(Block::default().title(" Transcript ").borders(Borders::ALL)),
        transcript,
    );
    frame.render_widget(&app.editor, editor);
    frame.render_widget(
        Paragraph::new(format!(
            " {}  |  tab operator  enter send  esc menu  ctrl+r sessions  ctrl+g promote  ctrl+q quit ",
            app.session_footer
        ))
        .block(Block::default().borders(Borders::TOP)),
        footer,
    );
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
                    ListItem::new(format!("{marker}{}  ({})", entry.name, entry.modified))
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
        assert!(rendered.contains("No runs."));
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
    fn session_mode_renders_transcript_and_emits_prompt() {
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
        assert!(rendered.contains("PI SESSION"));
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
                },
                SessionEntry {
                    path: "/tmp/older.jsonl".into(),
                    name: "older".into(),
                    modified: "9m ago".into(),
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
            }],
            selected: 0,
        });
        assert!(matches!(
            app.handle_key(KeyCode::Char('f')),
            Some(UiEvent::Action(Action::PiForkSession(path))) if path == "/tmp/newer.jsonl"
        ));
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
                LoopChannels {
                    bulk_tx: bulk_for_loop,
                    stream: &mut stream_rx,
                    priority: &mut priority_rx,
                    bulk: &mut bulk_rx,
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
