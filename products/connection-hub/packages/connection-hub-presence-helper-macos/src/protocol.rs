use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::error::{CoreError, CoreResult, ErrorCode};

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_REQUEST_BYTES: usize = 64 * 1024;
pub const MAX_RESPONSE_BYTES: usize = 256 * 1024;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum HelperCommand {
    Status,
    AuthorizeSession,
    ExecuteOperation,
    RemoveSession,
    PurgeAllSessions,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TargetCoordinates {
    pub origin: String,
    pub tenant: String,
    pub project: String,
    pub caller_profile: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub oauth_client_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OperationInvocation {
    pub operation_id: String,
    pub invocation_id: String,
    pub arguments: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HelperRequest {
    pub protocol_version: u32,
    pub request_id: String,
    pub command: HelperCommand,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<TargetCoordinates>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub operation: Option<OperationInvocation>,
}

impl HelperRequest {
    pub fn decode(input: &[u8]) -> Result<Self, ProtocolError> {
        if input.len() > MAX_REQUEST_BYTES {
            return Err(ProtocolError::new(ErrorCode::RequestTooLarge, None));
        }
        let root: Value = serde_json::from_slice(input)
            .map_err(|_| ProtocolError::new(ErrorCode::InvalidRequest, None))?;
        let object = root
            .as_object()
            .ok_or_else(|| ProtocolError::new(ErrorCode::InvalidRequest, None))?;
        let request_id = object
            .get("request_id")
            .and_then(Value::as_str)
            .and_then(|value| Uuid::parse_str(value).ok())
            .map(|value| value.hyphenated().to_string());
        let Some(request_id) = request_id else {
            return Err(ProtocolError::new(ErrorCode::InvalidRequest, None));
        };
        let allowed: BTreeSet<&str> = [
            "protocol_version",
            "request_id",
            "command",
            "session_id",
            "target",
            "operation",
        ]
        .into_iter()
        .collect();
        if !["protocol_version", "request_id", "command"]
            .iter()
            .all(|key| object.contains_key(*key))
            || object.keys().any(|key| !allowed.contains(key.as_str()))
        {
            return Err(ProtocolError::new(
                ErrorCode::InvalidRequest,
                Some(request_id),
            ));
        }
        if object.get("protocol_version").and_then(Value::as_u64)
            != Some(u64::from(PROTOCOL_VERSION))
        {
            return Err(ProtocolError::new(
                ErrorCode::UnsupportedProtocol,
                Some(request_id),
            ));
        }
        let command = object.get("command").and_then(Value::as_str);
        if !matches!(
            command,
            Some(
                "status"
                    | "authorize_session"
                    | "execute_operation"
                    | "remove_session"
                    | "purge_all_sessions"
            )
        ) {
            return Err(ProtocolError::new(
                ErrorCode::UnsupportedCommand,
                Some(request_id),
            ));
        }
        let request: Self = serde_json::from_value(root)
            .map_err(|_| ProtocolError::new(ErrorCode::InvalidRequest, Some(request_id.clone())))?;
        request
            .validate_shape()
            .map_err(|error| ProtocolError::new(error.code, Some(request_id.clone())))?;
        Ok(request)
    }

    pub fn validated_request_id(&self) -> CoreResult<String> {
        Uuid::parse_str(&self.request_id)
            .map(|value| value.hyphenated().to_string())
            .map_err(|_| CoreError::new(ErrorCode::InvalidRequest))
    }

    fn validate_shape(&self) -> CoreResult<()> {
        let valid = match self.command {
            HelperCommand::Status | HelperCommand::PurgeAllSessions => {
                self.session_id.is_none() && self.target.is_none() && self.operation.is_none()
            }
            HelperCommand::AuthorizeSession => {
                self.session_id.is_none() && self.target.is_some() && self.operation.is_none()
            }
            HelperCommand::ExecuteOperation => {
                self.session_id.is_some() && self.target.is_none() && self.operation.is_some()
            }
            HelperCommand::RemoveSession => {
                self.session_id.is_some() && self.target.is_none() && self.operation.is_none()
            }
        };
        if valid {
            Ok(())
        } else {
            Err(CoreError::new(ErrorCode::InvalidRequest))
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProtocolError {
    pub code: ErrorCode,
    pub request_id: Option<String>,
}

impl ProtocolError {
    fn new(code: ErrorCode, request_id: Option<String>) -> Self {
        Self { code, request_id }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct SafeError {
    pub code: ErrorCode,
    pub message: String,
    pub retryable: bool,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct HelperStatus {
    pub helper_version: String,
    pub protocol_version: u32,
    pub capabilities: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct SafeSessionSummary {
    pub session_id: String,
    pub normalized_origin: String,
    pub tenant: String,
    pub project: String,
    pub access_expires_at: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct SafeOperationEvidence {
    pub operation_id: String,
    pub invocation_id: String,
    pub outcome: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub application_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub generation: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub card_revision: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub catalog_revision: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub policy_revision: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum SafeResult {
    Status { status: HelperStatus },
    Session { session: SafeSessionSummary },
    Operation { operation: SafeOperationEvidence },
    Removed,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct HelperResponse {
    pub protocol_version: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<SafeResult>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<SafeError>,
}

impl HelperResponse {
    pub fn success(request_id: String, result: SafeResult) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            request_id: Some(request_id),
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    pub fn failure(request_id: Option<String>, code: ErrorCode, retryable: bool) -> Self {
        Self {
            protocol_version: PROTOCOL_VERSION,
            request_id,
            ok: false,
            result: None,
            error: Some(SafeError {
                code,
                message: code.fixed_message().to_owned(),
                retryable,
            }),
        }
    }
}
