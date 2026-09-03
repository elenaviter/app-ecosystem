use std::sync::Arc;
use std::time::Duration;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use rand::rngs::OsRng;
use rand::TryRngCore;
use serde::de::DeserializeOwned;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use url::form_urlencoded::Serializer;
use url::Url;
use uuid::Uuid;

use crate::browser::Browser;
use crate::callback::AuthorizationCallbackFactory;
use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::http::{json_content_type, HttpRequest, HttpTransport};
use crate::management::management_resource;
use crate::service::{Clock, OAuthAuthorizer, OAuthRefresher, OAuthRevoker};
use crate::session::{
    session_binding_digest, AuthorizedSession, OAuthMetadata, OAuthTokenSet, ProtectedOAuthSession,
    SessionDescriptor, TargetRecord,
};
use crate::validation::{same_origin, validate_endpoint, ValidatedTarget};

#[derive(Clone, Debug)]
pub struct OAuthPolicy {
    requested_scopes: Vec<String>,
    client_name: String,
    callback_timeout: Duration,
}

impl OAuthPolicy {
    pub fn new(requested_scopes: Vec<String>) -> CoreResult<Self> {
        let mut requested_scopes = requested_scopes;
        requested_scopes.sort();
        requested_scopes.dedup();
        if requested_scopes.len() > 16
            || requested_scopes
                .iter()
                .any(|value| value.is_empty() || value.len() > 128)
        {
            return Err(CoreError::new(ErrorCode::OAuthProtocolUnavailable));
        }
        Ok(Self {
            requested_scopes,
            client_name: "KDCube Connection Hub Presence Helper".to_owned(),
            callback_timeout: Duration::from_secs(300),
        })
    }
}

#[derive(Deserialize)]
struct ProtectedResourceMetadata {
    resource: String,
    authorization_servers: Vec<String>,
    scopes_supported: Option<Vec<String>>,
}

#[derive(Deserialize)]
struct AuthorizationServerMetadata {
    issuer: String,
    authorization_endpoint: String,
    token_endpoint: String,
    revocation_endpoint: Option<String>,
    registration_endpoint: Option<String>,
    grant_types_supported: Vec<String>,
    response_types_supported: Vec<String>,
    code_challenge_methods_supported: Vec<String>,
    token_endpoint_auth_methods_supported: Vec<String>,
    authorization_response_iss_parameter_supported: Option<bool>,
}

#[derive(Deserialize)]
struct ClientRegistrationResponse {
    client_id: String,
    redirect_uris: Vec<String>,
    token_endpoint_auth_method: Option<String>,
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    refresh_token: Option<String>,
    token_type: String,
    expires_in: f64,
    refresh_token_expires_in: Option<f64>,
}

pub struct StandardOAuthAuthorizer {
    policy: OAuthPolicy,
    transport: Arc<dyn HttpTransport>,
    browser: Arc<dyn Browser>,
    callbacks: Arc<dyn AuthorizationCallbackFactory>,
    clock: Arc<dyn Clock>,
}

impl StandardOAuthAuthorizer {
    pub fn new(
        policy: OAuthPolicy,
        transport: Arc<dyn HttpTransport>,
        browser: Arc<dyn Browser>,
        callbacks: Arc<dyn AuthorizationCallbackFactory>,
        clock: Arc<dyn Clock>,
    ) -> Self {
        Self {
            policy,
            transport,
            browser,
            callbacks,
            clock,
        }
    }

    fn discover(&self, target: &ValidatedTarget, redirect_uri: &Url) -> CoreResult<OAuthMetadata> {
        let origin = Url::parse(&target.normalized_origin)
            .map_err(|_| CoreError::new(ErrorCode::OAuthProtocolUnavailable))?;
        let metadata_url = origin
            .join("/api/integrations/management/v1/.well-known/oauth-protected-resource")
            .map_err(|_| CoreError::new(ErrorCode::OAuthProtocolUnavailable))?;
        let resource: ProtectedResourceMetadata = self.get_json(metadata_url)?;
        let expected_resource = management_resource(&TargetRecord::from(target));
        if resource.authorization_servers.len() != 1
            || resource.resource != expected_resource
            || !self.policy.requested_scopes.iter().all(|scope| {
                resource
                    .scopes_supported
                    .as_ref()
                    .is_some_and(|supported| supported.contains(scope))
            })
        {
            return Err(CoreError::new(ErrorCode::OAuthProtocolUnavailable));
        }
        let issuer = Url::parse(&resource.authorization_servers[0])
            .map_err(|_| CoreError::new(ErrorCode::OAuthProtocolUnavailable))?;
        validate_endpoint(&issuer)
            .map_err(|_| CoreError::new(ErrorCode::OAuthProtocolUnavailable))?;
        if !same_origin(&issuer, &origin) {
            return Err(CoreError::new(ErrorCode::OAuthProtocolUnavailable));
        }
        let server = self.authorization_server_metadata(&issuer)?;
        let authorization_endpoint = endpoint(&server.authorization_endpoint)?;
        let token_endpoint = endpoint(&server.token_endpoint)?;
        if server.issuer != issuer.as_str()
            || !server
                .grant_types_supported
                .iter()
                .any(|value| value == "authorization_code")
            || !server
                .grant_types_supported
                .iter()
                .any(|value| value == "refresh_token")
            || !server
                .response_types_supported
                .iter()
                .any(|value| value == "code")
            || !server
                .code_challenge_methods_supported
                .iter()
                .any(|value| value == "S256")
            || !server
                .token_endpoint_auth_methods_supported
                .iter()
                .any(|value| value == "none")
            || !same_origin(&authorization_endpoint, &issuer)
            || !same_origin(&token_endpoint, &issuer)
        {
            return Err(CoreError::new(ErrorCode::OAuthProtocolUnavailable));
        }
        let registration_endpoint =
            optional_same_origin_endpoint(server.registration_endpoint.as_deref(), &issuer)?;
        let revocation_endpoint =
            optional_same_origin_endpoint(server.revocation_endpoint.as_deref(), &issuer)?;
        let client_id = self.register_client(
            registration_endpoint.as_ref(),
            redirect_uri,
            target.oauth_client_id.as_deref(),
        )?;
        Ok(OAuthMetadata {
            issuer: issuer.to_string(),
            authorization_endpoint: authorization_endpoint.to_string(),
            token_endpoint: token_endpoint.to_string(),
            revocation_endpoint: revocation_endpoint.map(|value| value.to_string()),
            client_id,
            redirect_uri: redirect_uri.to_string(),
            resource: resource.resource,
            scopes: self.policy.requested_scopes.clone(),
            authorization_response_issuer_required: server
                .authorization_response_iss_parameter_supported
                .unwrap_or(false),
        })
    }

    fn authorization_server_metadata(
        &self,
        issuer: &Url,
    ) -> CoreResult<AuthorizationServerMetadata> {
        for candidate in authorization_metadata_urls(issuer) {
            if let Ok(value) = self.get_json(candidate) {
                return Ok(value);
            }
        }
        Err(CoreError::new(ErrorCode::OAuthProtocolUnavailable))
    }

    fn register_client(
        &self,
        endpoint: Option<&Url>,
        redirect_uri: &Url,
        provisioned_client_id: Option<&str>,
    ) -> CoreResult<String> {
        if let Some(client_id) = provisioned_client_id {
            return Ok(client_id.to_owned());
        }
        let endpoint =
            endpoint.ok_or_else(|| CoreError::new(ErrorCode::OAuthProtocolUnavailable))?;
        let body = serde_json::json!({
            "client_name": self.policy.client_name,
            "redirect_uris": [redirect_uri.as_str()],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none"
        });
        let mut request = HttpRequest::post(
            endpoint.clone(),
            serde_json::to_vec(&body)
                .map_err(|_| CoreError::new(ErrorCode::OAuthProtocolUnavailable))?,
        );
        request.headers = vec![
            ("Accept", "application/json".to_owned()),
            ("Content-Type", "application/json".to_owned()),
        ];
        let response = self.transport.send(request)?;
        if !(200..=299).contains(&response.status_code)
            || !json_content_type(response.content_type.as_deref())
        {
            return Err(CoreError::new(ErrorCode::OAuthProtocolUnavailable));
        }
        let registration: ClientRegistrationResponse = decode_json(&response.body)?;
        if registration.client_id.is_empty()
            || registration.client_id.len() > 512
            || registration.redirect_uris.len() > 16
            || !registration
                .redirect_uris
                .iter()
                .any(|value| value == redirect_uri.as_str())
            || registration
                .token_endpoint_auth_method
                .as_deref()
                .unwrap_or("none")
                != "none"
        {
            return Err(CoreError::new(ErrorCode::OAuthProtocolUnavailable));
        }
        Ok(registration.client_id)
    }

    fn get_json<T: DeserializeOwned>(&self, url: Url) -> CoreResult<T> {
        let mut request = HttpRequest::get(url);
        request.headers = vec![("Accept", "application/json".to_owned())];
        let response = self.transport.send(request)?;
        if !(200..=299).contains(&response.status_code)
            || !json_content_type(response.content_type.as_deref())
        {
            return Err(CoreError::new(ErrorCode::OAuthProtocolUnavailable));
        }
        decode_json(&response.body)
    }

    fn exchange_code(
        &self,
        code: &str,
        verifier: &str,
        metadata: &OAuthMetadata,
    ) -> CoreResult<OAuthTokenSet> {
        let token_endpoint = metadata.token_endpoint()?;
        let body = form_body(&[
            ("grant_type", "authorization_code"),
            ("code", code),
            ("redirect_uri", &metadata.redirect_uri),
            ("client_id", &metadata.client_id),
            ("code_verifier", verifier),
            ("resource", &metadata.resource),
        ]);
        let mut request = HttpRequest::post(token_endpoint, body);
        request.headers = form_headers();
        let response = self.transport.send(request)?;
        if !(200..=299).contains(&response.status_code)
            || !json_content_type(response.content_type.as_deref())
        {
            return Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed));
        }
        let token: TokenResponse = decode_json(&response.body)?;
        validate_token_response(&token, true)?;
        let TokenResponse {
            access_token,
            refresh_token,
            expires_in,
            refresh_token_expires_in,
            ..
        } = token;
        let refresh_token =
            refresh_token.ok_or_else(|| CoreError::new(ErrorCode::OAuthAuthorizationFailed))?;
        token_set(
            access_token,
            refresh_token,
            expires_in,
            refresh_token_expires_in,
            self.clock.now_unix(),
        )
    }
}

impl OAuthAuthorizer for StandardOAuthAuthorizer {
    fn authorize(&self, target: &ValidatedTarget) -> CoreResult<AuthorizedSession> {
        let mut callback = self.callbacks.create()?;
        let metadata = self.discover(target, callback.redirect_uri())?;
        let verifier = random_urlsafe(48)?;
        let state = random_urlsafe(32)?;
        let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
        let mut authorization_url = Url::parse(&metadata.authorization_endpoint)
            .map_err(|_| CoreError::new(ErrorCode::OAuthAuthorizationFailed))?;
        {
            let mut query = authorization_url.query_pairs_mut();
            query
                .clear()
                .append_pair("response_type", "code")
                .append_pair("client_id", &metadata.client_id)
                .append_pair("redirect_uri", &metadata.redirect_uri)
                .append_pair("resource", &metadata.resource)
                .append_pair("state", &state)
                .append_pair("code_challenge", &challenge)
                .append_pair("code_challenge_method", "S256");
            if !metadata.scopes.is_empty() {
                query.append_pair("scope", &metadata.scopes.join(" "));
            }
        }
        if !self.browser.open(&authorization_url) {
            return Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed));
        }
        let code = callback.receive_code(
            &state,
            &metadata.issuer,
            metadata.authorization_response_issuer_required,
            self.policy.callback_timeout,
        )?;
        let tokens = self.exchange_code(&code, &verifier, &metadata)?;
        let session_id = Uuid::new_v4();
        let target = TargetRecord::from(target);
        let binding_digest = session_binding_digest(session_id, &target, &metadata);
        let session = ProtectedOAuthSession {
            schema_version: 1,
            session_id,
            target: target.clone(),
            metadata,
            tokens,
            generation: 1,
            binding_digest: binding_digest.clone(),
        };
        Ok(AuthorizedSession {
            descriptor: SessionDescriptor {
                session_id,
                target,
                binding_digest,
                reauthorization_required: false,
            },
            session,
        })
    }
}

pub struct StandardOAuthRefresher {
    transport: Arc<dyn HttpTransport>,
    clock: Arc<dyn Clock>,
}

impl StandardOAuthRefresher {
    pub fn new(transport: Arc<dyn HttpTransport>, clock: Arc<dyn Clock>) -> Self {
        Self { transport, clock }
    }
}

impl OAuthRefresher for StandardOAuthRefresher {
    fn refresh(&self, session: &ProtectedOAuthSession) -> CoreResult<OAuthTokenSet> {
        let now = self.clock.now_unix();
        if session
            .tokens
            .refresh_expires_at
            .is_some_and(|expires| expires <= now)
        {
            return Err(CoreError::new(ErrorCode::SessionReauthorizationRequired));
        }
        let mut values = vec![
            ("grant_type", "refresh_token"),
            ("refresh_token", session.tokens.refresh_token.expose()),
            ("client_id", &session.metadata.client_id),
            ("resource", &session.metadata.resource),
        ];
        let scopes = session.metadata.scopes.join(" ");
        if !scopes.is_empty() {
            values.push(("scope", &scopes));
        }
        let mut request = HttpRequest::post(session.metadata.token_endpoint()?, form_body(&values));
        request.headers = form_headers();
        let response = self.transport.send(request)?;
        if !(200..=299).contains(&response.status_code)
            || !json_content_type(response.content_type.as_deref())
        {
            return Err(CoreError::new(ErrorCode::SessionReauthorizationRequired));
        }
        let token: TokenResponse = decode_json(&response.body)?;
        validate_token_response(&token, false)?;
        let TokenResponse {
            access_token,
            refresh_token,
            expires_in,
            refresh_token_expires_in,
            ..
        } = token;
        let refresh_token =
            refresh_token.unwrap_or_else(|| session.tokens.refresh_token.expose().to_owned());
        token_set(
            access_token,
            refresh_token,
            expires_in,
            refresh_token_expires_in,
            now,
        )
    }
}

pub struct StandardOAuthRevoker {
    transport: Arc<dyn HttpTransport>,
}

impl StandardOAuthRevoker {
    pub fn new(transport: Arc<dyn HttpTransport>) -> Self {
        Self { transport }
    }
}

impl OAuthRevoker for StandardOAuthRevoker {
    fn revoke(&self, session: &ProtectedOAuthSession) -> CoreResult<()> {
        let body = form_body(&[
            ("token", session.tokens.refresh_token.expose()),
            ("token_type_hint", "refresh_token"),
            ("client_id", &session.metadata.client_id),
        ]);
        let mut request = HttpRequest::post(session.metadata.revocation_endpoint()?, body);
        request.headers = form_headers();
        let response = self.transport.send(request)?;
        if (200..=299).contains(&response.status_code) {
            Ok(())
        } else {
            Err(CoreError::new(ErrorCode::OAuthRevocationFailed))
        }
    }
}

fn endpoint(value: &str) -> CoreResult<Url> {
    let value =
        Url::parse(value).map_err(|_| CoreError::new(ErrorCode::OAuthProtocolUnavailable))?;
    validate_endpoint(&value).map_err(|_| CoreError::new(ErrorCode::OAuthProtocolUnavailable))?;
    Ok(value)
}

fn optional_same_origin_endpoint(value: Option<&str>, issuer: &Url) -> CoreResult<Option<Url>> {
    value
        .map(|value| {
            let endpoint = endpoint(value)?;
            if same_origin(&endpoint, issuer) {
                Ok(endpoint)
            } else {
                Err(CoreError::new(ErrorCode::OAuthProtocolUnavailable))
            }
        })
        .transpose()
}

fn authorization_metadata_urls(issuer: &Url) -> Vec<Url> {
    let path = issuer.path().trim_end_matches('/');
    let mut appended = issuer.clone();
    appended.set_query(None);
    appended.set_fragment(None);
    appended.set_path(&format!("{path}/.well-known/oauth-authorization-server"));
    let mut standard = issuer.clone();
    standard.set_query(None);
    standard.set_fragment(None);
    standard.set_path(&format!("/.well-known/oauth-authorization-server{path}"));
    if appended == standard {
        vec![appended]
    } else {
        vec![appended, standard]
    }
}

fn decode_json<T: DeserializeOwned>(body: &[u8]) -> CoreResult<T> {
    if body.is_empty() || body.len() > 1024 * 1024 {
        return Err(CoreError::new(ErrorCode::ResponseTooLarge));
    }
    serde_json::from_slice(body).map_err(|_| CoreError::new(ErrorCode::OAuthAuthorizationFailed))
}

fn random_urlsafe(byte_count: usize) -> CoreResult<String> {
    let mut bytes = vec![0_u8; byte_count];
    OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

fn form_body(values: &[(&str, &str)]) -> Vec<u8> {
    let mut serializer = Serializer::new(String::new());
    for (name, value) in values {
        serializer.append_pair(name, value);
    }
    serializer.finish().into_bytes()
}

fn form_headers() -> Vec<(&'static str, String)> {
    vec![
        ("Accept", "application/json".to_owned()),
        (
            "Content-Type",
            "application/x-www-form-urlencoded".to_owned(),
        ),
    ]
}

fn valid_lifetime(value: f64) -> bool {
    value.is_finite() && value > 0.0 && value <= 365.0 * 24.0 * 60.0 * 60.0
}

fn validate_token_response(token: &TokenResponse, refresh_required: bool) -> CoreResult<()> {
    if !token.token_type.eq_ignore_ascii_case("Bearer")
        || !valid_lifetime(token.expires_in)
        || token
            .refresh_token_expires_in
            .is_some_and(|value| !valid_lifetime(value))
        || (refresh_required && token.refresh_token.is_none())
    {
        Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed))
    } else {
        Ok(())
    }
}

fn token_set(
    access_token: String,
    refresh_token: String,
    expires_in: f64,
    refresh_token_expires_in: Option<f64>,
    now: i64,
) -> CoreResult<OAuthTokenSet> {
    OAuthTokenSet::new(
        access_token,
        refresh_token,
        now.saturating_add(expires_in as i64),
        refresh_token_expires_in.map(|value| now.saturating_add(value as i64)),
    )
}
