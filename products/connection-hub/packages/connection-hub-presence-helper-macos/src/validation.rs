use std::collections::BTreeMap;
use std::net::IpAddr;

use url::{Host, Url};
use uuid::Uuid;

use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::protocol::{OperationInvocation, TargetCoordinates};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ValidatedTarget {
    pub normalized_origin: String,
    pub tenant: String,
    pub project: String,
    pub caller_profile: String,
    pub oauth_client_id: Option<String>,
}

impl TryFrom<TargetCoordinates> for ValidatedTarget {
    type Error = CoreError;

    fn try_from(value: TargetCoordinates) -> Result<Self, Self::Error> {
        Ok(Self {
            normalized_origin: normalize_origin(&value.origin)?,
            tenant: identifier(&value.tenant)?,
            project: identifier(&value.project)?,
            caller_profile: identifier(&value.caller_profile)?,
            oauth_client_id: optional_oauth_client_id(value.oauth_client_id)?,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ValidatedInvocation {
    pub operation_id: String,
    pub invocation_id: Uuid,
    pub arguments: BTreeMap<String, String>,
}

impl TryFrom<OperationInvocation> for ValidatedInvocation {
    type Error = CoreError;

    fn try_from(value: OperationInvocation) -> Result<Self, Self::Error> {
        let operation_id = identifier(&value.operation_id)?;
        let invocation_id = Uuid::parse_str(&value.invocation_id)
            .map_err(|_| CoreError::new(ErrorCode::InvalidRequest))?;
        if value.arguments.len() > 16 {
            return Err(CoreError::new(ErrorCode::InvalidRequest));
        }
        for (name, argument) in &value.arguments {
            if name.is_empty()
                || name.len() > 64
                || !name.bytes().enumerate().all(|(index, byte)| {
                    matches!(
                        (index, byte),
                        (0, b'a'..=b'z') | (_, b'a'..=b'z' | b'0'..=b'9' | b'_')
                    )
                })
                || argument.len() > 1024
                || argument.chars().any(char::is_control)
            {
                return Err(CoreError::new(ErrorCode::InvalidRequest));
            }
        }
        Ok(Self {
            operation_id,
            invocation_id,
            arguments: value.arguments,
        })
    }
}

pub fn parse_session_id(value: Option<&str>) -> CoreResult<Uuid> {
    value
        .and_then(|candidate| Uuid::parse_str(candidate).ok())
        .ok_or_else(|| CoreError::new(ErrorCode::InvalidRequest))
}

pub fn application_id(value: &str) -> CoreResult<&str> {
    if value.is_empty()
        || value.len() > 256
        || matches!(value, "." | "..")
        || value
            .chars()
            .any(|character| character.is_control() || character.is_whitespace())
        || value
            .bytes()
            .any(|byte| matches!(byte, b'*' | b'/' | b'\\' | b'?' | b'#'))
        || value.contains("://")
    {
        Err(CoreError::new(ErrorCode::InvalidRequest))
    } else {
        Ok(value)
    }
}

pub fn is_loopback_host(host: &str) -> bool {
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }
    host.parse::<IpAddr>()
        .is_ok_and(|value| value.is_loopback())
}

pub fn validate_endpoint(url: &Url) -> CoreResult<()> {
    let host = url
        .host_str()
        .ok_or_else(|| CoreError::new(ErrorCode::InvalidRequest))?;
    if !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || !(url.scheme() == "https" || (url.scheme() == "http" && is_loopback_host(host)))
    {
        return Err(CoreError::new(ErrorCode::InvalidRequest));
    }
    Ok(())
}

pub fn same_origin(left: &Url, right: &Url) -> bool {
    left.scheme().eq_ignore_ascii_case(right.scheme())
        && normalized_host(left) == normalized_host(right)
        && left.port_or_known_default() == right.port_or_known_default()
}

fn normalize_origin(value: &str) -> CoreResult<String> {
    if value.len() > 2048 {
        return Err(CoreError::new(ErrorCode::InvalidRequest));
    }
    let parsed = Url::parse(value).map_err(|_| CoreError::new(ErrorCode::InvalidRequest))?;
    validate_endpoint(&parsed)?;
    if parsed.query().is_some() || !matches!(parsed.path(), "" | "/") {
        return Err(CoreError::new(ErrorCode::InvalidRequest));
    }
    let host = match parsed.host() {
        Some(Host::Ipv6(value)) => format!("[{value}]"),
        Some(value) => value.to_string().to_ascii_lowercase(),
        None => return Err(CoreError::new(ErrorCode::InvalidRequest)),
    };
    let port = parsed
        .port_or_known_default()
        .ok_or_else(|| CoreError::new(ErrorCode::InvalidRequest))?;
    Ok(format!(
        "{}://{host}:{port}",
        parsed.scheme().to_ascii_lowercase()
    ))
}

fn normalized_host(value: &Url) -> Option<String> {
    value.host_str().map(|host| host.to_ascii_lowercase())
}

fn identifier(value: &str) -> CoreResult<String> {
    let valid = !value.is_empty()
        && value.len() <= 256
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric()
                || (index > 0 && matches!(byte, b'.' | b'_' | b':' | b'@' | b'/' | b'-'))
        });
    if valid {
        Ok(value.to_owned())
    } else {
        Err(CoreError::new(ErrorCode::InvalidRequest))
    }
}

fn optional_oauth_client_id(value: Option<String>) -> CoreResult<Option<String>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let candidate = value.trim();
    if candidate.is_empty()
        || candidate.len() > 4096
        || candidate.chars().any(char::is_control)
        || candidate.chars().any(char::is_whitespace)
    {
        return Err(CoreError::new(ErrorCode::InvalidRequest));
    }
    Ok(Some(candidate.to_owned()))
}
