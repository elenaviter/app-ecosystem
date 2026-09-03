use std::io::{stdin, stdout};
use std::sync::Arc;
use std::time::Duration;

use crate::browser::SystemBrowser;
use crate::callback::LoopbackCallbackFactory;
use crate::error::{CoreError, ErrorCode};
use crate::http::FixedHttpTransport;
use crate::keychain::KeychainSessionStore;
use crate::lock::FileSessionLock;
use crate::management::KdcubeManagementOperations;
use crate::oauth::{
    OAuthPolicy, StandardOAuthAuthorizer, StandardOAuthRefresher, StandardOAuthRevoker,
};
use crate::process_io::{read_request, write_response};
use crate::protocol::{HelperRequest, HelperResponse};
use crate::service::{PresenceService, SystemClock};

fn make_service() -> Result<PresenceService, CoreError> {
    let transport = Arc::new(FixedHttpTransport::new()?);
    let browser = Arc::new(SystemBrowser);
    let clock = Arc::new(SystemClock);
    Ok(PresenceService::new(
        env!("KDCUBE_HELPER_VERSION"),
        Arc::new(KeychainSessionStore::new()),
        Arc::new(StandardOAuthAuthorizer::new(
            OAuthPolicy::new(Vec::new())?,
            transport.clone(),
            browser.clone(),
            Arc::new(LoopbackCallbackFactory),
            clock.clone(),
        )),
        Arc::new(StandardOAuthRefresher::new(
            transport.clone(),
            clock.clone(),
        )),
        Arc::new(StandardOAuthRevoker::new(transport.clone())),
        Arc::new(KdcubeManagementOperations::new(transport, browser)),
        Arc::new(FileSessionLock::new(
            FileSessionLock::default_directory()?,
            Duration::from_secs(15),
        )),
        clock,
    ))
}

pub(crate) fn run() -> i32 {
    let response = match read_request(stdin().lock()) {
        Ok(bytes) => match HelperRequest::decode(&bytes) {
            Ok(request) => match make_service() {
                Ok(service) => service.handle(request),
                Err(error) => HelperResponse::failure(None, error.code, error.retryable),
            },
            Err(error) => HelperResponse::failure(error.request_id, error.code, false),
        },
        Err(error) => HelperResponse::failure(None, error.code, error.retryable),
    };
    if write_response(&response, stdout().lock()).is_err() {
        return 1;
    }
    i32::from(!response.ok)
}

pub(crate) fn write_internal_failure() -> i32 {
    let response = HelperResponse::failure(None, ErrorCode::InternalFailure, false);
    let _ = write_response(&response, stdout().lock());
    1
}
