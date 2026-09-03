use std::collections::BTreeMap;
use std::sync::Arc;
use std::thread;

use uuid::Uuid;

use crate::error::{CoreError, ErrorCode};
use crate::protocol::{
    HelperCommand, HelperRequest, OperationInvocation, SafeResult, TargetCoordinates,
    PROTOCOL_VERSION,
};
use crate::service::PresenceService;

use super::fixtures::{
    authorized_session, response_is_safe, rotated_tokens, FakeAuthorizer, FakeOperations,
    FakeRefresher, FakeRevoker, FakeStore, FixedClock, MutexSessionLock, ACCESS_MARKER, NOW,
    ROTATED_ACCESS_MARKER,
};

fn operation() -> OperationInvocation {
    OperationInvocation {
        operation_id: "fixture.inspect".to_owned(),
        invocation_id: Uuid::new_v4().hyphenated().to_string(),
        arguments: BTreeMap::from([("application_id".to_owned(), "workspace@1-0".to_owned())]),
    }
}

fn execute_request(session_id: Uuid) -> HelperRequest {
    HelperRequest {
        protocol_version: PROTOCOL_VERSION,
        request_id: Uuid::new_v4().hyphenated().to_string(),
        command: HelperCommand::ExecuteOperation,
        session_id: Some(session_id.hyphenated().to_string()),
        target: None,
        operation: Some(operation()),
    }
}

fn remove_request(session_id: Uuid) -> HelperRequest {
    HelperRequest {
        protocol_version: PROTOCOL_VERSION,
        request_id: Uuid::new_v4().hyphenated().to_string(),
        command: HelperCommand::RemoveSession,
        session_id: Some(session_id.hyphenated().to_string()),
        target: None,
        operation: None,
    }
}

fn service(
    store: Arc<FakeStore>,
    refresher: Arc<FakeRefresher>,
    revoker: Arc<FakeRevoker>,
    operations: Arc<FakeOperations>,
) -> PresenceService {
    PresenceService::new(
        "rust-test",
        store,
        Arc::new(FakeAuthorizer::new(authorized_session(false))),
        refresher,
        revoker,
        operations,
        Arc::new(MutexSessionLock::default()),
        Arc::new(FixedClock::new(NOW)),
    )
}

fn error_code(response: &crate::protocol::HelperResponse) -> Option<ErrorCode> {
    response.error.as_ref().map(|value| value.code)
}

#[test]
fn authorization_stores_credentials_but_returns_only_safe_summary() {
    let store = Arc::new(FakeStore::empty());
    let authorized = authorized_session(false);
    let service = PresenceService::new(
        "rust-test",
        store.clone(),
        Arc::new(FakeAuthorizer::new(authorized)),
        Arc::new(FakeRefresher::new(rotated_tokens())),
        Arc::new(FakeRevoker::new()),
        Arc::new(FakeOperations::new()),
        Arc::new(MutexSessionLock::default()),
        Arc::new(FixedClock::new(NOW)),
    );
    let response = service.handle(HelperRequest {
        protocol_version: PROTOCOL_VERSION,
        request_id: Uuid::new_v4().to_string(),
        command: HelperCommand::AuthorizeSession,
        session_id: None,
        target: Some(TargetCoordinates {
            origin: "https://target.example".to_owned(),
            tenant: "tenant-a".to_owned(),
            project: "project-a".to_owned(),
            caller_profile: "human-admin".to_owned(),
            oauth_client_id: None,
        }),
        operation: None,
    });
    assert!(response.ok);
    assert!(matches!(response.result, Some(SafeResult::Session { .. })));
    assert!(store.has_session());
    assert!(response_is_safe(&response));
}

#[test]
fn invalid_operation_is_rejected_before_protected_session_read() {
    let authorized = authorized_session(false);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    let subject = service(
        store.clone(),
        Arc::new(FakeRefresher::new(rotated_tokens())),
        Arc::new(FakeRevoker::new()),
        Arc::new(FakeOperations::new()),
    );
    let mut request = execute_request(session_id);
    request.operation.as_mut().unwrap().arguments = BTreeMap::new();
    let response = subject.handle(request);
    assert_eq!(
        error_code(&response),
        Some(ErrorCode::OperationNotSupported)
    );
    assert_eq!(store.reads(), 0);
    assert!(response_is_safe(&response));
}

#[test]
fn cancellation_causes_no_operation_dispatch() {
    let authorized = authorized_session(false);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    store.set_read_error(CoreError::new(ErrorCode::UserPresenceCancelled));
    let operations = Arc::new(FakeOperations::new());
    let response = service(
        store.clone(),
        Arc::new(FakeRefresher::new(rotated_tokens())),
        Arc::new(FakeRevoker::new()),
        operations.clone(),
    )
    .handle(execute_request(session_id));
    assert_eq!(
        error_code(&response),
        Some(ErrorCode::UserPresenceCancelled)
    );
    assert_eq!(store.reads(), 1);
    assert_eq!(operations.count(), 0);
    assert!(response_is_safe(&response));
}

#[test]
fn unexpired_session_executes_once_without_refresh() {
    let authorized = authorized_session(false);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    let refresher = Arc::new(FakeRefresher::new(rotated_tokens()));
    let operations = Arc::new(FakeOperations::new());
    let response = service(
        store.clone(),
        refresher.clone(),
        Arc::new(FakeRevoker::new()),
        operations.clone(),
    )
    .handle(execute_request(session_id));
    assert!(response.ok);
    assert_eq!(refresher.count(), 0);
    assert_eq!(operations.count(), 1);
    assert_eq!(operations.access_tokens(), vec![ACCESS_MARKER]);
    assert!(response_is_safe(&response));
}

#[test]
fn refresh_rotation_is_replaced_before_operation_dispatch() {
    let authorized = authorized_session(true);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    let refresher = Arc::new(FakeRefresher::new(rotated_tokens()));
    let operations = Arc::new(FakeOperations::new());
    let response = service(
        store.clone(),
        refresher.clone(),
        Arc::new(FakeRevoker::new()),
        operations.clone(),
    )
    .handle(execute_request(session_id));
    assert!(response.ok);
    assert_eq!(refresher.count(), 1);
    assert_eq!(store.replacements(), 1);
    assert_eq!(store.events(), vec!["read", "replace"]);
    assert_eq!(operations.access_tokens(), vec![ROTATED_ACCESS_MARKER]);
    assert!(response_is_safe(&response));
}

#[test]
fn failed_atomic_replacement_blocks_dispatch_and_marks_reauthorization() {
    let authorized = authorized_session(true);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    store.set_replace_error(CoreError::new(ErrorCode::InternalFailure));
    let operations = Arc::new(FakeOperations::new());
    let response = service(
        store.clone(),
        Arc::new(FakeRefresher::new(rotated_tokens())),
        Arc::new(FakeRevoker::new()),
        operations.clone(),
    )
    .handle(execute_request(session_id));
    assert_eq!(
        error_code(&response),
        Some(ErrorCode::SessionReauthorizationRequired)
    );
    assert_eq!(operations.count(), 0);
    assert_eq!(store.reauthorizations(), 1);
    assert!(response_is_safe(&response));
}

#[test]
fn descriptor_and_protected_record_mismatch_fails_closed() {
    let authorized = authorized_session(false);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    store.corrupt_binding();
    let operations = Arc::new(FakeOperations::new());
    let response = service(
        store.clone(),
        Arc::new(FakeRefresher::new(rotated_tokens())),
        Arc::new(FakeRevoker::new()),
        operations.clone(),
    )
    .handle(execute_request(session_id));
    assert_eq!(
        error_code(&response),
        Some(ErrorCode::SessionReauthorizationRequired)
    );
    assert_eq!(operations.count(), 0);
    assert!(response_is_safe(&response));
}

#[test]
fn concurrent_expiry_causes_one_refresh_and_no_token_ipc() {
    let authorized = authorized_session(true);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    let refresher = Arc::new(FakeRefresher::new(rotated_tokens()));
    let operations = Arc::new(FakeOperations::new());
    let subject = Arc::new(service(
        store.clone(),
        refresher.clone(),
        Arc::new(FakeRevoker::new()),
        operations.clone(),
    ));
    let handles: Vec<_> = (0..2)
        .map(|_| {
            let subject = subject.clone();
            thread::spawn(move || subject.handle(execute_request(session_id)))
        })
        .collect();
    let responses: Vec<_> = handles
        .into_iter()
        .map(|value| value.join().unwrap())
        .collect();
    assert!(responses
        .iter()
        .all(|value| value.ok && response_is_safe(value)));
    assert_eq!(refresher.count(), 1);
    assert_eq!(store.replacements(), 1);
    assert_eq!(operations.count(), 2);
}

#[test]
fn removal_revokes_before_deleting_and_returns_no_credentials() {
    let authorized = authorized_session(false);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    let revoker = Arc::new(FakeRevoker::new());
    let response = service(
        store.clone(),
        Arc::new(FakeRefresher::new(rotated_tokens())),
        revoker.clone(),
        Arc::new(FakeOperations::new()),
    )
    .handle(remove_request(session_id));
    assert!(response.ok);
    assert_eq!(revoker.count(), 1);
    assert!(revoker.all_refresh_tokens_were_internal());
    assert_eq!(store.removals(), 1);
    assert_eq!(store.events(), vec!["read", "remove"]);
    assert!(!store.has_session());
    assert!(response_is_safe(&response));
}

#[test]
fn failed_revocation_preserves_the_protected_session() {
    let authorized = authorized_session(false);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    let revoker = Arc::new(FakeRevoker::new());
    revoker.fail(CoreError::new(ErrorCode::InternalFailure));
    let response = service(
        store.clone(),
        Arc::new(FakeRefresher::new(rotated_tokens())),
        revoker,
        Arc::new(FakeOperations::new()),
    )
    .handle(remove_request(session_id));
    assert_eq!(
        error_code(&response),
        Some(ErrorCode::OAuthRevocationFailed)
    );
    assert_eq!(store.removals(), 0);
    assert!(store.has_session());
    assert!(response_is_safe(&response));
}

#[test]
fn purge_lists_and_removes_all_known_sessions() {
    let authorized = authorized_session(false);
    let store = Arc::new(FakeStore::with_session(authorized));
    let revoker = Arc::new(FakeRevoker::new());
    let response = service(
        store.clone(),
        Arc::new(FakeRefresher::new(rotated_tokens())),
        revoker.clone(),
        Arc::new(FakeOperations::new()),
    )
    .handle(HelperRequest {
        protocol_version: PROTOCOL_VERSION,
        request_id: Uuid::new_v4().to_string(),
        command: HelperCommand::PurgeAllSessions,
        session_id: None,
        target: None,
        operation: None,
    });
    assert!(response.ok);
    assert_eq!(store.lists(), 1);
    assert_eq!(revoker.count(), 1);
    assert!(!store.has_session());
    assert!(response_is_safe(&response));
}

#[test]
fn operation_reauthorization_failure_is_persisted() {
    let authorized = authorized_session(false);
    let session_id = authorized.session.session_id;
    let store = Arc::new(FakeStore::with_session(authorized));
    let operations = Arc::new(FakeOperations::new());
    operations.fail(CoreError::new(ErrorCode::SessionReauthorizationRequired));
    let response = service(
        store.clone(),
        Arc::new(FakeRefresher::new(rotated_tokens())),
        Arc::new(FakeRevoker::new()),
        operations,
    )
    .handle(execute_request(session_id));
    assert_eq!(
        error_code(&response),
        Some(ErrorCode::SessionReauthorizationRequired)
    );
    assert_eq!(store.reauthorizations(), 1);
    assert!(response_is_safe(&response));
}
