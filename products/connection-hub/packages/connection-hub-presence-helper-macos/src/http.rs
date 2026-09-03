use std::io::Read;
use std::time::Duration;

use reqwest::blocking::Client;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue, AUTHORIZATION, CONTENT_TYPE};
use reqwest::redirect::Policy;
use url::Url;

use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::validation::validate_endpoint;

pub const DEFAULT_MAX_RESPONSE_BYTES: usize = 1024 * 1024;

#[derive(Clone)]
pub struct HttpRequest {
    pub url: Url,
    pub method: &'static str,
    pub headers: Vec<(&'static str, String)>,
    pub body: Vec<u8>,
    pub maximum_response_bytes: usize,
    pub timeout: Duration,
}

impl HttpRequest {
    pub fn get(url: Url) -> Self {
        Self {
            url,
            method: "GET",
            headers: Vec::new(),
            body: Vec::new(),
            maximum_response_bytes: DEFAULT_MAX_RESPONSE_BYTES,
            timeout: Duration::from_secs(30),
        }
    }

    pub fn post(url: Url, body: Vec<u8>) -> Self {
        Self {
            url,
            method: "POST",
            headers: Vec::new(),
            body,
            maximum_response_bytes: DEFAULT_MAX_RESPONSE_BYTES,
            timeout: Duration::from_secs(30),
        }
    }
}

#[derive(Clone)]
pub struct HttpResponse {
    pub status_code: u16,
    pub content_type: Option<String>,
    pub body: Vec<u8>,
}

pub trait HttpTransport: Send + Sync {
    fn send(&self, request: HttpRequest) -> CoreResult<HttpResponse>;
}

pub struct FixedHttpTransport {
    client: Client,
}

impl FixedHttpTransport {
    pub fn new() -> CoreResult<Self> {
        let client = Client::builder()
            .redirect(Policy::none())
            .no_proxy()
            .build()
            .map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
        Ok(Self { client })
    }
}

impl HttpTransport for FixedHttpTransport {
    fn send(&self, request: HttpRequest) -> CoreResult<HttpResponse> {
        validate_endpoint(&request.url)?;
        if !matches!(request.method, "GET" | "POST")
            || request.maximum_response_bytes == 0
            || request.maximum_response_bytes > 4 * 1024 * 1024
            || request.body.len() > 1024 * 1024
            || request.timeout.is_zero()
            || request.timeout > Duration::from_secs(120)
        {
            return Err(CoreError::new(ErrorCode::InvalidRequest));
        }
        let mut headers = HeaderMap::new();
        for (name, value) in request.headers {
            let name = HeaderName::from_bytes(name.as_bytes())
                .map_err(|_| CoreError::new(ErrorCode::InvalidRequest))?;
            let mut value = HeaderValue::from_str(&value)
                .map_err(|_| CoreError::new(ErrorCode::InvalidRequest))?;
            if name == AUTHORIZATION {
                value.set_sensitive(true);
            }
            headers.insert(name, value);
        }
        let mut builder = match request.method {
            "GET" => self.client.get(request.url),
            "POST" => self.client.post(request.url).body(request.body),
            _ => return Err(CoreError::new(ErrorCode::InvalidRequest)),
        };
        builder = builder.headers(headers).timeout(request.timeout);
        let response = builder
            .send()
            .map_err(|_| CoreError::retryable(ErrorCode::OperationFailed))?;
        if response
            .content_length()
            .is_some_and(|length| length > request.maximum_response_bytes as u64)
        {
            return Err(CoreError::new(ErrorCode::ResponseTooLarge));
        }
        let status_code = response.status().as_u16();
        let content_type = response
            .headers()
            .get(CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .map(str::to_owned);
        let mut body = Vec::with_capacity(request.maximum_response_bytes.min(64 * 1024));
        response
            .take((request.maximum_response_bytes + 1) as u64)
            .read_to_end(&mut body)
            .map_err(|_| CoreError::retryable(ErrorCode::OperationFailed))?;
        if body.len() > request.maximum_response_bytes {
            return Err(CoreError::new(ErrorCode::ResponseTooLarge));
        }
        Ok(HttpResponse {
            status_code,
            content_type,
            body,
        })
    }
}

pub fn json_content_type(value: Option<&str>) -> bool {
    value.is_some_and(|content_type| {
        content_type
            .split(';')
            .next()
            .is_some_and(|kind| kind.trim().eq_ignore_ascii_case("application/json"))
    })
}
