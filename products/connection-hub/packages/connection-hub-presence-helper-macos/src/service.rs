use std::sync::Arc;

use chrono::{DateTime, SecondsFormat, Utc};
use uuid::Uuid;

use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::lock::SessionLock;
use crate::protocol::{
    HelperCommand, HelperRequest, HelperResponse, HelperStatus, SafeOperationEvidence, SafeResult,
    SafeSessionSummary, PROTOCOL_VERSION,
};
use crate::session::{
    AuthorizedSession, OAuthTokenSet, ProtectedOAuthSession, SessionDescriptor, TargetRecord,
};
use crate::validation::{parse_session_id, ValidatedInvocation, ValidatedTarget};

pub trait SessionStore: Send + Sync {
    fn create(&self, value: &AuthorizedSession) -> CoreResult<()>;
    fn list_session_ids(&self) -> CoreResult<Vec<Uuid>>;
    fn describe(&self, session_id: Uuid) -> CoreResult<SessionDescriptor>;
    fn read(&self, session_id: Uuid, prompt: &str) -> CoreResult<ProtectedOAuthSession>;
    fn replace(
        &self,
        session_id: Uuid,
        expected_generation: u64,
        value: &ProtectedOAuthSession,
    ) -> CoreResult<()>;
    fn require_reauthorization(&self, session_id: Uuid) -> CoreResult<()>;
    fn remove(&self, session_id: Uuid, prompt: &str) -> CoreResult<bool>;
}

pub trait OAuthAuthorizer: Send + Sync {
    fn authorize(&self, target: &ValidatedTarget) -> CoreResult<AuthorizedSession>;
}

pub trait OAuthRefresher: Send + Sync {
    fn refresh(&self, session: &ProtectedOAuthSession) -> CoreResult<OAuthTokenSet>;
}

pub trait OAuthRevoker: Send + Sync {
    fn revoke(&self, session: &ProtectedOAuthSession) -> CoreResult<()>;
}

pub trait OperationRegistry: Send + Sync {
    fn operation_ids(&self) -> Vec<&'static str>;
    fn prompt(&self, target: &TargetRecord, invocation: &ValidatedInvocation)
        -> CoreResult<String>;
    fn execute(
        &self,
        session: &ProtectedOAuthSession,
        invocation: &ValidatedInvocation,
    ) -> CoreResult<SafeOperationEvidence>;
}

pub trait Clock: Send + Sync {
    fn now_unix(&self) -> i64;
}

#[derive(Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_unix(&self) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |value| value.as_secs() as i64)
    }
}

pub struct PresenceService {
    helper_version: String,
    store: Arc<dyn SessionStore>,
    authorizer: Arc<dyn OAuthAuthorizer>,
    refresher: Arc<dyn OAuthRefresher>,
    revoker: Arc<dyn OAuthRevoker>,
    operations: Arc<dyn OperationRegistry>,
    locks: Arc<dyn SessionLock>,
    clock: Arc<dyn Clock>,
    refresh_leeway_seconds: i64,
}

impl PresenceService {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        helper_version: impl Into<String>,
        store: Arc<dyn SessionStore>,
        authorizer: Arc<dyn OAuthAuthorizer>,
        refresher: Arc<dyn OAuthRefresher>,
        revoker: Arc<dyn OAuthRevoker>,
        operations: Arc<dyn OperationRegistry>,
        locks: Arc<dyn SessionLock>,
        clock: Arc<dyn Clock>,
    ) -> Self {
        Self {
            helper_version: helper_version.into(),
            store,
            authorizer,
            refresher,
            revoker,
            operations,
            locks,
            clock,
            refresh_leeway_seconds: 60,
        }
    }

    pub fn handle(&self, request: HelperRequest) -> HelperResponse {
        let request_id = match request.validated_request_id() {
            Ok(value) if request.protocol_version == PROTOCOL_VERSION => value,
            Ok(_) => return HelperResponse::failure(None, ErrorCode::UnsupportedProtocol, false),
            Err(error) => return HelperResponse::failure(None, error.code, error.retryable),
        };
        match self.dispatch(request) {
            Ok(result) => HelperResponse::success(request_id, result),
            Err(error) => HelperResponse::failure(Some(request_id), error.code, error.retryable),
        }
    }

    fn dispatch(&self, request: HelperRequest) -> CoreResult<SafeResult> {
        match request.command {
            HelperCommand::Status => {
                let mut capabilities = vec![
                    "oauth-session-custody".to_owned(),
                    "request-bound-execution".to_owned(),
                    "user-presence-keychain".to_owned(),
                ];
                capabilities.extend(
                    self.operations
                        .operation_ids()
                        .into_iter()
                        .map(|operation| format!("operation:{operation}")),
                );
                capabilities.sort();
                Ok(SafeResult::Status {
                    status: HelperStatus {
                        helper_version: self.helper_version.clone(),
                        protocol_version: PROTOCOL_VERSION,
                        capabilities,
                    },
                })
            }
            HelperCommand::AuthorizeSession => {
                let target = ValidatedTarget::try_from(
                    request
                        .target
                        .ok_or_else(|| CoreError::new(ErrorCode::InvalidRequest))?,
                )?;
                let authorized = self
                    .authorizer
                    .authorize(&target)
                    .map_err(|error| preserve_or(error, ErrorCode::OAuthAuthorizationFailed))?;
                validate_authorized_session(&authorized, &target)?;
                self.store
                    .create(&authorized)
                    .map_err(|error| preserve_or(error, ErrorCode::OAuthAuthorizationFailed))?;
                Ok(SafeResult::Session {
                    session: safe_summary(&authorized.session)?,
                })
            }
            HelperCommand::ExecuteOperation => {
                let session_id = parse_session_id(request.session_id.as_deref())?;
                let invocation = ValidatedInvocation::try_from(
                    request
                        .operation
                        .ok_or_else(|| CoreError::new(ErrorCode::InvalidRequest))?,
                )?;
                Ok(SafeResult::Operation {
                    operation: self.execute(session_id, &invocation)?,
                })
            }
            HelperCommand::RemoveSession => {
                let session_id = parse_session_id(request.session_id.as_deref())?;
                self.revoke_and_remove(session_id)?;
                Ok(SafeResult::Removed)
            }
            HelperCommand::PurgeAllSessions => {
                let session_ids = self
                    .store
                    .list_session_ids()
                    .map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
                for session_id in session_ids {
                    self.revoke_and_remove(session_id)?;
                }
                Ok(SafeResult::Removed)
            }
        }
    }

    fn execute(
        &self,
        session_id: Uuid,
        invocation: &ValidatedInvocation,
    ) -> CoreResult<SafeOperationEvidence> {
        let initial_descriptor = self.describe(session_id)?;
        let prompt = self
            .operations
            .prompt(&initial_descriptor.target, invocation)
            .map_err(|error| preserve_or(error, ErrorCode::OperationNotSupported))?;
        let mut result = None;
        let mut operation = || {
            let descriptor = self.describe(session_id)?;
            if descriptor != initial_descriptor || descriptor.reauthorization_required {
                return Err(CoreError::new(ErrorCode::SessionReauthorizationRequired));
            }
            let mut session = self.store.read(session_id, &prompt)?;
            validate_stored_session(&session, &descriptor)?;
            if session.tokens.access_expires_at
                <= self.clock.now_unix() + self.refresh_leeway_seconds
            {
                let replacement = match self.refresher.refresh(&session) {
                    Ok(tokens) => session.replacing(tokens),
                    Err(_) => {
                        let _ = self.store.require_reauthorization(session_id);
                        return Err(CoreError::new(ErrorCode::SessionReauthorizationRequired));
                    }
                };
                if self
                    .store
                    .replace(session_id, session.generation, &replacement)
                    .is_err()
                {
                    let _ = self.store.require_reauthorization(session_id);
                    return Err(CoreError::new(ErrorCode::SessionReauthorizationRequired));
                }
                session = replacement;
            }
            match self.operations.execute(&session, invocation) {
                Ok(evidence) => {
                    result = Some(evidence);
                    Ok(())
                }
                Err(error) => {
                    if error.code == ErrorCode::SessionReauthorizationRequired {
                        let _ = self.store.require_reauthorization(session_id);
                    }
                    Err(error)
                }
            }
        };
        self.locks.with_lock(session_id, &mut operation)?;
        result.ok_or_else(|| CoreError::new(ErrorCode::OperationFailed))
    }

    fn revoke_and_remove(&self, session_id: Uuid) -> CoreResult<()> {
        let mut operation = || {
            let descriptor = self.describe(session_id)?;
            let prompt = format!(
                "Disconnect {}/{} at {}",
                descriptor.target.tenant,
                descriptor.target.project,
                descriptor.target.normalized_origin
            );
            let session = self.store.read(session_id, &prompt)?;
            validate_stored_session(&session, &descriptor)?;
            self.revoker
                .revoke(&session)
                .map_err(|error| preserve_presence_or(error, ErrorCode::OAuthRevocationFailed))?;
            match self.store.remove(session_id, &prompt) {
                Ok(true) => Ok(()),
                Ok(false) => Err(CoreError::new(ErrorCode::SessionNotFound)),
                Err(error) => Err(preserve_presence_or(error, ErrorCode::InternalFailure)),
            }
        };
        self.locks.with_lock(session_id, &mut operation)
    }

    fn describe(&self, session_id: Uuid) -> CoreResult<SessionDescriptor> {
        let descriptor = self
            .store
            .describe(session_id)
            .map_err(|error| preserve_or(error, ErrorCode::SessionNotFound))?;
        if descriptor.session_id == session_id {
            Ok(descriptor)
        } else {
            Err(CoreError::new(ErrorCode::SessionNotFound))
        }
    }
}

fn validate_authorized_session(
    value: &AuthorizedSession,
    expected: &ValidatedTarget,
) -> CoreResult<()> {
    let target = TargetRecord::from(expected);
    if value.descriptor.session_id == value.session.session_id
        && value.descriptor.target == target
        && value.session.target == target
        && value.descriptor.binding_digest == value.session.binding_digest
    {
        Ok(())
    } else {
        Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed))
    }
}

fn validate_stored_session(
    session: &ProtectedOAuthSession,
    descriptor: &SessionDescriptor,
) -> CoreResult<()> {
    if session.session_id == descriptor.session_id
        && session.target == descriptor.target
        && session.binding_digest == descriptor.binding_digest
        && session.schema_version == 1
        && session.generation >= 1
    {
        Ok(())
    } else {
        Err(CoreError::new(ErrorCode::SessionReauthorizationRequired))
    }
}

fn safe_summary(session: &ProtectedOAuthSession) -> CoreResult<SafeSessionSummary> {
    let access_expires_at = DateTime::<Utc>::from_timestamp(session.tokens.access_expires_at, 0)
        .ok_or_else(|| CoreError::new(ErrorCode::InternalFailure))?
        .to_rfc3339_opts(SecondsFormat::Secs, true);
    Ok(SafeSessionSummary {
        session_id: session.session_id.hyphenated().to_string(),
        normalized_origin: session.target.normalized_origin.clone(),
        tenant: session.target.tenant.clone(),
        project: session.target.project.clone(),
        access_expires_at,
    })
}

fn preserve_or(error: CoreError, fallback: ErrorCode) -> CoreError {
    match error.code {
        ErrorCode::InvalidRequest
        | ErrorCode::SessionNotFound
        | ErrorCode::SessionBusy
        | ErrorCode::SessionReauthorizationRequired
        | ErrorCode::UserPresenceCancelled
        | ErrorCode::UserPresenceUnavailable
        | ErrorCode::HelperSigningInvalid
        | ErrorCode::OAuthProtocolUnavailable
        | ErrorCode::OperationNotSupported
        | ErrorCode::OperationApprovalRequired
        | ErrorCode::OperationDenied
        | ErrorCode::ResponseTooLarge => error,
        _ => CoreError {
            code: fallback,
            retryable: error.retryable,
        },
    }
}

fn preserve_presence_or(error: CoreError, fallback: ErrorCode) -> CoreError {
    match error.code {
        ErrorCode::UserPresenceCancelled
        | ErrorCode::UserPresenceUnavailable
        | ErrorCode::HelperSigningInvalid => error,
        _ => CoreError::new(fallback),
    }
}
