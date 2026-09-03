use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use url::Url;
use uuid::Uuid;
use zeroize::Zeroize;

use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::validation::ValidatedTarget;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct TargetRecord {
    pub normalized_origin: String,
    pub tenant: String,
    pub project: String,
    pub caller_profile: String,
    pub oauth_client_id: Option<String>,
}

impl From<&ValidatedTarget> for TargetRecord {
    fn from(value: &ValidatedTarget) -> Self {
        Self {
            normalized_origin: value.normalized_origin.clone(),
            tenant: value.tenant.clone(),
            project: value.project.clone(),
            caller_profile: value.caller_profile.clone(),
            oauth_client_id: value.oauth_client_id.clone(),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct OAuthMetadata {
    pub issuer: String,
    pub authorization_endpoint: String,
    pub token_endpoint: String,
    pub revocation_endpoint: Option<String>,
    pub client_id: String,
    pub redirect_uri: String,
    pub resource: String,
    pub scopes: Vec<String>,
    pub authorization_response_issuer_required: bool,
}

impl OAuthMetadata {
    pub fn token_endpoint(&self) -> CoreResult<Url> {
        Url::parse(&self.token_endpoint)
            .map_err(|_| CoreError::new(ErrorCode::SessionReauthorizationRequired))
    }

    pub fn revocation_endpoint(&self) -> CoreResult<Url> {
        self.revocation_endpoint
            .as_deref()
            .and_then(|value| Url::parse(value).ok())
            .ok_or_else(|| CoreError::new(ErrorCode::OAuthRevocationFailed))
    }
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize)]
#[serde(transparent)]
pub struct SecretString(String);

impl SecretString {
    pub fn new(value: String) -> CoreResult<Self> {
        if value.is_empty()
            || value.len() > 16 * 1024
            || value.chars().any(char::is_whitespace)
            || value.chars().any(char::is_control)
        {
            Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed))
        } else {
            Ok(Self(value))
        }
    }

    pub(crate) fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for SecretString {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("<redacted>")
    }
}

impl Drop for SecretString {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize)]
pub struct OAuthTokenSet {
    pub(crate) access_token: SecretString,
    pub(crate) refresh_token: SecretString,
    pub access_expires_at: i64,
    pub refresh_expires_at: Option<i64>,
}

impl std::fmt::Debug for OAuthTokenSet {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("<OAuthTokenSet redacted>")
    }
}

impl OAuthTokenSet {
    pub fn new(
        access_token: String,
        refresh_token: String,
        access_expires_at: i64,
        refresh_expires_at: Option<i64>,
    ) -> CoreResult<Self> {
        Ok(Self {
            access_token: SecretString::new(access_token)?,
            refresh_token: SecretString::new(refresh_token)?,
            access_expires_at,
            refresh_expires_at,
        })
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct SessionDescriptor {
    pub session_id: Uuid,
    pub target: TargetRecord,
    pub binding_digest: String,
    #[serde(default)]
    pub reauthorization_required: bool,
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize)]
pub struct ProtectedOAuthSession {
    pub schema_version: u32,
    pub session_id: Uuid,
    pub target: TargetRecord,
    pub metadata: OAuthMetadata,
    pub(crate) tokens: OAuthTokenSet,
    pub generation: u64,
    pub binding_digest: String,
}

impl std::fmt::Debug for ProtectedOAuthSession {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("<ProtectedOAuthSession redacted>")
    }
}

impl ProtectedOAuthSession {
    pub fn replacing(&self, tokens: OAuthTokenSet) -> Self {
        Self {
            schema_version: self.schema_version,
            session_id: self.session_id,
            target: self.target.clone(),
            metadata: self.metadata.clone(),
            tokens,
            generation: self.generation + 1,
            binding_digest: self.binding_digest.clone(),
        }
    }
}

pub struct AuthorizedSession {
    pub descriptor: SessionDescriptor,
    pub session: ProtectedOAuthSession,
}

pub fn session_binding_digest(
    session_id: Uuid,
    target: &TargetRecord,
    metadata: &OAuthMetadata,
) -> String {
    let mut scopes = metadata.scopes.clone();
    scopes.sort();
    let fields = [
        format!("session_id={session_id}"),
        format!("origin={}", target.normalized_origin),
        format!("tenant={}", target.tenant),
        format!("project={}", target.project),
        format!("caller_profile={}", target.caller_profile),
        format!(
            "configured_oauth_client_id={}",
            target.oauth_client_id.as_deref().unwrap_or_default()
        ),
        format!("issuer={}", metadata.issuer),
        format!("authorization_endpoint={}", metadata.authorization_endpoint),
        format!("token_endpoint={}", metadata.token_endpoint),
        format!(
            "revocation_endpoint={}",
            metadata.revocation_endpoint.as_deref().unwrap_or_default()
        ),
        format!("client_id={}", metadata.client_id),
        format!("redirect_uri={}", metadata.redirect_uri),
        format!("resource={}", metadata.resource),
        format!("scopes={}", scopes.join(" ")),
        format!(
            "authorization_response_issuer_required={}",
            metadata.authorization_response_issuer_required
        ),
    ];
    hex_digest(fields.join("\n").as_bytes())
}

pub fn hex_digest(value: &[u8]) -> String {
    Sha256::digest(value)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
