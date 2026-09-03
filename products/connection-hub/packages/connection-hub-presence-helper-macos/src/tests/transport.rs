use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use url::Url;

use crate::error::ErrorCode;
use crate::http::{FixedHttpTransport, HttpRequest, HttpTransport};

use super::fixtures::{ACCESS_MARKER, ERROR_MARKER};

fn read_request(stream: &mut std::net::TcpStream) -> Vec<u8> {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut value = Vec::new();
    let mut buffer = [0_u8; 4096];
    loop {
        let count = stream.read(&mut buffer).unwrap_or(0);
        if count == 0 {
            break;
        }
        value.extend_from_slice(&buffer[..count]);
        if let Some(headers_end) = value.windows(4).position(|part| part == b"\r\n\r\n") {
            let headers = String::from_utf8_lossy(&value[..headers_end]);
            let content_length = headers
                .lines()
                .find_map(|line| {
                    line.strip_prefix("content-length: ")
                        .or_else(|| line.strip_prefix("Content-Length: "))
                })
                .and_then(|value| value.parse::<usize>().ok())
                .unwrap_or(0);
            if value.len() >= headers_end + 4 + content_length {
                break;
            }
        }
    }
    value
}

#[test]
fn fixed_transport_sends_the_exact_bound_request_to_loopback() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let observed = Arc::new(Mutex::new(Vec::new()));
    let observed_server = observed.clone();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        *observed_server.lock().unwrap() = read_request(&mut stream);
        stream
            .write_all(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{\"ok\":true}",
            )
            .unwrap();
    });
    let transport = FixedHttpTransport::new().unwrap();
    let mut request = HttpRequest::post(
        Url::parse(&format!("http://{address}/api/exact?mode=bound")).unwrap(),
        b"{\"value\":1}".to_vec(),
    );
    request.headers = vec![
        ("Authorization", format!("Bearer {ACCESS_MARKER}")),
        ("Content-Type", "application/json".to_owned()),
    ];
    let response = transport.send(request).unwrap();
    server.join().unwrap();

    assert_eq!(response.status_code, 200);
    assert_eq!(response.body, b"{\"ok\":true}");
    let request = String::from_utf8(observed.lock().unwrap().clone()).unwrap();
    assert!(request.starts_with("POST /api/exact?mode=bound HTTP/1.1\r\n"));
    assert!(request.to_ascii_lowercase().contains(&format!(
        "authorization: bearer {}",
        ACCESS_MARKER.to_ascii_lowercase()
    )));
    assert!(request.ends_with("{\"value\":1}"));
}

#[test]
fn redirects_are_not_followed_and_do_not_forward_authorization() {
    let destination = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    destination.set_nonblocking(true).unwrap();
    let destination_address = destination.local_addr().unwrap();
    let source = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let source_address = source.local_addr().unwrap();
    let observed = Arc::new(Mutex::new(Vec::new()));
    let observed_server = observed.clone();
    let server = thread::spawn(move || {
        let (mut stream, _) = source.accept().unwrap();
        *observed_server.lock().unwrap() = read_request(&mut stream);
        let response = format!(
            "HTTP/1.1 302 Found\r\nLocation: http://{destination_address}/capture\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        );
        stream.write_all(response.as_bytes()).unwrap();
    });
    let transport = FixedHttpTransport::new().unwrap();
    let mut request =
        HttpRequest::get(Url::parse(&format!("http://{source_address}/redirect")).unwrap());
    request.headers = vec![("Authorization", format!("Bearer {ACCESS_MARKER}"))];
    let response = transport.send(request).unwrap();
    server.join().unwrap();
    thread::sleep(Duration::from_millis(100));

    assert_eq!(response.status_code, 302);
    assert!(destination.accept().is_err());
    assert!(String::from_utf8(observed.lock().unwrap().clone())
        .unwrap()
        .contains(ACCESS_MARKER));
}

#[test]
fn declared_oversized_response_is_rejected_before_body_collection() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let _ = read_request(&mut stream);
        stream
            .write_all(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 12\r\nConnection: close\r\n\r\nhello world!",
            )
            .unwrap();
    });
    let transport = FixedHttpTransport::new().unwrap();
    let mut request = HttpRequest::get(Url::parse(&format!("http://{address}/large")).unwrap());
    request.maximum_response_bytes = 5;
    let error = match transport.send(request) {
        Ok(_) => panic!("oversized response was accepted"),
        Err(error) => error,
    };
    server.join().unwrap();
    assert_eq!(error.code, ErrorCode::ResponseTooLarge);
}

#[test]
fn invalid_transport_options_fail_before_network_access() {
    let transport = FixedHttpTransport::new().unwrap();
    let mut request = HttpRequest::get(Url::parse("https://target.example/").unwrap());
    request.timeout = Duration::ZERO;
    let error = match transport.send(request) {
        Ok(_) => panic!("zero timeout was accepted"),
        Err(error) => error,
    };
    assert_eq!(error.code, ErrorCode::InvalidRequest);
}

#[test]
fn transport_failures_expose_only_fixed_error_text() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let address = listener.local_addr().unwrap();
    drop(listener);
    let transport = FixedHttpTransport::new().unwrap();
    let request =
        HttpRequest::get(Url::parse(&format!("http://{address}/{ERROR_MARKER}")).unwrap());
    let error = match transport.send(request) {
        Ok(_) => panic!("closed fixture unexpectedly returned a response"),
        Err(error) => error,
    };
    assert_eq!(error.code, ErrorCode::OperationFailed);
    assert!(!error.to_string().contains(ERROR_MARKER));
    assert!(std::error::Error::source(&error).is_none());
}
