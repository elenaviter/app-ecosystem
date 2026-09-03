use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use std::time::Duration;

use serde::Deserialize;
use url::Url;

use crate::browser::Browser;
use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::http::{json_content_type, HttpRequest, HttpResponse, HttpTransport};
use crate::protocol::{SafeOperationEvidence, MAX_RESPONSE_BYTES};
use crate::service::OperationRegistry;
use crate::session::{hex_digest, ProtectedOAuthSession, TargetRecord};
use crate::validation::{application_id, same_origin, ValidatedInvocation};

pub const INSPECT_OPERATION: &str = "kdcube.management.deployment.inspect";
pub const SURFACES_OPERATION: &str = "kdcube.management.application.surfaces.read";
pub const RELOAD_OPERATION: &str = "kdcube.management.application.reload";

struct PreparedOperation {
    operation_id: &'static str,
    application_id: Option<String>,
    method: &'static str,
    path: String,
    body: Vec<u8>,
    prompt_verb: &'static str,
}

#[derive(Deserialize)]
struct ManagementTarget {
    tenant: String,
    project: String,
}

#[derive(Deserialize)]
struct ManagementInvocation {
    id: String,
}

#[derive(Default, Deserialize)]
struct ManagementAuthority {
    card_revision: Option<u64>,
    card_catalog_version: Option<String>,
    active_catalog_version: Option<String>,
    invocation_policy_revision: Option<u64>,
}

#[derive(Default, Deserialize)]
struct ManagementResult {
    application_id: Option<String>,
    state: Option<String>,
    generation: Option<String>,
}

#[derive(Deserialize)]
struct ManagementEnvelope {
    schema: String,
    ok: bool,
    operation: String,
    resource: String,
    target: ManagementTarget,
    invocation: ManagementInvocation,
    #[serde(default)]
    authority: ManagementAuthority,
    #[serde(default)]
    result: ManagementResult,
}

#[derive(Deserialize)]
struct ManagementErrorDetail {
    code: String,
    retryable: bool,
}

#[derive(Deserialize)]
struct ManagementRecovery {
    #[serde(rename = "type")]
    kind: String,
    reason: String,
    authorization_url: String,
    access_id: String,
    resource: String,
    operation: String,
    application_id: Option<String>,
    invocation_id: String,
    request_digest: String,
    card_revision: u64,
    catalog_version: String,
    expires_at: Option<i64>,
    permit_ttl_seconds: Option<u64>,
    choices: Vec<String>,
}

#[derive(Deserialize)]
struct ManagementErrorEnvelope {
    schema: String,
    ok: bool,
    operation: String,
    resource: String,
    target: ManagementTarget,
    invocation_id: String,
    error: ManagementErrorDetail,
    recovery: Option<ManagementRecovery>,
}

pub struct KdcubeManagementOperations {
    transport: Arc<dyn HttpTransport>,
    browser: Arc<dyn Browser>,
}

impl KdcubeManagementOperations {
    pub fn new(transport: Arc<dyn HttpTransport>, browser: Arc<dyn Browser>) -> Self {
        Self { transport, browser }
    }

    fn prepare(&self, invocation: &ValidatedInvocation) -> CoreResult<PreparedOperation> {
        match invocation.operation_id.as_str() {
            INSPECT_OPERATION if invocation.arguments.is_empty() => Ok(PreparedOperation {
                operation_id: INSPECT_OPERATION,
                application_id: None,
                method: "GET",
                path: "/api/integrations/management/v1/deployment".to_owned(),
                body: Vec::new(),
                prompt_verb: "Inspect deployment",
            }),
            SURFACES_OPERATION | RELOAD_OPERATION if invocation.arguments.len() == 1 => {
                let raw = invocation
                    .arguments
                    .get("application_id")
                    .ok_or_else(|| CoreError::new(ErrorCode::InvalidRequest))?;
                let application = application_id(raw)?.to_owned();
                let segment = percent_encoded_segment(&application);
                if invocation.operation_id == SURFACES_OPERATION {
                    Ok(PreparedOperation {
                        operation_id: SURFACES_OPERATION,
                        application_id: Some(application),
                        method: "GET",
                        path: format!(
                            "/api/integrations/management/v1/applications/{segment}/surfaces"
                        ),
                        body: Vec::new(),
                        prompt_verb: "Read application surfaces for",
                    })
                } else {
                    Ok(PreparedOperation {
                        operation_id: RELOAD_OPERATION,
                        application_id: Some(application),
                        method: "POST",
                        path: format!(
                            "/api/integrations/management/v1/applications/{segment}/reload"
                        ),
                        body: b"{}".to_vec(),
                        prompt_verb: "Reload application",
                    })
                }
            }
            INSPECT_OPERATION | SURFACES_OPERATION | RELOAD_OPERATION => {
                Err(CoreError::new(ErrorCode::InvalidRequest))
            }
            _ => Err(CoreError::new(ErrorCode::OperationNotSupported)),
        }
    }

    fn approval_url(
        &self,
        response: &HttpResponse,
        session: &ProtectedOAuthSession,
        invocation: &ValidatedInvocation,
        prepared: &PreparedOperation,
    ) -> CoreResult<Option<Url>> {
        if !json_content_type(response.content_type.as_deref()) {
            return Ok(None);
        }
        let Ok(envelope) = serde_json::from_slice::<ManagementErrorEnvelope>(&response.body) else {
            return Ok(None);
        };
        if envelope.error.code != "delegated_request_permit_required" {
            return Ok(None);
        }
        let invocation_id = invocation.invocation_id.hyphenated().to_string();
        let request_digest = management_request_digest(
            prepared.operation_id,
            &session.metadata.resource,
            prepared.application_id.as_deref().unwrap_or_default(),
        );
        let recovery = envelope
            .recovery
            .ok_or_else(|| CoreError::new(ErrorCode::OperationFailed))?;
        let choices: BTreeSet<&str> = recovery.choices.iter().map(String::as_str).collect();
        if envelope.schema != "kdcube.management.error.v1"
            || envelope.ok
            || envelope.operation != prepared.operation_id
            || envelope.resource != session.metadata.resource
            || envelope.target.tenant != session.target.tenant
            || envelope.target.project != session.target.project
            || !envelope.invocation_id.eq_ignore_ascii_case(&invocation_id)
            || envelope.error.retryable
            || recovery.kind != "consent_required"
            || recovery.reason != "delegated_request_permit_required"
            || recovery.access_id.is_empty()
            || recovery.access_id.len() > 256
            || recovery.resource != session.metadata.resource
            || recovery.operation != prepared.operation_id
            || recovery.application_id != prepared.application_id
            || !recovery.invocation_id.eq_ignore_ascii_case(&invocation_id)
            || recovery.request_digest != request_digest
            || recovery.card_revision == 0
            || recovery.catalog_version.is_empty()
            || recovery.catalog_version.len() > 256
            || recovery.choices.len() != 2
            || choices != BTreeSet::from(["allow_always", "allow_once"])
            || !valid_recovery_lifetime(&recovery)
        {
            return Err(CoreError::new(ErrorCode::OperationFailed));
        }
        let approval_url = Url::parse(&recovery.authorization_url)
            .map_err(|_| CoreError::new(ErrorCode::OperationFailed))?;
        if !valid_approval_url(&approval_url, session, prepared, &invocation_id, &recovery)
            || contains_protected_material(approval_url.as_str(), session)
        {
            return Err(CoreError::new(ErrorCode::OperationFailed));
        }
        Ok(Some(approval_url))
    }
}

impl OperationRegistry for KdcubeManagementOperations {
    fn operation_ids(&self) -> Vec<&'static str> {
        vec![INSPECT_OPERATION, RELOAD_OPERATION, SURFACES_OPERATION]
    }

    fn prompt(
        &self,
        target: &TargetRecord,
        invocation: &ValidatedInvocation,
    ) -> CoreResult<String> {
        let prepared = self.prepare(invocation)?;
        let application = prepared
            .application_id
            .as_deref()
            .map(|value| format!(" {value}"))
            .unwrap_or_default();
        Ok(format!(
            "{}{application} for {}/{} at {}",
            prepared.prompt_verb, target.tenant, target.project, target.normalized_origin
        ))
    }

    fn execute(
        &self,
        session: &ProtectedOAuthSession,
        invocation: &ValidatedInvocation,
    ) -> CoreResult<SafeOperationEvidence> {
        let prepared = self.prepare(invocation)?;
        let origin = Url::parse(&session.target.normalized_origin)
            .map_err(|_| CoreError::new(ErrorCode::OperationFailed))?;
        let endpoint = origin
            .join(&prepared.path)
            .map_err(|_| CoreError::new(ErrorCode::OperationFailed))?;
        let mut request = if prepared.method == "GET" {
            HttpRequest::get(endpoint)
        } else {
            HttpRequest::post(endpoint, prepared.body.clone())
        };
        request.maximum_response_bytes = MAX_RESPONSE_BYTES;
        request.timeout = Duration::from_secs(30);
        request.headers = vec![
            ("Accept", "application/json".to_owned()),
            (
                "Authorization",
                format!("Bearer {}", session.tokens.access_token.expose()),
            ),
            (
                "Idempotency-Key",
                invocation.invocation_id.hyphenated().to_string(),
            ),
        ];
        if !prepared.body.is_empty() {
            request
                .headers
                .push(("Content-Type", "application/json".to_owned()));
        }
        let response = self.transport.send(request)?;
        match response.status_code {
            200..=299 => {}
            401 => return Err(CoreError::new(ErrorCode::SessionReauthorizationRequired)),
            403 => {
                if let Some(approval_url) =
                    self.approval_url(&response, session, invocation, &prepared)?
                {
                    if !self.browser.open(&approval_url) {
                        return Err(CoreError::retryable(ErrorCode::OperationFailed));
                    }
                    return Err(CoreError::retryable(ErrorCode::OperationApprovalRequired));
                }
                return Err(CoreError::new(ErrorCode::OperationDenied));
            }
            404 => return Err(CoreError::new(ErrorCode::OperationDenied)),
            409 => return Err(CoreError::retryable(ErrorCode::OperationDenied)),
            500..=599 => return Err(CoreError::retryable(ErrorCode::OperationFailed)),
            _ => return Err(CoreError::new(ErrorCode::OperationFailed)),
        }
        if !json_content_type(response.content_type.as_deref()) {
            return Err(CoreError::new(ErrorCode::OperationFailed));
        }
        let envelope: ManagementEnvelope = serde_json::from_slice(&response.body)
            .map_err(|_| CoreError::new(ErrorCode::OperationFailed))?;
        let invocation_id = invocation.invocation_id.hyphenated().to_string();
        if envelope.schema != "kdcube.management.result.v1"
            || !envelope.ok
            || envelope.operation != prepared.operation_id
            || envelope.resource != session.metadata.resource
            || envelope.target.tenant != session.target.tenant
            || envelope.target.project != session.target.project
            || !envelope.invocation.id.eq_ignore_ascii_case(&invocation_id)
            || envelope.result.application_id != prepared.application_id
        {
            return Err(CoreError::new(ErrorCode::OperationFailed));
        }
        let outcome = validated_outcome(envelope.result.state.as_deref().unwrap_or("completed"))?;
        let generation = bounded_evidence(envelope.result.generation);
        let catalog_revision = bounded_evidence(
            envelope
                .authority
                .active_catalog_version
                .or(envelope.authority.card_catalog_version),
        );
        let values = [
            Some(outcome.as_str()),
            generation.as_deref(),
            catalog_revision.as_deref(),
        ];
        if values
            .into_iter()
            .flatten()
            .any(|value| contains_protected_material(value, session))
        {
            return Err(CoreError::new(ErrorCode::OperationFailed));
        }
        Ok(SafeOperationEvidence {
            operation_id: prepared.operation_id.to_owned(),
            invocation_id,
            outcome,
            application_id: prepared.application_id,
            generation,
            card_revision: envelope
                .authority
                .card_revision
                .map(|value| value.to_string()),
            catalog_revision,
            policy_revision: envelope
                .authority
                .invocation_policy_revision
                .map(|value| value.to_string()),
        })
    }
}

pub fn management_resource(target: &TargetRecord) -> String {
    format!(
        "urn:kdcube:management:deployment:{}:{}",
        percent_encoded_segment(&target.tenant),
        percent_encoded_segment(&target.project)
    )
}

pub fn management_request_digest(operation: &str, resource: &str, application_id: &str) -> String {
    let document = format!(
        "{{\"application_id\":{},\"body\":{{}},\"operation\":{},\"resource\":{},\"schema\":\"kdcube.management.request.v1\"}}",
        python_ascii_json_string(application_id),
        python_ascii_json_string(operation),
        python_ascii_json_string(resource),
    );
    hex_digest(document.as_bytes())
}

fn valid_recovery_lifetime(recovery: &ManagementRecovery) -> bool {
    recovery.expires_at.is_some_and(|value| value > 0)
        || recovery
            .permit_ttl_seconds
            .is_some_and(|value| (1..=600).contains(&value))
}

fn valid_approval_url(
    approval_url: &Url,
    session: &ProtectedOAuthSession,
    prepared: &PreparedOperation,
    invocation_id: &str,
    recovery: &ManagementRecovery,
) -> bool {
    let Ok(origin) = Url::parse(&session.target.normalized_origin) else {
        return false;
    };
    let expected_segments = vec![
        "api".to_owned(),
        "integrations".to_owned(),
        "bundles".to_owned(),
        session.target.tenant.clone(),
        session.target.project.clone(),
        "connection-hub@1-0".to_owned(),
        "widgets".to_owned(),
        "connections_settings".to_owned(),
    ];
    let segments: Option<Vec<String>> = approval_url.path_segments().and_then(|values| {
        values
            .filter(|value| !value.is_empty())
            .map(|value| {
                percent_encoding::percent_decode_str(value)
                    .decode_utf8()
                    .map(|value| value.into_owned())
            })
            .collect::<Result<Vec<_>, _>>()
            .ok()
    });
    if approval_url.as_str().len() > 16 * 1024
        || approval_url.username() != ""
        || approval_url.password().is_some()
        || approval_url.fragment().is_some()
        || !same_origin(approval_url, &origin)
        || segments.as_ref() != Some(&expected_segments)
    {
        return false;
    }
    let mut query: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (name, value) in approval_url.query_pairs() {
        query
            .entry(name.into_owned())
            .or_default()
            .push(value.into_owned());
    }
    let mut allowed_names = BTreeSet::from([
        "tab",
        "resource",
        "outer_operation",
        "invocation_policy",
        "invocation_change_id",
        "request_bound",
        "request_digest",
        "request_card_revision",
        "request_authority_revision",
        "request_approval_ticket",
    ]);
    if query.contains_key("access_id") {
        allowed_names.insert("access_id");
    } else {
        allowed_names.insert("manual_access_id");
    }
    if prepared.application_id.is_some() {
        allowed_names.insert("approval_application_id");
    }
    query.len() == allowed_names.len()
        && query
            .keys()
            .all(|name| allowed_names.contains(name.as_str()))
        && exact(&query, "tab", "delegated_by_kdcube")
        && exact(&query, "resource", &recovery.resource)
        && exact(&query, "outer_operation", prepared.operation_id)
        && exact(&query, "invocation_policy", "choose")
        && exact(&query, "invocation_change_id", invocation_id)
        && exact(&query, "request_bound", "1")
        && exact(&query, "request_digest", &recovery.request_digest)
        && exact(
            &query,
            "request_card_revision",
            &recovery.card_revision.to_string(),
        )
        && exact(
            &query,
            "request_authority_revision",
            &recovery.catalog_version,
        )
        && one_nonempty(&query, "request_approval_ticket")
        && access_id_matches(&query, &recovery.access_id)
        && prepared
            .application_id
            .as_ref()
            .is_none_or(|application| exact(&query, "approval_application_id", application))
}

fn percent_encoded_segment(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            output.push(char::from(*byte));
        } else {
            output.push_str(&format!("%{byte:02X}"));
        }
    }
    output
}

fn exact(query: &BTreeMap<String, Vec<String>>, name: &str, expected: &str) -> bool {
    query
        .get(name)
        .is_some_and(|values| values.len() == 1 && values[0] == expected)
}

fn one_nonempty(query: &BTreeMap<String, Vec<String>>, name: &str) -> bool {
    query.get(name).is_some_and(|values| {
        values.len() == 1 && !values[0].is_empty() && values[0].len() <= 16 * 1024
    })
}

fn access_id_matches(query: &BTreeMap<String, Vec<String>>, expected: &str) -> bool {
    let direct = query.get("access_id");
    let manual = query.get("manual_access_id");
    (direct.is_some() != manual.is_some())
        && (direct.is_some_and(|value| value.len() == 1 && value[0] == expected)
            || manual.is_some_and(|value| value.len() == 1 && value[0] == expected))
}

fn validated_outcome(value: &str) -> CoreResult<String> {
    if value.is_empty()
        || value.len() > 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        Err(CoreError::new(ErrorCode::OperationFailed))
    } else {
        Ok(value.to_owned())
    }
}

fn bounded_evidence(value: Option<String>) -> Option<String> {
    value.filter(|value| {
        !value.is_empty() && value.len() <= 256 && !value.chars().any(char::is_control)
    })
}

fn contains_protected_material(value: &str, session: &ProtectedOAuthSession) -> bool {
    value.contains(session.tokens.access_token.expose())
        || value.contains(session.tokens.refresh_token.expose())
}

fn python_ascii_json_string(value: &str) -> String {
    let mut output = String::from("\"");
    for character in value.chars() {
        match character {
            '\u{08}' => output.push_str("\\b"),
            '\t' => output.push_str("\\t"),
            '\n' => output.push_str("\\n"),
            '\u{0c}' => output.push_str("\\f"),
            '\r' => output.push_str("\\r"),
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            value if value <= '\u{1f}' => output.push_str(&format!("\\u{:04x}", value as u32)),
            value if value <= '\u{7e}' => output.push(value),
            value if value <= '\u{ffff}' => {
                output.push_str(&format!("\\u{:04x}", value as u32));
            }
            value => {
                let scalar = value as u32 - 0x1_0000;
                let high = 0xd800 + (scalar >> 10);
                let low = 0xdc00 + (scalar & 0x3ff);
                output.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
            }
        }
    }
    output.push('"');
    output
}
