#![cfg_attr(not(target_os = "macos"), allow(dead_code))]

#[cfg(not(target_os = "macos"))]
compile_error!("connection-hub-presence-helper-macos only supports macOS");

mod browser;
mod callback;
mod error;
mod http;
#[cfg(feature = "interactive-check")]
mod interactive;
mod keychain;
mod lock;
mod management;
mod oauth;
mod process_io;
mod protocol;
mod runtime;
mod service;
mod session;
mod validation;

pub fn run_helper() -> i32 {
    std::panic::catch_unwind(runtime::run).unwrap_or_else(|_| runtime::write_internal_failure())
}

#[cfg(feature = "interactive-check")]
pub fn run_interactive_check() -> i32 {
    interactive::run()
}

#[cfg(test)]
mod tests;
