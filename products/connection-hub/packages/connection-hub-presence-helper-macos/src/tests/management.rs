use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;

use serde_json::json;
use url::Url;
use uuid::Uuid;

use crate::browser::Browser;
use crate::error::ErrorCode;
use crate::management::{
    management_request_digest, KdcubeManagementOperations, INSPECT_OPERATION, RELOAD_OPERATION,
    SURFACES_OPERATION,
};
use crate::service::OperationRegistry;
use crate::validation::ValidatedInvocation;

use super::fixtures::{
    authorized_session, json_response, FakeBrowser, FakeTransport, ACCESS_MARKER, REFRESH_MARKER,
};

fn invocation(operation_id: &str, application_id: Option<&str>) -> ValidatedInvocation {
    ValidatedInvocation {
        operation_id: operation_id.to_owned(),
        invocation_id: Uuid::new_v4(),
        arguments: application_id
            .map(|value| BTreeMap::from([("application_id".to_owned(), value.to_owned())]))
            .unwrap_or_default(),
    }
}

fn success_response(
    operation: &str,
    invocation_id: Uuid,
    application_id: Option<&str>,
    tenant: &str,
    generation: &str,
) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "schema": "kdcube.management.result.v1",
        "ok": true,
        "operation": operation,
        "resource": "urn:kdcube:management:deployment:tenant-a:project-a",
        "target": {"tenant": tenant, "project": "project-a"},
        "invocation": {"id": invocation_id.hyphenated().to_string()},
        "authority": {
            "card_revision": 4,
            "active_catalog_version": "catalog-3",
            "invocation_policy_revision": 7
        },
        "result": {
            "application_id": application_id,
            "state": "completed",
            "generation": generation
        }
    }))
    .unwrap()
}

fn approval_url(
    origin: &str,
    invocation_id: Uuid,
    digest: &str,
    extra: Option<(&str, &str)>,
) -> Url {
    let mut url = Url::parse(&format!(
        "{origin}/api/integrations/bundles/tenant-a/project-a/connection-hub@1-0/widgets/connections_settings"
    ))
    .unwrap();
    {
        let mut query = url.query_pairs_mut();
        query
            .append_pair("tab", "delegated_by_kdcube")
            .append_pair(
                "resource",
                "urn:kdcube:management:deployment:tenant-a:project-a",
            )
            .append_pair("outer_operation", RELOAD_OPERATION)
            .append_pair("invocation_policy", "choose")
            .append_pair(
                "invocation_change_id",
                &invocation_id.hyphenated().to_string(),
            )
            .append_pair("request_bound", "1")
            .append_pair("request_digest", digest)
            .append_pair("request_card_revision", "4")
            .append_pair("request_authority_revision", "catalog-3")
            .append_pair("request_approval_ticket", "signed-ticket-value")
            .append_pair("access_id", "access-123")
            .append_pair("approval_application_id", "workspace@1-0");
        if let Some((name, value)) = extra {
            query.append_pair(name, value);
        }
    }
    url
}

fn recovery_response(
    invocation_id: Uuid,
    browser_origin: &str,
    envelope_digest: &str,
    query_digest: &str,
    extra: Option<(&str, &str)>,
) -> Vec<u8> {
    let url = approval_url(browser_origin, invocation_id, query_digest, extra);
    serde_json::to_vec(&json!({
        "schema": "kdcube.management.error.v1",
        "ok": false,
        "operation": RELOAD_OPERATION,
        "resource": "urn:kdcube:management:deployment:tenant-a:project-a",
        "target": {"tenant": "tenant-a", "project": "project-a"},
        "invocation_id": invocation_id.hyphenated().to_string(),
        "error": {
            "code": "delegated_request_permit_required",
            "retryable": false
        },
        "recovery": {
            "type": "consent_required",
            "reason": "delegated_request_permit_required",
            "authorization_url": url.as_str(),
            "access_id": "access-123",
            "resource": "urn:kdcube:management:deployment:tenant-a:project-a",
            "operation": RELOAD_OPERATION,
            "application_id": "workspace@1-0",
            "invocation_id": invocation_id.hyphenated().to_string(),
            "request_digest": envelope_digest,
            "card_revision": 4,
            "catalog_version": "catalog-3",
            "permit_ttl_seconds": 600,
            "choices": ["allow_once", "allow_always"]
        }
    }))
    .unwrap()
}

#[test]
fn registered_reload_binds_method_path_body_prompt_and_credential() {
    let session = authorized_session(false).session;
    let invocation = invocation(RELOAD_OPERATION, Some("workspace@1-0"));
    let transport = Arc::new(FakeTransport::new(vec![Ok(json_response(
        success_response(
            RELOAD_OPERATION,
            invocation.invocation_id,
            Some("workspace@1-0"),
            "tenant-a",
            "generation-2",
        ),
        200,
    ))]));
    let registry =
        KdcubeManagementOperations::new(transport.clone(), Arc::new(FakeBrowser::default()));

    let prompt = registry.prompt(&session.target, &invocation).unwrap();
    let evidence = registry.execute(&session, &invocation).unwrap();
    let request = transport.requests().remove(0);
    let headers: HashMap<_, _> = request.headers.into_iter().collect();

    assert_eq!(
        prompt,
        "Reload application workspace@1-0 for tenant-a/project-a at https://target.example:443"
    );
    assert_eq!(
        request.url.as_str(),
        "https://target.example/api/integrations/management/v1/applications/workspace%401-0/reload"
    );
    assert_eq!(request.method, "POST");
    assert_eq!(request.body, b"{}");
    assert_eq!(
        headers.get("Authorization").map(String::as_str),
        Some("Bearer ACCESS-MARKER-MUST-NEVER-CROSS-IPC")
    );
    assert_eq!(
        headers.get("Idempotency-Key").map(String::as_str),
        Some(invocation.invocation_id.hyphenated().to_string().as_str())
    );
    assert_eq!(evidence.application_id.as_deref(), Some("workspace@1-0"));
    assert_eq!(evidence.generation.as_deref(), Some("generation-2"));
    let encoded = serde_json::to_string(&evidence).unwrap();
    assert!(!encoded.contains(ACCESS_MARKER));
    assert!(!encoded.contains(REFRESH_MARKER));
}

#[test]
fn application_identifier_uses_rfc3986_unreserved_path_encoding() {
    let session = authorized_session(false).session;
    let invocation = invocation(SURFACES_OPERATION, Some("café._~-@1-0"));
    let transport = Arc::new(FakeTransport::new(vec![Ok(json_response(
        success_response(
            SURFACES_OPERATION,
            invocation.invocation_id,
            Some("café._~-@1-0"),
            "tenant-a",
            "generation-2",
        ),
        200,
    ))]));
    let registry =
        KdcubeManagementOperations::new(transport.clone(), Arc::new(FakeBrowser::default()));
    registry.execute(&session, &invocation).unwrap();
    assert_eq!(
        transport.requests()[0].url.path(),
        "/api/integrations/management/v1/applications/caf%C3%A9._~-%401-0/surfaces"
    );
}

#[test]
fn management_digest_matches_the_frozen_python_contract() {
    let resource = "urn:kdcube:management:deployment:tenant-a:project-a";
    assert_eq!(
        management_request_digest(RELOAD_OPERATION, resource, "workspace@1-0"),
        "93775a1522e4458c47a3511ec7f3cfdee219a911498202c9808bae84c36c6f07"
    );
    assert_eq!(
        management_request_digest(RELOAD_OPERATION, resource, "café@1-0"),
        "f6d5b63954ab5a9f9f40b9c636b6d9e0dad04103c0b95ef8fb4fefb806a83ca6"
    );
}

#[test]
fn exact_request_bound_denial_opens_only_the_validated_url() {
    let session = authorized_session(false).session;
    let invocation = invocation(RELOAD_OPERATION, Some("workspace@1-0"));
    let digest = management_request_digest(
        RELOAD_OPERATION,
        &session.metadata.resource,
        "workspace@1-0",
    );
    let transport = Arc::new(FakeTransport::new(vec![Ok(json_response(
        recovery_response(
            invocation.invocation_id,
            "https://target.example",
            &digest,
            &digest,
            None,
        ),
        403,
    ))]));
    let browser = Arc::new(FakeBrowser::default());
    let registry = KdcubeManagementOperations::new(transport, browser.clone());

    let error = registry.execute(&session, &invocation).unwrap_err();
    assert_eq!(error.code, ErrorCode::OperationApprovalRequired);
    assert!(error.retryable);
    assert_eq!(browser.urls().len(), 1);
    assert_eq!(browser.urls()[0].host_str(), Some("target.example"));
}

#[test]
fn cross_origin_recovery_is_never_opened() {
    let session = authorized_session(false).session;
    let invocation = invocation(RELOAD_OPERATION, Some("workspace@1-0"));
    let digest = management_request_digest(
        RELOAD_OPERATION,
        &session.metadata.resource,
        "workspace@1-0",
    );
    let browser = Arc::new(FakeBrowser::default());
    let registry = KdcubeManagementOperations::new(
        Arc::new(FakeTransport::new(vec![Ok(json_response(
            recovery_response(
                invocation.invocation_id,
                "https://attacker.example",
                &digest,
                &digest,
                None,
            ),
            403,
        ))])),
        browser.clone(),
    );
    let error = registry.execute(&session, &invocation).unwrap_err();
    assert_eq!(error.code, ErrorCode::OperationFailed);
    assert!(browser.urls().is_empty());
}

#[test]
fn mismatched_recovery_digest_is_never_opened() {
    let session = authorized_session(false).session;
    let invocation = invocation(RELOAD_OPERATION, Some("workspace@1-0"));
    let digest = management_request_digest(
        RELOAD_OPERATION,
        &session.metadata.resource,
        "workspace@1-0",
    );
    let browser = Arc::new(FakeBrowser::default());
    let registry = KdcubeManagementOperations::new(
        Arc::new(FakeTransport::new(vec![Ok(json_response(
            recovery_response(
                invocation.invocation_id,
                "https://target.example",
                &digest,
                &"0".repeat(64),
                None,
            ),
            403,
        ))])),
        browser.clone(),
    );
    let error = registry.execute(&session, &invocation).unwrap_err();
    assert_eq!(error.code, ErrorCode::OperationFailed);
    assert!(browser.urls().is_empty());
}

#[test]
fn unknown_or_protected_recovery_query_is_never_opened() {
    let session = authorized_session(false).session;
    let invocation = invocation(RELOAD_OPERATION, Some("workspace@1-0"));
    let digest = management_request_digest(
        RELOAD_OPERATION,
        &session.metadata.resource,
        "workspace@1-0",
    );
    for value in ["untrusted", ACCESS_MARKER] {
        let browser = Arc::new(FakeBrowser::default());
        let registry = KdcubeManagementOperations::new(
            Arc::new(FakeTransport::new(vec![Ok(json_response(
                recovery_response(
                    invocation.invocation_id,
                    "https://target.example",
                    &digest,
                    &digest,
                    Some(("untrusted_hint", value)),
                ),
                403,
            ))])),
            browser.clone(),
        );
        let error = registry.execute(&session, &invocation).unwrap_err();
        assert_eq!(error.code, ErrorCode::OperationFailed);
        assert!(browser.urls().is_empty());
    }
}

#[test]
fn mismatched_management_evidence_fails_closed() {
    let session = authorized_session(false).session;
    let invocation = invocation(INSPECT_OPERATION, None);
    let registry = KdcubeManagementOperations::new(
        Arc::new(FakeTransport::new(vec![Ok(json_response(
            success_response(
                INSPECT_OPERATION,
                invocation.invocation_id,
                None,
                "other-tenant",
                "generation-2",
            ),
            200,
        ))])),
        Arc::new(FakeBrowser::default()),
    );
    assert_eq!(
        registry.execute(&session, &invocation).unwrap_err().code,
        ErrorCode::OperationFailed
    );
}

#[test]
fn success_evidence_cannot_echo_protected_material() {
    let session = authorized_session(false).session;
    let invocation = invocation(RELOAD_OPERATION, Some("workspace@1-0"));
    let registry = KdcubeManagementOperations::new(
        Arc::new(FakeTransport::new(vec![Ok(json_response(
            success_response(
                RELOAD_OPERATION,
                invocation.invocation_id,
                Some("workspace@1-0"),
                "tenant-a",
                &format!("generation-{ACCESS_MARKER}"),
            ),
            200,
        ))])),
        Arc::new(FakeBrowser::default()),
    );
    let error = registry.execute(&session, &invocation).unwrap_err();
    assert_eq!(error.code, ErrorCode::OperationFailed);
    assert!(!error.to_string().contains(ACCESS_MARKER));
}

#[test]
fn unsupported_operation_is_rejected_without_transport() {
    let session = authorized_session(false).session;
    let transport = Arc::new(FakeTransport::new(Vec::new()));
    let registry =
        KdcubeManagementOperations::new(transport.clone(), Arc::new(FakeBrowser::default()));
    let error = registry
        .execute(&session, &invocation("unregistered.operation", None))
        .unwrap_err();
    assert_eq!(error.code, ErrorCode::OperationNotSupported);
    assert!(transport.requests().is_empty());
}

#[test]
fn browser_trait_does_not_receive_credentials() {
    let browser = FakeBrowser::default();
    let url = Url::parse("https://target.example/approve?ticket=signed").unwrap();
    assert!(browser.open(&url));
    let rendered = browser.urls()[0].as_str().to_owned();
    assert!(!rendered.contains(ACCESS_MARKER));
    assert!(!rendered.contains(REFRESH_MARKER));
}
