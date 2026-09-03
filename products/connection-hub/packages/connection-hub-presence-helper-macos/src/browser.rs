use std::process::{Command, Stdio};

use url::Url;

pub trait Browser: Send + Sync {
    fn open(&self, url: &Url) -> bool;
}

#[derive(Default)]
pub struct SystemBrowser;

impl Browser for SystemBrowser {
    fn open(&self, url: &Url) -> bool {
        Command::new("/usr/bin/open")
            .arg(url.as_str())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
    }
}
