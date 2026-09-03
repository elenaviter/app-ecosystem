use std::ffi::CStr;
use std::fs::{self, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use uuid::Uuid;

use crate::error::{CoreError, CoreResult, ErrorCode};

pub trait SessionLock: Send + Sync {
    fn with_lock(
        &self,
        session_id: Uuid,
        operation: &mut dyn FnMut() -> CoreResult<()>,
    ) -> CoreResult<()>;
}

pub struct FileSessionLock {
    directory: PathBuf,
    timeout: Duration,
}

impl FileSessionLock {
    pub fn new(directory: PathBuf, timeout: Duration) -> Self {
        Self { directory, timeout }
    }

    pub fn default_directory() -> CoreResult<PathBuf> {
        Ok(current_user_home()?
            .join("Library/Application Support/KDCube/ConnectionHubPresenceHelper/locks"))
    }

    fn prepare_directory(&self) -> CoreResult<()> {
        fs::create_dir_all(&self.directory)
            .map_err(|_| CoreError::retryable(ErrorCode::SessionBusy))?;
        fs::set_permissions(&self.directory, fs::Permissions::from_mode(0o700))
            .map_err(|_| CoreError::retryable(ErrorCode::SessionBusy))?;
        let metadata = fs::symlink_metadata(&self.directory)
            .map_err(|_| CoreError::retryable(ErrorCode::SessionBusy))?;
        if !metadata.file_type().is_dir()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o077 != 0
        {
            return Err(CoreError::retryable(ErrorCode::SessionBusy));
        }
        Ok(())
    }

    fn open_lock_file(&self, path: &Path) -> CoreResult<std::fs::File> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .mode(0o600)
            .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
            .open(path)
            .map_err(|_| CoreError::retryable(ErrorCode::SessionBusy))?;
        let metadata = file
            .metadata()
            .map_err(|_| CoreError::retryable(ErrorCode::SessionBusy))?;
        if !metadata.file_type().is_file()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o077 != 0
        {
            return Err(CoreError::retryable(ErrorCode::SessionBusy));
        }
        Ok(file)
    }
}

fn current_user_home() -> CoreResult<PathBuf> {
    let uid = unsafe { libc::geteuid() };
    let suggested = unsafe { libc::sysconf(libc::_SC_GETPW_R_SIZE_MAX) };
    let buffer_size = if suggested > 0 {
        usize::try_from(suggested)
            .unwrap_or(16 * 1024)
            .clamp(1024, 1024 * 1024)
    } else {
        16 * 1024
    };
    let mut record = unsafe { std::mem::zeroed::<libc::passwd>() };
    let mut result = std::ptr::null_mut();
    let mut buffer = vec![0_i8; buffer_size];
    let status = unsafe {
        libc::getpwuid_r(
            uid,
            &mut record,
            buffer.as_mut_ptr(),
            buffer.len(),
            &mut result,
        )
    };
    if status != 0 || result.is_null() || record.pw_dir.is_null() {
        return Err(CoreError::new(ErrorCode::SessionBusy));
    }
    let bytes = unsafe { CStr::from_ptr(record.pw_dir) }.to_bytes();
    if bytes.is_empty() || bytes[0] != b'/' {
        return Err(CoreError::new(ErrorCode::SessionBusy));
    }
    Ok(PathBuf::from(std::ffi::OsStr::from_bytes(bytes)))
}

impl SessionLock for FileSessionLock {
    fn with_lock(
        &self,
        session_id: Uuid,
        operation: &mut dyn FnMut() -> CoreResult<()>,
    ) -> CoreResult<()> {
        self.prepare_directory()?;
        let path = self.directory.join(format!("{session_id}.lock"));
        let file = self.open_lock_file(&path)?;
        let deadline = Instant::now() + self.timeout;
        loop {
            let status = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
            if status == 0 {
                break;
            }
            let error = std::io::Error::last_os_error();
            let contended = error
                .raw_os_error()
                .is_some_and(|code| code == libc::EWOULDBLOCK || code == libc::EAGAIN);
            if !contended || Instant::now() >= deadline {
                return Err(CoreError::retryable(ErrorCode::SessionBusy));
            }
            thread::sleep(Duration::from_millis(20));
        }
        let result = operation();
        unsafe {
            libc::flock(file.as_raw_fd(), libc::LOCK_UN);
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::FileSessionLock;

    #[test]
    fn default_lock_directory_uses_the_os_account_home() {
        let directory = FileSessionLock::default_directory().unwrap();
        assert!(directory.is_absolute());
        assert!(directory
            .ends_with("Library/Application Support/KDCube/ConnectionHubPresenceHelper/locks"));
    }
}
