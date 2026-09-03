use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;
use std::time::{Duration, Instant};

use url::Url;

use crate::error::{CoreError, CoreResult, ErrorCode};

pub trait AuthorizationCallback: Send {
    fn redirect_uri(&self) -> &Url;
    fn receive_code(
        &mut self,
        expected_state: &str,
        expected_issuer: &str,
        issuer_required: bool,
        timeout: Duration,
    ) -> CoreResult<String>;
}

pub trait AuthorizationCallbackFactory: Send + Sync {
    fn create(&self) -> CoreResult<Box<dyn AuthorizationCallback>>;
}

#[derive(Default)]
pub struct LoopbackCallbackFactory;

impl AuthorizationCallbackFactory for LoopbackCallbackFactory {
    fn create(&self) -> CoreResult<Box<dyn AuthorizationCallback>> {
        Ok(Box::new(LoopbackCallback::new()?))
    }
}

pub struct LoopbackCallback {
    listener: TcpListener,
    redirect_uri: Url,
}

impl LoopbackCallback {
    pub fn new() -> CoreResult<Self> {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .map_err(|_| CoreError::new(ErrorCode::OAuthAuthorizationFailed))?;
        listener
            .set_nonblocking(true)
            .map_err(|_| CoreError::new(ErrorCode::OAuthAuthorizationFailed))?;
        let port = listener
            .local_addr()
            .map_err(|_| CoreError::new(ErrorCode::OAuthAuthorizationFailed))?
            .port();
        let redirect_uri = Url::parse(&format!("http://127.0.0.1:{port}/oauth/callback"))
            .map_err(|_| CoreError::new(ErrorCode::OAuthAuthorizationFailed))?;
        Ok(Self {
            listener,
            redirect_uri,
        })
    }

    fn receive_one(
        &self,
        stream: &mut TcpStream,
        expected_state: &str,
        expected_issuer: &str,
        issuer_required: bool,
    ) -> Option<String> {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
        let mut bytes = Vec::new();
        if (&mut *stream).take(8193).read_to_end(&mut bytes).is_err() || bytes.len() > 8192 {
            send_response(stream, false);
            return None;
        }
        let request = String::from_utf8(bytes).ok()?;
        let line = request.split("\r\n").next()?;
        let mut fields = line.split_ascii_whitespace();
        let method = fields.next()?;
        let target = fields.next()?;
        let version = fields.next()?;
        if fields.next().is_some() || method != "GET" || !matches!(version, "HTTP/1.0" | "HTTP/1.1")
        {
            send_response(stream, false);
            return None;
        }
        let url = Url::parse(&format!("http://127.0.0.1{target}")).ok()?;
        if url.path() != "/oauth/callback" || url.fragment().is_some() {
            send_response(stream, false);
            return None;
        }
        let mut code = Vec::new();
        let mut state = Vec::new();
        let mut issuer = Vec::new();
        let mut errors = 0;
        for (name, value) in url.query_pairs() {
            match name.as_ref() {
                "code" => code.push(value.into_owned()),
                "state" => state.push(value.into_owned()),
                "iss" => issuer.push(value.into_owned()),
                "error" => errors += 1,
                _ => {}
            }
        }
        let issuer_valid = if issuer_required {
            issuer.as_slice() == [expected_issuer]
        } else {
            issuer.is_empty() || issuer.as_slice() == [expected_issuer]
        };
        let result = (errors == 0
            && state.as_slice() == [expected_state]
            && code.len() == 1
            && !code[0].is_empty()
            && code[0].len() <= 4096
            && issuer_valid)
            .then(|| code.remove(0));
        send_response(stream, result.is_some());
        result
    }
}

impl AuthorizationCallback for LoopbackCallback {
    fn redirect_uri(&self) -> &Url {
        &self.redirect_uri
    }

    fn receive_code(
        &mut self,
        expected_state: &str,
        expected_issuer: &str,
        issuer_required: bool,
        timeout: Duration,
    ) -> CoreResult<String> {
        if timeout.is_zero() || timeout > Duration::from_secs(300) {
            return Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed));
        }
        let deadline = Instant::now() + timeout;
        let mut attempts = 0;
        while Instant::now() < deadline && attempts < 8 {
            match self.listener.accept() {
                Ok((mut stream, address)) => {
                    attempts += 1;
                    if !address.ip().is_loopback() {
                        continue;
                    }
                    if let Some(code) = self.receive_one(
                        &mut stream,
                        expected_state,
                        expected_issuer,
                        issuer_required,
                    ) {
                        return Ok(code);
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(20));
                }
                Err(_) => {
                    return Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed));
                }
            }
        }
        Err(CoreError::new(ErrorCode::OAuthAuthorizationFailed))
    }
}

fn send_response(stream: &mut TcpStream, success: bool) {
    let body = if success {
        "Authorization completed. Return to the terminal."
    } else {
        "Authorization was not accepted. Return to the terminal."
    };
    let status = if success { "200 OK" } else { "400 Bad Request" };
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes());
}

#[cfg(test)]
mod tests {
    use std::io::Write;
    use std::net::TcpStream;
    use std::thread;
    use std::time::Duration;

    use super::{AuthorizationCallback, LoopbackCallback};
    use crate::error::ErrorCode;

    #[test]
    fn loopback_callback_binds_state_issuer_and_path() {
        let mut callback = LoopbackCallback::new().unwrap();
        let address = callback.redirect_uri().socket_addrs(|| None).unwrap()[0];
        let sender = thread::spawn(move || {
            thread::sleep(Duration::from_millis(40));
            let mut stream = TcpStream::connect(address).unwrap();
            stream
                .write_all(
                    b"GET /oauth/callback?code=one-time-code&state=expected-state&iss=https%3A%2F%2Ftarget.example%2Foauth HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
                )
                .unwrap();
        });
        let code = callback
            .receive_code(
                "expected-state",
                "https://target.example/oauth",
                true,
                Duration::from_secs(2),
            )
            .unwrap();
        sender.join().unwrap();
        assert_eq!(code, "one-time-code");
    }

    #[test]
    fn invalid_callback_returns_only_a_fixed_failure() {
        let mut callback = LoopbackCallback::new().unwrap();
        let address = callback.redirect_uri().socket_addrs(|| None).unwrap()[0];
        let sender = thread::spawn(move || {
            thread::sleep(Duration::from_millis(40));
            let mut stream = TcpStream::connect(address).unwrap();
            stream
                .write_all(
                    b"GET /oauth/callback?code=secret-marker&state=wrong HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
                )
                .unwrap();
        });
        let error = callback
            .receive_code(
                "expected-state",
                "https://target.example/oauth",
                true,
                Duration::from_millis(150),
            )
            .unwrap_err();
        sender.join().unwrap();
        assert_eq!(error.code, ErrorCode::OAuthAuthorizationFailed);
        assert!(!error.to_string().contains("secret-marker"));
    }
}
