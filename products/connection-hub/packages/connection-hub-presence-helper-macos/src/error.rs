use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    InvalidRequest,
    RequestTooLarge,
    UnsupportedProtocol,
    UnsupportedCommand,
    SessionNotFound,
    SessionBusy,
    SessionReauthorizationRequired,
    UserPresenceCancelled,
    UserPresenceUnavailable,
    HelperSigningInvalid,
    OAuthProtocolUnavailable,
    OAuthAuthorizationFailed,
    OAuthRefreshFailed,
    OAuthRevocationFailed,
    OperationNotSupported,
    OperationApprovalRequired,
    OperationDenied,
    OperationFailed,
    ResponseTooLarge,
    InternalFailure,
}

impl ErrorCode {
    pub fn fixed_message(self) -> &'static str {
        match self {
            Self::InvalidRequest => "The helper request is invalid.",
            Self::RequestTooLarge => "The helper request exceeded the fixed size limit.",
            Self::UnsupportedProtocol => "The helper protocol version is unsupported.",
            Self::UnsupportedCommand => "The helper command is unsupported.",
            Self::SessionNotFound => "The protected session does not exist.",
            Self::SessionBusy => "The protected session is busy.",
            Self::SessionReauthorizationRequired => {
                "The protected session requires browser authorization again."
            }
            Self::UserPresenceCancelled => "The user cancelled system authentication.",
            Self::UserPresenceUnavailable => "System user-presence authentication is unavailable.",
            Self::HelperSigningInvalid => {
                "The installed helper does not have the required signing identity."
            }
            Self::OAuthProtocolUnavailable => {
                "The target OAuth protocol is not registered in this helper build."
            }
            Self::OAuthAuthorizationFailed => {
                "The protected OAuth session could not be authorized."
            }
            Self::OAuthRefreshFailed => "The protected OAuth session could not be refreshed.",
            Self::OAuthRevocationFailed => "The protected OAuth session could not be revoked.",
            Self::OperationNotSupported => {
                "The requested operation is not registered in this helper build."
            }
            Self::OperationApprovalRequired => {
                "Browser approval is required for this exact protected operation."
            }
            Self::OperationDenied => "The protected operation was denied.",
            Self::OperationFailed => "The protected operation could not be completed.",
            Self::ResponseTooLarge => {
                "The protected operation response exceeded the fixed size limit."
            }
            Self::InternalFailure => "The helper could not complete the request.",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CoreError {
    pub code: ErrorCode,
    pub retryable: bool,
}

impl CoreError {
    pub const fn new(code: ErrorCode) -> Self {
        Self {
            code,
            retryable: false,
        }
    }

    pub const fn retryable(code: ErrorCode) -> Self {
        Self {
            code,
            retryable: true,
        }
    }
}

impl std::fmt::Display for CoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.code.fixed_message())
    }
}

impl std::error::Error for CoreError {}

pub type CoreResult<T> = Result<T, CoreError>;
