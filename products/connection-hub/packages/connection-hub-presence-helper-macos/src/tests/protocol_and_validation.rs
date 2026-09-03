use std::collections::BTreeMap;

use serde_json::json;
use uuid::Uuid;

use crate::error::ErrorCode;
use crate::process_io::{encode_response, read_request};
use crate::protocol::{
    HelperCommand, HelperRequest, HelperResponse, OperationInvocation, SafeResult,
    TargetCoordinates, MAX_REQUEST_BYTES,
};
use crate::session::{session_binding_digest, TargetRecord};
use crate::validation::{ValidatedInvocation, ValidatedTarget};

use super::fixtures::{authorized_session, metadata, ACCESS_MARKER, REFRESH_MARKER};

fn request_id() -> &'static str {
    "00000000-0000-4000-8000-000000000001"
}

#[test]
fn strict_decoder_accepts_the_minimal_status_request() {
    let request = HelperRequest::decode(
        br#"{"protocol_version":1,"request_id":"00000000-0000-4000-8000-000000000001","command":"status"}"#,
    )
    .unwrap();
    assert_eq!(request.command, HelperCommand::Status);
    assert_eq!(request.request_id, request_id());
}

#[test]
fn strict_decoder_rejects_unknown_fields() {
    let value = format!(
        r#"{{"protocol_version":1,"request_id":"{}","command":"status","credential":"{}"}}"#,
        request_id(),
        ACCESS_MARKER
    );
    let error = HelperRequest::decode(value.as_bytes()).unwrap_err();
    assert_eq!(error.code, ErrorCode::InvalidRequest);
    assert_eq!(error.request_id.as_deref(), Some(request_id()));
}

#[test]
fn invalid_request_id_is_not_reflected() {
    let value = format!(
        r#"{{"protocol_version":1,"request_id":"{}","command":"status"}}"#,
        ACCESS_MARKER
    );
    let error = HelperRequest::decode(value.as_bytes()).unwrap_err();
    assert_eq!(error.code, ErrorCode::InvalidRequest);
    assert_eq!(error.request_id, None);
}

#[test]
fn oversized_ipc_input_fails_closed_without_parsing() {
    let input = vec![b'x'; MAX_REQUEST_BYTES + 1];
    let error = HelperRequest::decode(&input).unwrap_err();
    assert_eq!(error.code, ErrorCode::RequestTooLarge);
    assert_eq!(error.request_id, None);
    assert_eq!(
        read_request(input.as_slice()).unwrap_err().code,
        ErrorCode::RequestTooLarge
    );
}

#[test]
fn command_shape_is_exact() {
    let value = json!({
        "protocol_version": 1,
        "request_id": request_id(),
        "command": "execute_operation",
        "session_id": Uuid::new_v4().to_string()
    });
    let error = HelperRequest::decode(value.to_string().as_bytes()).unwrap_err();
    assert_eq!(error.code, ErrorCode::InvalidRequest);
}

#[test]
fn response_serialization_never_includes_protected_tokens() {
    let session = authorized_session(false).session;
    let response = HelperResponse::success(
        request_id().to_owned(),
        SafeResult::Session {
            session: crate::protocol::SafeSessionSummary {
                session_id: session.session_id.to_string(),
                normalized_origin: session.target.normalized_origin,
                tenant: session.target.tenant,
                project: session.target.project,
                access_expires_at: "2027-01-15T08:00:00Z".to_owned(),
            },
        },
    );
    let encoded = encode_response(&response).unwrap();
    let text = String::from_utf8(encoded).unwrap();
    assert!(!text.contains(ACCESS_MARKER));
    assert!(!text.contains(REFRESH_MARKER));
}

#[test]
fn target_normalization_includes_default_port() {
    let target = ValidatedTarget::try_from(TargetCoordinates {
        origin: "HTTPS://Example.COM".to_owned(),
        tenant: "tenant-a".to_owned(),
        project: "project-a".to_owned(),
        caller_profile: "human-admin".to_owned(),
        oauth_client_id: None,
    })
    .unwrap();
    assert_eq!(target.normalized_origin, "https://example.com:443");
}

#[test]
fn plaintext_is_limited_to_literal_loopback_hosts() {
    for origin in ["http://target.example", "http://127.0.0.1.example"] {
        let error = ValidatedTarget::try_from(TargetCoordinates {
            origin: origin.to_owned(),
            tenant: "tenant-a".to_owned(),
            project: "project-a".to_owned(),
            caller_profile: "human-admin".to_owned(),
            oauth_client_id: None,
        })
        .unwrap_err();
        assert_eq!(error.code, ErrorCode::InvalidRequest);
    }
    let loopback = ValidatedTarget::try_from(TargetCoordinates {
        origin: "http://127.0.0.1:9123".to_owned(),
        tenant: "tenant-a".to_owned(),
        project: "project-a".to_owned(),
        caller_profile: "human-admin".to_owned(),
        oauth_client_id: None,
    })
    .unwrap();
    assert_eq!(loopback.normalized_origin, "http://127.0.0.1:9123");
}

#[test]
fn operation_arguments_are_bounded_and_structured() {
    let invalid = ValidatedInvocation::try_from(OperationInvocation {
        operation_id: "fixture.inspect".to_owned(),
        invocation_id: Uuid::new_v4().to_string(),
        arguments: BTreeMap::from([("Bad-Name".to_owned(), "value".to_owned())]),
    })
    .unwrap_err();
    assert_eq!(invalid.code, ErrorCode::InvalidRequest);
}

#[test]
fn protected_models_redact_debug_output() {
    let session = authorized_session(false).session;
    let rendered = format!("{session:?} {:?}", session.tokens);
    assert!(!rendered.contains(ACCESS_MARKER));
    assert!(!rendered.contains(REFRESH_MARKER));
    assert!(rendered.contains("redacted"));
}

#[test]
fn session_binding_changes_with_security_relevant_metadata() {
    let session = authorized_session(false).session;
    let mut changed = metadata();
    changed.token_endpoint = "https://target.example:443/oauth/other-token".to_owned();
    let changed_digest = session_binding_digest(session.session_id, &session.target, &changed);
    assert_ne!(session.binding_digest, changed_digest);
    assert_eq!(
        session.binding_digest,
        session_binding_digest(
            session.session_id,
            &TargetRecord::from(&super::fixtures::validated_target()),
            &metadata()
        )
    );
}
