use std::error::Error;
use std::sync::Arc;

use serde_json::json;
use url::Url;

use crate::error::ErrorCode;
use crate::oauth::{
    OAuthPolicy, StandardOAuthAuthorizer, StandardOAuthRefresher, StandardOAuthRevoker,
};
use crate::service::{OAuthAuthorizer, OAuthRefresher, OAuthRevoker};
use crate::session::TargetRecord;

use super::fixtures::{
    authorized_session, json_response, validated_target, FakeBrowser, FakeCallbackFactory,
    FakeTransport, FixedClock, ACCESS_MARKER, ERROR_MARKER, NOW, REFRESH_MARKER,
    ROTATED_ACCESS_MARKER, ROTATED_REFRESH_MARKER,
};

fn issuer() -> Url {
    Url::parse("https://target.example:443/oauth").unwrap()
}

fn protected_resource(scopes: Option<Vec<&str>>) -> Vec<u8> {
    let mut value = json!({
        "resource": "urn:kdcube:management:deployment:tenant-a:project-a",
        "authorization_servers": [issuer().as_str()]
    });
    if let Some(scopes) = scopes {
        value["scopes_supported"] = json!(scopes);
    }
    serde_json::to_vec(&value).unwrap()
}

fn server_metadata(token_endpoint: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "issuer": issuer().as_str(),
        "authorization_endpoint": issuer().join("authorize").unwrap().as_str(),
        "token_endpoint": token_endpoint,
        "revocation_endpoint": issuer().join("revoke").unwrap().as_str(),
        "registration_endpoint": issuer().join("register").unwrap().as_str(),
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "authorization_response_iss_parameter_supported": true
    }))
    .unwrap()
}

fn registration(redirect_uri: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "client_id": "registered-presence-helper",
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none"
    }))
    .unwrap()
}

fn token_response(access: &str, refresh: Option<&str>) -> Vec<u8> {
    let mut value = json!({
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token_expires_in": 7200
    });
    if let Some(refresh) = refresh {
        value["refresh_token"] = json!(refresh);
    }
    serde_json::to_vec(&value).unwrap()
}

#[test]
fn authorization_owns_discovery_pkce_registration_and_token_exchange() {
    let callback = Arc::new(FakeCallbackFactory::new());
    let transport = Arc::new(FakeTransport::new(vec![
        Ok(json_response(protected_resource(None), 200)),
        Ok(json_response(
            server_metadata(issuer().join("token").unwrap().as_str()),
            200,
        )),
        Ok(json_response(
            registration(callback.redirect_uri().as_str()),
            201,
        )),
        Ok(json_response(
            token_response(ACCESS_MARKER, Some(REFRESH_MARKER)),
            200,
        )),
    ]));
    let browser = Arc::new(FakeBrowser::default());
    let authorizer = StandardOAuthAuthorizer::new(
        OAuthPolicy::new(Vec::new()).unwrap(),
        transport.clone(),
        browser.clone(),
        callback,
        Arc::new(FixedClock::new(NOW)),
    );

    let authorized = authorizer.authorize(&validated_target()).unwrap();

    assert_eq!(
        authorized.session.tokens.access_token.expose(),
        ACCESS_MARKER
    );
    assert_eq!(
        authorized.session.tokens.refresh_token.expose(),
        REFRESH_MARKER
    );
    assert_eq!(authorized.session.tokens.access_expires_at, NOW + 3600);
    assert_eq!(transport.requests().len(), 4);
    let opened = browser.urls();
    assert_eq!(opened.len(), 1);
    let query: std::collections::BTreeMap<_, _> = opened[0]
        .query_pairs()
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();
    assert_eq!(
        query.get("code_challenge_method").map(String::as_str),
        Some("S256")
    );
    assert_eq!(
        query.get("resource").map(String::as_str),
        Some(authorized.session.metadata.resource.as_str())
    );
    let rendered = serde_json::to_string(&crate::protocol::SafeSessionSummary {
        session_id: authorized.session.session_id.to_string(),
        normalized_origin: authorized.session.target.normalized_origin.clone(),
        tenant: authorized.session.target.tenant.clone(),
        project: authorized.session.target.project.clone(),
        access_expires_at: "2027-01-15T09:00:00Z".to_owned(),
    })
    .unwrap();
    assert!(!rendered.contains(ACCESS_MARKER));
    assert!(!rendered.contains(REFRESH_MARKER));
}

#[test]
fn oauth_metadata_cannot_redirect_tokens_to_another_origin() {
    let transport = Arc::new(FakeTransport::new(vec![
        Ok(json_response(protected_resource(None), 200)),
        Ok(json_response(
            server_metadata("https://attacker.example/oauth/token"),
            200,
        )),
    ]));
    let browser = Arc::new(FakeBrowser::default());
    let authorizer = StandardOAuthAuthorizer::new(
        OAuthPolicy::new(Vec::new()).unwrap(),
        transport.clone(),
        browser.clone(),
        Arc::new(FakeCallbackFactory::new()),
        Arc::new(FixedClock::new(NOW)),
    );

    let error = match authorizer.authorize(&validated_target()) {
        Ok(_) => panic!("cross-origin token endpoint was accepted"),
        Err(error) => error,
    };
    assert_eq!(error.code, ErrorCode::OAuthProtocolUnavailable);
    assert!(browser.urls().is_empty());
    assert_eq!(transport.requests().len(), 2);
}

#[test]
fn omitted_scope_catalog_is_valid_when_no_scopes_are_requested() {
    let callback = Arc::new(FakeCallbackFactory::new());
    let transport = Arc::new(FakeTransport::new(vec![
        Ok(json_response(protected_resource(None), 200)),
        Ok(json_response(
            server_metadata(issuer().join("token").unwrap().as_str()),
            200,
        )),
        Ok(json_response(
            registration(callback.redirect_uri().as_str()),
            201,
        )),
        Ok(json_response(
            token_response(ACCESS_MARKER, Some(REFRESH_MARKER)),
            200,
        )),
    ]));
    let authorizer = StandardOAuthAuthorizer::new(
        OAuthPolicy::new(Vec::new()).unwrap(),
        transport,
        Arc::new(FakeBrowser::default()),
        callback,
        Arc::new(FixedClock::new(NOW)),
    );
    assert!(authorizer.authorize(&validated_target()).is_ok());
}

#[test]
fn provisioned_client_id_bypasses_dynamic_registration() {
    let mut target = validated_target();
    target.oauth_client_id = Some("provisioned-native-client".to_owned());
    let transport = Arc::new(FakeTransport::new(vec![
        Ok(json_response(protected_resource(None), 200)),
        Ok(json_response(
            server_metadata(issuer().join("token").unwrap().as_str()),
            200,
        )),
        Ok(json_response(
            token_response(ACCESS_MARKER, Some(REFRESH_MARKER)),
            200,
        )),
    ]));
    let authorizer = StandardOAuthAuthorizer::new(
        OAuthPolicy::new(Vec::new()).unwrap(),
        transport.clone(),
        Arc::new(FakeBrowser::default()),
        Arc::new(FakeCallbackFactory::new()),
        Arc::new(FixedClock::new(NOW)),
    );

    let authorized = authorizer.authorize(&target).unwrap();
    assert_eq!(
        authorized.session.metadata.client_id,
        "provisioned-native-client"
    );
    assert_eq!(transport.requests().len(), 3);
    assert!(transport
        .requests()
        .iter()
        .all(|request| !request.url.path().ends_with("/register")));
}

#[test]
fn refresh_token_rotation_remains_inside_the_helper() {
    let transport = Arc::new(FakeTransport::new(vec![Ok(json_response(
        token_response(ROTATED_ACCESS_MARKER, Some(ROTATED_REFRESH_MARKER)),
        200,
    ))]));
    let refresher = StandardOAuthRefresher::new(transport.clone(), Arc::new(FixedClock::new(NOW)));
    let session = authorized_session(true).session;

    let replacement = refresher.refresh(&session).unwrap();

    assert_eq!(replacement.access_token.expose(), ROTATED_ACCESS_MARKER);
    assert_eq!(replacement.refresh_token.expose(), ROTATED_REFRESH_MARKER);
    let request = transport.requests().remove(0);
    let body = String::from_utf8(request.body).unwrap();
    assert!(body.contains(&format!("refresh_token={REFRESH_MARKER}")));
    assert!(!format!("{replacement:?}").contains(ROTATED_REFRESH_MARKER));
}

#[test]
fn refresh_without_rotation_preserves_the_existing_refresh_token_internally() {
    let transport = Arc::new(FakeTransport::new(vec![Ok(json_response(
        token_response(ROTATED_ACCESS_MARKER, None),
        200,
    ))]));
    let refresher = StandardOAuthRefresher::new(transport, Arc::new(FixedClock::new(NOW)));
    let replacement = refresher
        .refresh(&authorized_session(true).session)
        .unwrap();
    assert_eq!(replacement.refresh_token.expose(), REFRESH_MARKER);
}

#[test]
fn malformed_token_response_maps_to_a_fixed_error() {
    let marker_body = serde_json::to_vec(&json!({
        "access_token": ERROR_MARKER,
        "refresh_token": REFRESH_MARKER,
        "token_type": "attacker-selected-type",
        "expires_in": 3600
    }))
    .unwrap();
    let transport = Arc::new(FakeTransport::new(vec![Ok(json_response(
        marker_body,
        200,
    ))]));
    let refresher = StandardOAuthRefresher::new(transport, Arc::new(FixedClock::new(NOW)));
    let error = refresher
        .refresh(&authorized_session(true).session)
        .unwrap_err();
    assert_eq!(error.code, ErrorCode::OAuthAuthorizationFailed);
    assert!(!error.to_string().contains(ERROR_MARKER));
    assert!(error.source().is_none());
}

#[test]
fn revocation_uses_the_refresh_token_without_returning_it() {
    let transport = Arc::new(FakeTransport::new(vec![Ok(json_response(b"{}", 200))]));
    let revoker = StandardOAuthRevoker::new(transport.clone());
    let session = authorized_session(false).session;
    revoker.revoke(&session).unwrap();
    let request = transport.requests().remove(0);
    let body = String::from_utf8(request.body).unwrap();
    assert!(body.contains(&format!("token={REFRESH_MARKER}")));
    assert_eq!(request.url, session.metadata.revocation_endpoint().unwrap());
}

#[test]
fn management_resource_is_bound_to_target_coordinates() {
    let target = TargetRecord::from(&validated_target());
    assert_eq!(
        crate::management::management_resource(&target),
        "urn:kdcube:management:deployment:tenant-a:project-a"
    );
}
