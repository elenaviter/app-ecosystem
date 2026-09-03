use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use rand::rngs::OsRng;
use rand::TryRngCore;
use serde_json::json;
use uuid::Uuid;

use crate::browser::SystemBrowser;
use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::http::FixedHttpTransport;
use crate::keychain::KeychainSessionStore;
use crate::lock::FileSessionLock;
use crate::management::{KdcubeManagementOperations, INSPECT_OPERATION, RELOAD_OPERATION};
use crate::protocol::{HelperCommand, HelperRequest, OperationInvocation, PROTOCOL_VERSION};
use crate::service::{
    Clock, OAuthAuthorizer, OAuthRefresher, OAuthRevoker, PresenceService, SessionStore,
    SystemClock,
};
use crate::session::{
    session_binding_digest, AuthorizedSession, OAuthMetadata, OAuthTokenSet, ProtectedOAuthSession,
    SessionDescriptor, TargetRecord,
};
use crate::validation::ValidatedTarget;

const APPLICATION_ID: &str = "interactive-check@1-0";

#[derive(Clone)]
struct Observation {
    method: String,
    path: String,
    body: Vec<u8>,
    credential_matched: bool,
}

struct ParsedRequest {
    method: String,
    path: String,
    headers: BTreeMap<String, String>,
    body: Vec<u8>,
}

struct LoopbackFixture {
    origin: String,
    observations: Arc<Mutex<Vec<Observation>>>,
    stopping: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl LoopbackFixture {
    fn start(expected_bearer: String) -> CoreResult<Self> {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
        listener
            .set_nonblocking(true)
            .map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
        let address = listener
            .local_addr()
            .map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
        let observations = Arc::new(Mutex::new(Vec::new()));
        let stopping = Arc::new(AtomicBool::new(false));
        let thread_observations = observations.clone();
        let thread_stopping = stopping.clone();
        let thread = thread::spawn(move || {
            while !thread_stopping.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        handle_request(&mut stream, &expected_bearer, &thread_observations);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(_) => break,
                }
            }
        });
        Ok(Self {
            origin: format!("http://{address}"),
            observations,
            stopping,
            thread: Some(thread),
        })
    }

    fn snapshot(&self) -> Vec<Observation> {
        self.observations
            .lock()
            .map_or_else(|_| Vec::new(), |values| values.clone())
    }
}

impl Drop for LoopbackFixture {
    fn drop(&mut self) {
        self.stopping.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

struct UnusedOAuth;

impl OAuthAuthorizer for UnusedOAuth {
    fn authorize(&self, _target: &ValidatedTarget) -> CoreResult<AuthorizedSession> {
        Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed))
    }
}

impl OAuthRefresher for UnusedOAuth {
    fn refresh(&self, _session: &ProtectedOAuthSession) -> CoreResult<OAuthTokenSet> {
        Err(CoreError::new(ErrorCode::OAuthRefreshFailed))
    }
}

impl OAuthRevoker for UnusedOAuth {
    fn revoke(&self, _session: &ProtectedOAuthSession) -> CoreResult<()> {
        Err(CoreError::new(ErrorCode::OAuthRevocationFailed))
    }
}

pub fn run() -> i32 {
    if std::env::args().skip(1).collect::<Vec<_>>() != ["--run"] {
        println!("FAIL: pass --run only during a coordinated user-present window.");
        return 2;
    }
    let bearer = match random_bearer() {
        Ok(value) => value,
        Err(_) => {
            println!("FAIL: secure random generation is unavailable.");
            return 1;
        }
    };
    let fixture = match LoopbackFixture::start(bearer.clone()) {
        Ok(value) => value,
        Err(_) => {
            println!("FAIL: the disposable loopback server could not start.");
            return 1;
        }
    };
    let store = Arc::new(KeychainSessionStore::new());
    let authorized = match disposable_session(&fixture.origin, bearer) {
        Ok(value) => value,
        Err(_) => {
            println!("FAIL: the disposable protected session could not be prepared.");
            return 1;
        }
    };
    let session_id = authorized.session.session_id;
    if let Err(error) = store.create(&authorized) {
        println!(
            "FAIL: protected-session creation returned {}.",
            safe_code(error)
        );
        return 1;
    }
    let exercise = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        exercise_bound_operations(store.clone(), &fixture, session_id)
    }))
    .unwrap_or(false);

    let cleanup_prompt = format!(
        "Delete disposable check session for local-tenant/local-project at {}",
        fixture.origin
    );
    let cleanup = if wait_for_enter("Press Enter, then APPROVE disposable Keychain cleanup: ") {
        match store.remove(session_id, &cleanup_prompt) {
            Ok(true) => true,
            Ok(false) => {
                println!("FAIL: the disposable protected session was not found during cleanup.");
                false
            }
            Err(error) => {
                println!("FAIL: cleanup returned {}.", safe_code(error));
                false
            }
        }
    } else {
        println!("FAIL: cleanup confirmation input was unavailable.");
        false
    };

    if exercise && cleanup {
        println!("PASS: Rust bound execution and disposable-item cleanup succeeded.");
        0
    } else {
        if !cleanup {
            println!("FAIL: disposable Keychain item cleanup was not confirmed.");
        }
        1
    }
}

fn exercise_bound_operations(
    store: Arc<KeychainSessionStore>,
    fixture: &LoopbackFixture,
    session_id: Uuid,
) -> bool {
    let transport = match FixedHttpTransport::new() {
        Ok(value) => Arc::new(value),
        Err(_) => {
            println!("FAIL: the fixed helper transport could not start.");
            return false;
        }
    };
    let clock = Arc::new(SystemClock);
    let service = PresenceService::new(
        env!("KDCUBE_HELPER_VERSION"),
        store,
        Arc::new(UnusedOAuth),
        Arc::new(UnusedOAuth),
        Arc::new(UnusedOAuth),
        Arc::new(KdcubeManagementOperations::new(
            transport,
            Arc::new(SystemBrowser),
        )),
        match FileSessionLock::default_directory() {
            Ok(directory) => Arc::new(FileSessionLock::new(directory, Duration::from_secs(15))),
            Err(_) => {
                println!("FAIL: the session lock could not be prepared.");
                return false;
            }
        },
        clock,
    );

    let inspect = invocation(INSPECT_OPERATION, None);
    if !cancel_without_dispatch(
        &service,
        fixture,
        request(session_id, inspect.clone()),
        "Press Enter, then CANCEL the first system prompt: ",
    ) {
        return false;
    }
    if !wait_for_enter("Press Enter, then APPROVE deployment inspection: ") {
        return false;
    }
    let before = fixture.snapshot().len();
    let response = service.handle(request(session_id, inspect));
    let observations = fixture.snapshot();
    if !response.ok || observations.len() != before + 1 {
        println!("FAIL: approval did not produce exactly one successful request.");
        return false;
    }
    let observed = &observations[before];
    if observed.method != "GET"
        || observed.path != "/api/integrations/management/v1/deployment"
        || !observed.body.is_empty()
        || !observed.credential_matched
    {
        println!("FAIL: deployment inspection did not preserve the bound request.");
        return false;
    }

    let reload = invocation(RELOAD_OPERATION, Some(APPLICATION_ID));
    if !cancel_without_dispatch(
        &service,
        fixture,
        request(session_id, reload.clone()),
        "Press Enter, then CANCEL the changed operation/body prompt: ",
    ) {
        return false;
    }
    if !wait_for_enter("Press Enter, then APPROVE the changed operation/body: ") {
        return false;
    }
    let before = fixture.snapshot().len();
    let response = service.handle(request(session_id, reload));
    let observations = fixture.snapshot();
    if !response.ok || observations.len() != before + 1 {
        println!("FAIL: changed-operation approval did not dispatch exactly once.");
        return false;
    }
    let observed = &observations[before];
    if observed.method != "POST"
        || observed.path
            != "/api/integrations/management/v1/applications/interactive-check%401-0/reload"
        || observed.body != b"{}"
        || !observed.credential_matched
    {
        println!("FAIL: changed operation/body did not preserve the bound request.");
        return false;
    }
    true
}

fn cancel_without_dispatch(
    service: &PresenceService,
    fixture: &LoopbackFixture,
    request: HelperRequest,
    instruction: &str,
) -> bool {
    let before = fixture.snapshot().len();
    if !wait_for_enter(instruction) {
        return false;
    }
    let response = service.handle(request);
    if response.error.as_ref().map(|value| value.code) != Some(ErrorCode::UserPresenceCancelled) {
        println!("FAIL: cancellation returned an unexpected fixed error.");
        return false;
    }
    if fixture.snapshot().len() != before {
        println!("FAIL: cancellation changed the loopback dispatch count.");
        return false;
    }
    true
}

fn invocation(operation_id: &str, application_id: Option<&str>) -> OperationInvocation {
    OperationInvocation {
        operation_id: operation_id.to_owned(),
        invocation_id: Uuid::new_v4().hyphenated().to_string(),
        arguments: application_id
            .map(|value| BTreeMap::from([("application_id".to_owned(), value.to_owned())]))
            .unwrap_or_default(),
    }
}

fn request(session_id: Uuid, operation: OperationInvocation) -> HelperRequest {
    HelperRequest {
        protocol_version: PROTOCOL_VERSION,
        request_id: Uuid::new_v4().hyphenated().to_string(),
        command: HelperCommand::ExecuteOperation,
        session_id: Some(session_id.hyphenated().to_string()),
        target: None,
        operation: Some(operation),
    }
}

fn disposable_session(origin: &str, bearer: String) -> CoreResult<AuthorizedSession> {
    let session_id = Uuid::new_v4();
    let target = TargetRecord {
        normalized_origin: origin.to_owned(),
        tenant: "local-tenant".to_owned(),
        project: "local-project".to_owned(),
        caller_profile: "interactive-human".to_owned(),
        oauth_client_id: None,
    };
    let metadata = OAuthMetadata {
        issuer: format!("{origin}/oauth"),
        authorization_endpoint: format!("{origin}/oauth/authorize"),
        token_endpoint: format!("{origin}/oauth/token"),
        revocation_endpoint: Some(format!("{origin}/oauth/revoke")),
        client_id: "interactive-check".to_owned(),
        redirect_uri: "http://127.0.0.1:1/oauth/callback".to_owned(),
        resource: "urn:kdcube:management:deployment:local-tenant:local-project".to_owned(),
        scopes: Vec::new(),
        authorization_response_issuer_required: false,
    };
    let binding_digest = session_binding_digest(session_id, &target, &metadata);
    let now = SystemClock.now_unix();
    let session = ProtectedOAuthSession {
        schema_version: 1,
        session_id,
        target: target.clone(),
        metadata,
        tokens: OAuthTokenSet::new(
            bearer,
            random_bearer()?,
            now.saturating_add(3600),
            Some(now.saturating_add(7200)),
        )?,
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

fn handle_request(
    stream: &mut TcpStream,
    expected_bearer: &str,
    observations: &Mutex<Vec<Observation>>,
) {
    let Some(request) = read_http_request(stream) else {
        write_http_response(stream, 400, b"{}");
        return;
    };
    let authorization = request
        .headers
        .get("authorization")
        .map(String::as_str)
        .unwrap_or_default();
    let invocation_id = request
        .headers
        .get("idempotency-key")
        .cloned()
        .unwrap_or_default();
    let credential_matched = authorization == format!("Bearer {expected_bearer}");
    if let Ok(mut values) = observations.lock() {
        values.push(Observation {
            method: request.method.clone(),
            path: request.path.clone(),
            body: request.body.clone(),
            credential_matched,
        });
    }
    let (operation, application_id) = if request.method == "GET"
        && request.path == "/api/integrations/management/v1/deployment"
    {
        (INSPECT_OPERATION, None)
    } else if request.method == "POST"
        && request.path
            == "/api/integrations/management/v1/applications/interactive-check%401-0/reload"
        && request.body == b"{}"
    {
        (RELOAD_OPERATION, Some(APPLICATION_ID))
    } else {
        write_http_response(stream, 404, b"{}");
        return;
    };
    let response = serde_json::to_vec(&json!({
        "schema": "kdcube.management.result.v1",
        "ok": true,
        "operation": operation,
        "resource": "urn:kdcube:management:deployment:local-tenant:local-project",
        "target": {"tenant": "local-tenant", "project": "local-project"},
        "invocation": {"id": invocation_id},
        "authority": {
            "card_revision": 1,
            "active_catalog_version": "interactive-catalog",
            "invocation_policy_revision": 1
        },
        "result": {
            "application_id": application_id,
            "state": "completed",
            "generation": "interactive-generation"
        }
    }))
    .unwrap_or_else(|_| b"{}".to_vec());
    write_http_response(stream, 200, &response);
}

fn read_http_request(stream: &mut TcpStream) -> Option<ParsedRequest> {
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    let mut value = Vec::new();
    let mut buffer = [0_u8; 4096];
    let (headers_end, content_length) = loop {
        if value.len() > 64 * 1024 {
            return None;
        }
        let count = stream.read(&mut buffer).ok()?;
        if count == 0 {
            return None;
        }
        value.extend_from_slice(&buffer[..count]);
        if let Some(position) = value.windows(4).position(|part| part == b"\r\n\r\n") {
            let text = std::str::from_utf8(&value[..position]).ok()?;
            let length = text
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().ok())
                        .flatten()
                })
                .unwrap_or(0);
            if length > 1024 * 1024 {
                return None;
            }
            break (position, length);
        }
    };
    while value.len() < headers_end + 4 + content_length {
        let count = stream.read(&mut buffer).ok()?;
        if count == 0 {
            return None;
        }
        value.extend_from_slice(&buffer[..count]);
    }
    let text = std::str::from_utf8(&value[..headers_end]).ok()?;
    let mut lines = text.lines();
    let mut request_line = lines.next()?.split_ascii_whitespace();
    let method = request_line.next()?.to_owned();
    let path = request_line.next()?.to_owned();
    if request_line.next()? != "HTTP/1.1" || request_line.next().is_some() {
        return None;
    }
    let mut headers = BTreeMap::new();
    for line in lines {
        let (name, value) = line.split_once(':')?;
        let name = name.trim().to_ascii_lowercase();
        if headers.insert(name, value.trim().to_owned()).is_some() {
            return None;
        }
    }
    let body = value[headers_end + 4..headers_end + 4 + content_length].to_vec();
    Some(ParsedRequest {
        method,
        path,
        headers,
        body,
    })
}

fn write_http_response(stream: &mut TcpStream, status: u16, body: &[u8]) {
    let reason = if status == 200 { "OK" } else { "Rejected" };
    let headers = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(headers.as_bytes());
    let _ = stream.write_all(body);
}

fn wait_for_enter(instruction: &str) -> bool {
    print!("{instruction}");
    if std::io::stdout().flush().is_err() {
        return false;
    }
    let mut line = String::new();
    std::io::stdin().read_line(&mut line).is_ok()
}

fn random_bearer() -> CoreResult<String> {
    let mut bytes = [0_u8; 48];
    OsRng
        .try_fill_bytes(&mut bytes)
        .map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

fn safe_code(error: CoreError) -> &'static str {
    match error.code {
        ErrorCode::InvalidRequest => "invalid_request",
        ErrorCode::RequestTooLarge => "request_too_large",
        ErrorCode::UnsupportedProtocol => "unsupported_protocol",
        ErrorCode::UnsupportedCommand => "unsupported_command",
        ErrorCode::SessionNotFound => "session_not_found",
        ErrorCode::SessionBusy => "session_busy",
        ErrorCode::SessionReauthorizationRequired => "session_reauthorization_required",
        ErrorCode::UserPresenceCancelled => "user_presence_cancelled",
        ErrorCode::UserPresenceUnavailable => "user_presence_unavailable",
        ErrorCode::HelperSigningInvalid => "helper_signing_invalid",
        ErrorCode::OAuthProtocolUnavailable => "oauth_protocol_unavailable",
        ErrorCode::OAuthAuthorizationFailed => "oauth_authorization_failed",
        ErrorCode::OAuthRefreshFailed => "oauth_refresh_failed",
        ErrorCode::OAuthRevocationFailed => "oauth_revocation_failed",
        ErrorCode::OperationNotSupported => "operation_not_supported",
        ErrorCode::OperationApprovalRequired => "operation_approval_required",
        ErrorCode::OperationDenied => "operation_denied",
        ErrorCode::OperationFailed => "operation_failed",
        ErrorCode::ResponseTooLarge => "response_too_large",
        ErrorCode::InternalFailure => "internal_failure",
    }
}
