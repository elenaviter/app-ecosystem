use std::collections::VecDeque;
use std::sync::atomic::{AtomicI64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use url::Url;
use uuid::Uuid;

use crate::browser::Browser;
use crate::callback::{AuthorizationCallback, AuthorizationCallbackFactory};
use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::http::{HttpRequest, HttpResponse, HttpTransport};
use crate::lock::SessionLock;
use crate::protocol::SafeOperationEvidence;
use crate::service::{
    Clock, OAuthAuthorizer, OAuthRefresher, OAuthRevoker, OperationRegistry, SessionStore,
};
use crate::session::{
    session_binding_digest, AuthorizedSession, OAuthMetadata, OAuthTokenSet, ProtectedOAuthSession,
    SessionDescriptor, TargetRecord,
};
use crate::validation::{ValidatedInvocation, ValidatedTarget};

pub const ACCESS_MARKER: &str = "ACCESS-MARKER-MUST-NEVER-CROSS-IPC";
pub const REFRESH_MARKER: &str = "REFRESH-MARKER-MUST-NEVER-CROSS-IPC";
pub const ROTATED_ACCESS_MARKER: &str = "ROTATED-ACCESS-MARKER-MUST-NEVER-CROSS-IPC";
pub const ROTATED_REFRESH_MARKER: &str = "ROTATED-REFRESH-MARKER-MUST-NEVER-CROSS-IPC";
pub const ERROR_MARKER: &str = "ERROR-MARKER-MUST-NEVER-CROSS-IPC";
pub const NOW: i64 = 1_800_000_000;

pub fn validated_target() -> ValidatedTarget {
    ValidatedTarget {
        normalized_origin: "https://target.example:443".to_owned(),
        tenant: "tenant-a".to_owned(),
        project: "project-a".to_owned(),
        caller_profile: "human-admin".to_owned(),
        oauth_client_id: None,
    }
}

pub fn metadata() -> OAuthMetadata {
    OAuthMetadata {
        issuer: "https://target.example:443/oauth".to_owned(),
        authorization_endpoint: "https://target.example:443/oauth/authorize".to_owned(),
        token_endpoint: "https://target.example:443/oauth/token".to_owned(),
        revocation_endpoint: Some("https://target.example:443/oauth/revoke".to_owned()),
        client_id: "connection-hub-presence-helper".to_owned(),
        redirect_uri: "http://127.0.0.1:48191/oauth/callback".to_owned(),
        resource: "urn:kdcube:management:deployment:tenant-a:project-a".to_owned(),
        scopes: Vec::new(),
        authorization_response_issuer_required: true,
    }
}

pub fn authorized_session(expired: bool) -> AuthorizedSession {
    let session_id = Uuid::parse_str("00000000-0000-4000-8000-000000000101").unwrap();
    let target = TargetRecord::from(&validated_target());
    let metadata = metadata();
    let binding_digest = session_binding_digest(session_id, &target, &metadata);
    let session = ProtectedOAuthSession {
        schema_version: 1,
        session_id,
        target: target.clone(),
        metadata,
        tokens: OAuthTokenSet::new(
            ACCESS_MARKER.to_owned(),
            REFRESH_MARKER.to_owned(),
            if expired { NOW - 1 } else { NOW + 3600 },
            Some(NOW + 7200),
        )
        .unwrap(),
        generation: 1,
        binding_digest: binding_digest.clone(),
    };
    AuthorizedSession {
        descriptor: SessionDescriptor {
            session_id,
            target,
            binding_digest,
            reauthorization_required: false,
        },
        session,
    }
}

pub fn rotated_tokens() -> OAuthTokenSet {
    OAuthTokenSet::new(
        ROTATED_ACCESS_MARKER.to_owned(),
        ROTATED_REFRESH_MARKER.to_owned(),
        NOW + 3600,
        Some(NOW + 7200),
    )
    .unwrap()
}

pub struct FixedClock {
    now: AtomicI64,
}

impl FixedClock {
    pub fn new(now: i64) -> Self {
        Self {
            now: AtomicI64::new(now),
        }
    }
}

impl Clock for FixedClock {
    fn now_unix(&self) -> i64 {
        self.now.load(Ordering::SeqCst)
    }
}

#[derive(Default)]
struct StoreState {
    descriptor: Option<SessionDescriptor>,
    session: Option<ProtectedOAuthSession>,
    events: Vec<&'static str>,
    read_error: Option<CoreError>,
    replace_error: Option<CoreError>,
}

pub struct FakeStore {
    state: Mutex<StoreState>,
    reads: AtomicUsize,
    replacements: AtomicUsize,
    reauthorizations: AtomicUsize,
    removals: AtomicUsize,
    lists: AtomicUsize,
}

impl FakeStore {
    pub fn empty() -> Self {
        Self {
            state: Mutex::new(StoreState::default()),
            reads: AtomicUsize::new(0),
            replacements: AtomicUsize::new(0),
            reauthorizations: AtomicUsize::new(0),
            removals: AtomicUsize::new(0),
            lists: AtomicUsize::new(0),
        }
    }

    pub fn with_session(value: AuthorizedSession) -> Self {
        let store = Self::empty();
        {
            let mut state = store.state.lock().unwrap();
            state.descriptor = Some(value.descriptor);
            state.session = Some(value.session);
        }
        store
    }

    pub fn set_read_error(&self, error: CoreError) {
        self.state.lock().unwrap().read_error = Some(error);
    }

    pub fn set_replace_error(&self, error: CoreError) {
        self.state.lock().unwrap().replace_error = Some(error);
    }

    pub fn corrupt_binding(&self) {
        if let Some(session) = self.state.lock().unwrap().session.as_mut() {
            session.binding_digest = "different-binding".to_owned();
        }
    }

    pub fn reads(&self) -> usize {
        self.reads.load(Ordering::SeqCst)
    }

    pub fn replacements(&self) -> usize {
        self.replacements.load(Ordering::SeqCst)
    }

    pub fn reauthorizations(&self) -> usize {
        self.reauthorizations.load(Ordering::SeqCst)
    }

    pub fn removals(&self) -> usize {
        self.removals.load(Ordering::SeqCst)
    }

    pub fn lists(&self) -> usize {
        self.lists.load(Ordering::SeqCst)
    }

    pub fn events(&self) -> Vec<&'static str> {
        self.state.lock().unwrap().events.clone()
    }

    pub fn has_session(&self) -> bool {
        self.state.lock().unwrap().session.is_some()
    }
}

impl SessionStore for FakeStore {
    fn create(&self, value: &AuthorizedSession) -> CoreResult<()> {
        let mut state = self.state.lock().unwrap();
        if state.descriptor.is_some() || state.session.is_some() {
            return Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed));
        }
        state.descriptor = Some(value.descriptor.clone());
        state.session = Some(value.session.clone());
        state.events.push("create");
        Ok(())
    }

    fn list_session_ids(&self) -> CoreResult<Vec<Uuid>> {
        self.lists.fetch_add(1, Ordering::SeqCst);
        let mut state = self.state.lock().unwrap();
        state.events.push("list");
        Ok(state
            .descriptor
            .as_ref()
            .map(|value| vec![value.session_id])
            .unwrap_or_default())
    }

    fn describe(&self, session_id: Uuid) -> CoreResult<SessionDescriptor> {
        self.state
            .lock()
            .unwrap()
            .descriptor
            .as_ref()
            .filter(|value| value.session_id == session_id)
            .cloned()
            .ok_or_else(|| CoreError::new(ErrorCode::SessionNotFound))
    }

    fn read(&self, session_id: Uuid, prompt: &str) -> CoreResult<ProtectedOAuthSession> {
        self.reads.fetch_add(1, Ordering::SeqCst);
        let mut state = self.state.lock().unwrap();
        state.events.push("read");
        if !prompt.contains("tenant-a/project-a") || !prompt.contains("https://target.example:443")
        {
            return Err(CoreError::new(ErrorCode::InvalidRequest));
        }
        if let Some(error) = state.read_error {
            return Err(error);
        }
        state
            .session
            .as_ref()
            .filter(|value| value.session_id == session_id)
            .cloned()
            .ok_or_else(|| CoreError::new(ErrorCode::SessionNotFound))
    }

    fn replace(
        &self,
        session_id: Uuid,
        expected_generation: u64,
        value: &ProtectedOAuthSession,
    ) -> CoreResult<()> {
        self.replacements.fetch_add(1, Ordering::SeqCst);
        let mut state = self.state.lock().unwrap();
        state.events.push("replace");
        if let Some(error) = state.replace_error {
            return Err(error);
        }
        let current = state
            .session
            .as_ref()
            .ok_or_else(|| CoreError::new(ErrorCode::SessionNotFound))?;
        if current.session_id != session_id
            || current.generation != expected_generation
            || value.generation != expected_generation + 1
        {
            return Err(CoreError::new(ErrorCode::SessionReauthorizationRequired));
        }
        state.session = Some(value.clone());
        Ok(())
    }

    fn require_reauthorization(&self, session_id: Uuid) -> CoreResult<()> {
        self.reauthorizations.fetch_add(1, Ordering::SeqCst);
        let mut state = self.state.lock().unwrap();
        state.events.push("reauthorize");
        let descriptor = state
            .descriptor
            .as_mut()
            .filter(|value| value.session_id == session_id)
            .ok_or_else(|| CoreError::new(ErrorCode::SessionNotFound))?;
        descriptor.reauthorization_required = true;
        Ok(())
    }

    fn remove(&self, session_id: Uuid, prompt: &str) -> CoreResult<bool> {
        self.removals.fetch_add(1, Ordering::SeqCst);
        let mut state = self.state.lock().unwrap();
        state.events.push("remove");
        if !prompt.contains("Disconnect tenant-a/project-a") {
            return Err(CoreError::new(ErrorCode::InvalidRequest));
        }
        let existed = state
            .session
            .as_ref()
            .is_some_and(|value| value.session_id == session_id);
        if existed {
            state.session = None;
            state.descriptor = None;
        }
        Ok(existed)
    }
}

pub struct FakeAuthorizer {
    value: Mutex<Option<AuthorizedSession>>,
    failure: Mutex<Option<CoreError>>,
}

impl FakeAuthorizer {
    pub fn new(value: AuthorizedSession) -> Self {
        Self {
            value: Mutex::new(Some(value)),
            failure: Mutex::new(None),
        }
    }
}

impl OAuthAuthorizer for FakeAuthorizer {
    fn authorize(&self, target: &ValidatedTarget) -> CoreResult<AuthorizedSession> {
        if let Some(error) = *self.failure.lock().unwrap() {
            return Err(error);
        }
        let value = self
            .value
            .lock()
            .unwrap()
            .take()
            .ok_or_else(|| CoreError::new(ErrorCode::OAuthAuthorizationFailed))?;
        if value.session.target != TargetRecord::from(target) {
            return Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed));
        }
        Ok(value)
    }
}

pub struct FakeRefresher {
    replacement: OAuthTokenSet,
    failure: Mutex<Option<CoreError>>,
    count: AtomicUsize,
}

impl FakeRefresher {
    pub fn new(replacement: OAuthTokenSet) -> Self {
        Self {
            replacement,
            failure: Mutex::new(None),
            count: AtomicUsize::new(0),
        }
    }

    pub fn count(&self) -> usize {
        self.count.load(Ordering::SeqCst)
    }
}

impl OAuthRefresher for FakeRefresher {
    fn refresh(&self, session: &ProtectedOAuthSession) -> CoreResult<OAuthTokenSet> {
        self.count.fetch_add(1, Ordering::SeqCst);
        if session.tokens.refresh_token.expose() != REFRESH_MARKER {
            return Err(CoreError::new(ErrorCode::OAuthRefreshFailed));
        }
        if let Some(error) = *self.failure.lock().unwrap() {
            return Err(error);
        }
        Ok(self.replacement.clone())
    }
}

pub struct FakeRevoker {
    failure: Mutex<Option<CoreError>>,
    count: AtomicUsize,
    saw_refresh: Mutex<Vec<bool>>,
}

impl FakeRevoker {
    pub fn new() -> Self {
        Self {
            failure: Mutex::new(None),
            count: AtomicUsize::new(0),
            saw_refresh: Mutex::new(Vec::new()),
        }
    }

    pub fn fail(&self, error: CoreError) {
        *self.failure.lock().unwrap() = Some(error);
    }

    pub fn count(&self) -> usize {
        self.count.load(Ordering::SeqCst)
    }

    pub fn all_refresh_tokens_were_internal(&self) -> bool {
        self.saw_refresh.lock().unwrap().iter().all(|value| *value)
    }
}

impl OAuthRevoker for FakeRevoker {
    fn revoke(&self, session: &ProtectedOAuthSession) -> CoreResult<()> {
        self.count.fetch_add(1, Ordering::SeqCst);
        self.saw_refresh
            .lock()
            .unwrap()
            .push(session.tokens.refresh_token.expose() == REFRESH_MARKER);
        if let Some(error) = *self.failure.lock().unwrap() {
            return Err(error);
        }
        Ok(())
    }
}

pub struct FakeOperations {
    failure: Mutex<Option<CoreError>>,
    count: AtomicUsize,
    saw_access: Mutex<Vec<String>>,
}

impl FakeOperations {
    pub fn new() -> Self {
        Self {
            failure: Mutex::new(None),
            count: AtomicUsize::new(0),
            saw_access: Mutex::new(Vec::new()),
        }
    }

    pub fn fail(&self, error: CoreError) {
        *self.failure.lock().unwrap() = Some(error);
    }

    pub fn count(&self) -> usize {
        self.count.load(Ordering::SeqCst)
    }

    pub fn access_tokens(&self) -> Vec<String> {
        self.saw_access.lock().unwrap().clone()
    }
}

impl OperationRegistry for FakeOperations {
    fn operation_ids(&self) -> Vec<&'static str> {
        vec!["fixture.inspect"]
    }

    fn prompt(
        &self,
        target: &TargetRecord,
        invocation: &ValidatedInvocation,
    ) -> CoreResult<String> {
        if invocation.operation_id != "fixture.inspect"
            || invocation.arguments.len() != 1
            || !invocation.arguments.contains_key("application_id")
        {
            return Err(CoreError::new(ErrorCode::OperationNotSupported));
        }
        Ok(format!(
            "fixture.inspect for {}/{} at {}",
            target.tenant, target.project, target.normalized_origin
        ))
    }

    fn execute(
        &self,
        session: &ProtectedOAuthSession,
        invocation: &ValidatedInvocation,
    ) -> CoreResult<SafeOperationEvidence> {
        self.count.fetch_add(1, Ordering::SeqCst);
        self.saw_access
            .lock()
            .unwrap()
            .push(session.tokens.access_token.expose().to_owned());
        if let Some(error) = *self.failure.lock().unwrap() {
            return Err(error);
        }
        Ok(SafeOperationEvidence {
            operation_id: invocation.operation_id.clone(),
            invocation_id: invocation.invocation_id.hyphenated().to_string(),
            outcome: "accepted".to_owned(),
            application_id: invocation.arguments.get("application_id").cloned(),
            generation: Some("generation-2".to_owned()),
            card_revision: Some("4".to_owned()),
            catalog_revision: Some("catalog-3".to_owned()),
            policy_revision: Some("7".to_owned()),
        })
    }
}

#[derive(Default)]
pub struct MutexSessionLock {
    lock: Mutex<()>,
}

impl SessionLock for MutexSessionLock {
    fn with_lock(
        &self,
        _session_id: Uuid,
        operation: &mut dyn FnMut() -> CoreResult<()>,
    ) -> CoreResult<()> {
        let _guard = self.lock.lock().unwrap();
        operation()
    }
}

pub struct FakeTransport {
    responses: Mutex<VecDeque<CoreResult<HttpResponse>>>,
    requests: Mutex<Vec<HttpRequest>>,
}

impl FakeTransport {
    pub fn new(responses: Vec<CoreResult<HttpResponse>>) -> Self {
        Self {
            responses: Mutex::new(responses.into()),
            requests: Mutex::new(Vec::new()),
        }
    }

    pub fn requests(&self) -> Vec<HttpRequest> {
        self.requests.lock().unwrap().clone()
    }
}

impl HttpTransport for FakeTransport {
    fn send(&self, request: HttpRequest) -> CoreResult<HttpResponse> {
        self.requests.lock().unwrap().push(request);
        self.responses
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| Err(CoreError::new(ErrorCode::OperationFailed)))
    }
}

pub struct FakeBrowser {
    opened: Mutex<Vec<Url>>,
    accepted: Mutex<bool>,
}

impl Default for FakeBrowser {
    fn default() -> Self {
        Self {
            opened: Mutex::new(Vec::new()),
            accepted: Mutex::new(true),
        }
    }
}

impl FakeBrowser {
    pub fn urls(&self) -> Vec<Url> {
        self.opened.lock().unwrap().clone()
    }
}

impl Browser for FakeBrowser {
    fn open(&self, url: &Url) -> bool {
        self.opened.lock().unwrap().push(url.clone());
        *self.accepted.lock().unwrap()
    }
}

pub struct FakeCallbackFactory {
    redirect_uri: Url,
    code: String,
    observations: Arc<Mutex<Vec<(String, String, bool)>>>,
}

impl FakeCallbackFactory {
    pub fn new() -> Self {
        Self {
            redirect_uri: Url::parse("http://127.0.0.1:49191/oauth/callback").unwrap(),
            code: "disposable-authorization-code".to_owned(),
            observations: Arc::new(Mutex::new(Vec::new())),
        }
    }

    pub fn redirect_uri(&self) -> &Url {
        &self.redirect_uri
    }
}

struct FakeCallback {
    redirect_uri: Url,
    code: String,
    observations: Arc<Mutex<Vec<(String, String, bool)>>>,
}

impl AuthorizationCallback for FakeCallback {
    fn redirect_uri(&self) -> &Url {
        &self.redirect_uri
    }

    fn receive_code(
        &mut self,
        expected_state: &str,
        expected_issuer: &str,
        issuer_required: bool,
        _timeout: std::time::Duration,
    ) -> CoreResult<String> {
        self.observations.lock().unwrap().push((
            expected_state.to_owned(),
            expected_issuer.to_owned(),
            issuer_required,
        ));
        Ok(self.code.clone())
    }
}

impl AuthorizationCallbackFactory for FakeCallbackFactory {
    fn create(&self) -> CoreResult<Box<dyn AuthorizationCallback>> {
        Ok(Box::new(FakeCallback {
            redirect_uri: self.redirect_uri.clone(),
            code: self.code.clone(),
            observations: self.observations.clone(),
        }))
    }
}

pub fn json_response(body: impl Into<Vec<u8>>, status_code: u16) -> HttpResponse {
    HttpResponse {
        status_code,
        content_type: Some("application/json; charset=utf-8".to_owned()),
        body: body.into(),
    }
}

pub fn response_is_safe(response: &crate::protocol::HelperResponse) -> bool {
    let encoded = serde_json::to_string(response).unwrap();
    ![
        ACCESS_MARKER,
        REFRESH_MARKER,
        ROTATED_ACCESS_MARKER,
        ROTATED_REFRESH_MARKER,
        ERROR_MARKER,
    ]
    .iter()
    .any(|marker| encoded.contains(marker))
}
