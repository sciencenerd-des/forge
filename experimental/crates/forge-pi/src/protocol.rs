use std::time::Duration;

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum PiCommand {
    Prompt {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
        message: String,
        #[serde(rename = "streamingBehavior", skip_serializing_if = "Option::is_none")]
        streaming_behavior: Option<String>,
    },
    Abort {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    Steer {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
        message: String,
    },
    FollowUp {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
        message: String,
    },
    NewSession {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    GetState {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    GetMessages {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    SetModel {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
        provider: String,
        #[serde(rename = "modelId")]
        model_id: String,
    },
    CycleModel {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    GetAvailableModels {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    SetThinkingLevel {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
        level: String,
    },
    CycleThinkingLevel {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    Compact {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    SwitchSession {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
        #[serde(rename = "sessionPath")]
        session_path: String,
    },
    Fork {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
        #[serde(rename = "entryId")]
        entry_id: String,
    },
    GetSessionStats {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    GetCommands {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<String>,
    },
    ExtensionUiResponse {
        id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        confirmed: Option<bool>,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        cancelled: Option<bool>,
    },
}

impl PiCommand {
    /// The RPC command budget is part of the protocol contract. Keep slow
    /// session-changing work from inheriting the short state-query timeout.
    pub fn timeout(&self) -> Duration {
        match self {
            Self::Compact { .. } | Self::SwitchSession { .. } | Self::Fork { .. } => {
                Duration::from_secs(120)
            }
            _ => Duration::from_secs(15),
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum PiIncoming {
    Response {
        #[serde(default)]
        id: Option<String>,
        command: String,
        success: bool,
        #[serde(default)]
        data: serde_json::Value,
        #[serde(default)]
        error: Option<String>,
    },
    MessageUpdate {
        message: serde_json::Value,
        #[serde(rename = "assistantMessageEvent")]
        assistant_message_event: serde_json::Value,
    },
    ExtensionUiRequest {
        id: String,
        method: String,
        #[serde(default)]
        timeout: Option<u64>,
        #[serde(flatten)]
        details: serde_json::Value,
    },
    AgentEnd {
        #[serde(default)]
        messages: Vec<serde_json::Value>,
    },
    ToolExecutionStart {
        #[serde(rename = "toolCallId")]
        tool_call_id: String,
        #[serde(rename = "toolName")]
        tool_name: String,
        #[serde(default)]
        args: serde_json::Value,
    },
    ToolExecutionUpdate {
        #[serde(rename = "toolCallId")]
        tool_call_id: String,
        #[serde(rename = "toolName")]
        tool_name: String,
        #[serde(default)]
        args: serde_json::Value,
        #[serde(rename = "partialResult", default)]
        partial_result: serde_json::Value,
    },
    ToolExecutionEnd {
        #[serde(rename = "toolCallId")]
        tool_call_id: String,
        #[serde(rename = "toolName")]
        tool_name: String,
        #[serde(default)]
        result: serde_json::Value,
        #[serde(rename = "isError", default)]
        is_error: bool,
    },
    QueueUpdate {
        #[serde(flatten)]
        details: serde_json::Value,
    },
    CompactionStart,
    CompactionEnd {
        #[serde(flatten)]
        details: serde_json::Value,
    },
    ExtensionError {
        #[serde(flatten)]
        details: serde_json::Value,
    },
    #[serde(other)]
    Unknown,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_pi_camel_case_fields() {
        let command = PiCommand::SetModel {
            id: Some("req-1".into()),
            provider: "openai".into(),
            model_id: "gpt-test".into(),
        };
        assert_eq!(
            serde_json::to_value(command).expect("serialize"),
            serde_json::json!({"type":"set_model","id":"req-1","provider":"openai","modelId":"gpt-test"})
        );
    }

    #[test]
    fn assigns_longer_budgets_to_session_changing_commands() {
        assert_eq!(
            PiCommand::Compact { id: None }.timeout(),
            Duration::from_secs(120)
        );
        assert_eq!(
            PiCommand::GetSessionStats { id: None }.timeout(),
            Duration::from_secs(15)
        );
    }

    #[test]
    fn decodes_real_pi_agent_and_tool_events() {
        let end: PiIncoming = serde_json::from_value(serde_json::json!({
            "type": "agent_end", "messages": []
        }))
        .expect("agent end");
        assert!(matches!(end, PiIncoming::AgentEnd { .. }));
        let tool: PiIncoming = serde_json::from_value(serde_json::json!({
            "type": "tool_execution_end", "toolCallId": "call-1",
            "toolName": "read", "result": {"content": []}, "isError": false
        }))
        .expect("tool end");
        assert!(matches!(
            tool,
            PiIncoming::ToolExecutionEnd { tool_name, is_error: false, .. } if tool_name == "read"
        ));
    }
}
