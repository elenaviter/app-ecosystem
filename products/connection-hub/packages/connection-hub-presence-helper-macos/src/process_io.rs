use std::io::{Read, Write};

use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::protocol::{HelperResponse, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES};

pub fn read_request(mut input: impl Read) -> CoreResult<Vec<u8>> {
    let mut value = Vec::new();
    input
        .by_ref()
        .take((MAX_REQUEST_BYTES + 1) as u64)
        .read_to_end(&mut value)
        .map_err(|_| CoreError::new(ErrorCode::InvalidRequest))?;
    if value.len() > MAX_REQUEST_BYTES {
        Err(CoreError::new(ErrorCode::RequestTooLarge))
    } else {
        Ok(value)
    }
}

pub fn encode_response(response: &HelperResponse) -> CoreResult<Vec<u8>> {
    let value =
        serde_json::to_vec(response).map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
    if value.len() <= MAX_RESPONSE_BYTES {
        Ok(value)
    } else {
        serde_json::to_vec(&HelperResponse::failure(
            response.request_id.clone(),
            ErrorCode::ResponseTooLarge,
            false,
        ))
        .map_err(|_| CoreError::new(ErrorCode::InternalFailure))
    }
}

pub fn write_response(response: &HelperResponse, mut output: impl Write) -> CoreResult<()> {
    let mut value = encode_response(response)?;
    value.push(b'\n');
    output
        .write_all(&value)
        .map_err(|_| CoreError::new(ErrorCode::InternalFailure))
}
