use std::{
    fs::{self, File, Metadata, OpenOptions, TryLockError},
    io::{self, Read, Write},
    path::{Path, PathBuf},
};

#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
#[cfg(windows)]
use std::os::windows::{fs::OpenOptionsExt, io::AsRawHandle};

use anyhow::{Context, Result, ensure};
use sha2::{Digest, Sha256};

#[derive(Debug)]
pub struct Guard(pub &'static str);

impl std::fmt::Display for Guard {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}

impl std::error::Error for Guard {}

pub fn digest(body: &[u8]) -> String {
    hex::encode(Sha256::digest(body))
}

pub fn hex_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

#[derive(Eq, PartialEq)]
struct Identity {
    volume: u64,
    index: u64,
    links: u64,
    len: u64,
    last_modified: (i128, i64),
    last_changed: (i128, i64),
    owner: u32,
    mode: u32,
}

#[cfg(unix)]
fn identity(file: &File) -> Result<Identity> {
    let m = file.metadata()?;
    Ok(Identity {
        volume: m.dev(),
        index: m.ino(),
        links: m.nlink(),
        len: m.len(),
        last_modified: (m.mtime().into(), m.mtime_nsec()),
        last_changed: (m.ctime().into(), m.ctime_nsec()),
        owner: m.uid(),
        mode: m.mode(),
    })
}

#[cfg(windows)]
fn identity(file: &File) -> Result<Identity> {
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_REPARSE_POINT, GetFileInformationByHandle,
    };

    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0 {
        return Err(io::Error::last_os_error().into());
    }
    ensure!(
        info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT == 0,
        Guard("untrusted-path")
    );
    let combine = |high, low| u64::from(high) << 32 | u64::from(low);
    Ok(Identity {
        volume: u64::from(info.dwVolumeSerialNumber),
        index: combine(info.nFileIndexHigh, info.nFileIndexLow),
        links: u64::from(info.nNumberOfLinks),
        len: combine(info.nFileSizeHigh, info.nFileSizeLow),
        last_modified: (
            combine(
                info.ftLastWriteTime.dwHighDateTime,
                info.ftLastWriteTime.dwLowDateTime,
            )
            .into(),
            0,
        ),
        last_changed: (
            combine(
                info.ftCreationTime.dwHighDateTime,
                info.ftCreationTime.dwLowDateTime,
            )
            .into(),
            0,
        ),
        owner: 0,
        mode: info.dwFileAttributes,
    })
}

#[cfg(unix)]
fn owned(meta: &Metadata) -> bool {
    meta.uid() == unsafe { libc::geteuid() } && meta.mode() & 0o022 == 0
}

#[cfg(windows)]
fn owned(meta: &Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    use windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT;
    meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT == 0
}

#[cfg(unix)]
pub fn canonical(path: &Path) -> io::Result<PathBuf> {
    path.canonicalize()
}

#[cfg(windows)]
pub fn canonical(path: &Path) -> io::Result<PathBuf> {
    dunce::canonicalize(path)
}

fn options(write: bool) -> OpenOptions {
    let mut opts = OpenOptions::new();
    opts.read(true).write(write);
    #[cfg(unix)]
    opts.mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    #[cfg(windows)]
    opts.custom_flags(windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT);
    opts
}

fn directory(path: &Path) -> Result<()> {
    let info = fs::symlink_metadata(path)?;
    let actual = canonical(path)?;
    ensure!(
        info.is_dir() && actual == path && owned(&info),
        Guard("unsafe-installation-directory-preserved")
    );
    Ok(())
}

fn directory_identity(path: &Path) -> Result<(u64, u64, u32, u32)> {
    directory(path)?;
    let mut opts = options(false);
    #[cfg(unix)]
    opts.custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY);
    #[cfg(windows)]
    {
        use windows_sys::Win32::Storage::FileSystem::{
            FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        };
        opts.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS);
    }
    let info = identity(&opts.open(path)?)?;
    Ok((info.volume, info.index, info.owner, info.mode))
}

pub fn checked_parent(path: &Path, create: bool) -> Result<()> {
    let mut nearest = path;
    let mut missing = Vec::new();
    loop {
        match fs::symlink_metadata(nearest) {
            Ok(_) => {
                directory(nearest)?;
                break;
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                missing.push(nearest);
                nearest = nearest
                    .parent()
                    .context("no trusted installation ancestor")?;
            }
            Err(error) => return Err(error.into()),
        }
    }
    if create {
        for folder in missing.into_iter().rev() {
            #[cfg(unix)]
            let created = fs::DirBuilder::new().mode(0o700).create(folder);
            #[cfg(windows)]
            let created = fs::create_dir(folder);
            match created {
                Ok(()) => (),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => (),
                Err(error) => return Err(error.into()),
            }
            directory(folder)?;
        }
    }
    Ok(())
}

#[derive(Eq, PartialEq)]
pub struct Snapshot {
    pub bytes: Vec<u8>,
    stamp: Identity,
}

impl Snapshot {
    pub fn hash(&self) -> String {
        digest(&self.bytes)
    }

    pub fn matches(&self, body: &[u8], mode: u32) -> bool {
        self.bytes == body && (!cfg!(unix) || self.stamp.mode & 0o777 == mode)
    }
}

pub fn snapshot(path: &Path, limit: usize) -> Result<Option<Snapshot>> {
    let meta = match fs::symlink_metadata(path) {
        Ok(info) => info,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    ensure!(
        meta.is_file() && owned(&meta) && canonical(path)? == path,
        Guard("untrusted-path")
    );
    let mut file = options(false).open(path)?;
    let stamp = identity(&file)?;
    ensure!(
        stamp.links == 1 && stamp.len <= limit as u64 && owned(&file.metadata()?),
        Guard("untrusted-file")
    );
    let mut bytes = Vec::with_capacity(stamp.len as usize);
    (&mut file).take(limit as u64 + 1).read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 == stamp.len
            && identity(&file)? == stamp
            && identity(&options(false).open(path)?)? == stamp
            && canonical(path)? == path,
        Guard("concurrent-file-change")
    );
    Ok(Some(Snapshot { bytes, stamp }))
}

pub fn replace(
    path: &Path,
    body: &[u8],
    mode: u32,
    expected: Option<&Snapshot>,
    limit: usize,
) -> Result<bool> {
    let parent = path.parent().context("destination parent missing")?;
    checked_parent(parent, true)?;
    let parent_before = directory_identity(parent)?;
    if expected.is_some_and(|prior| prior.bytes == body) {
        #[cfg(unix)]
        if expected.is_some_and(|prior| prior.stamp.mode & 0o777 != mode) {
            let file = options(false).open(path)?;
            ensure!(
                expected.is_some_and(|prior| identity(&file).is_ok_and(|live| live == prior.stamp)),
                Guard("concurrent-file-change")
            );
            file.set_permissions(fs::Permissions::from_mode(mode))?;
            return Ok(true);
        }
        ensure!(
            snapshot(path, limit)?.as_ref() == expected,
            Guard("concurrent-file-change")
        );
        return Ok(false);
    }
    let mut pending = tempfile::Builder::new()
        .prefix(".prepare-")
        .tempfile_in(parent)?;
    pending.write_all(body)?;
    #[cfg(unix)]
    pending
        .as_file()
        .set_permissions(fs::Permissions::from_mode(mode))?;
    #[cfg(windows)]
    let _ = mode;
    pending.as_file().sync_all()?;
    ensure!(
        directory_identity(parent)? == parent_before,
        Guard("concurrent-file-change")
    );
    ensure!(
        snapshot(path, limit)?.as_ref() == expected,
        Guard("concurrent-file-change")
    );
    pending.persist(path)?;
    #[cfg(unix)]
    if matches!(
        path.file_name().and_then(|name| name.to_str()),
        Some(".modelhub-kit-install.json" | ".install-pending.json")
    ) {
        File::open(parent)?.sync_all()?;
    }
    Ok(true)
}

pub fn lock(path: &Path, shared: bool) -> Result<File> {
    checked_parent(path.parent().context("lock parent missing")?, true)?;
    let file = options(true).create(true).open(path)?;
    let info = file.metadata()?;
    let stamp = identity(&file)?;
    ensure!(
        info.is_file() && owned(&info) && stamp.links == 1 && canonical(path)? == path,
        Guard("untrusted-lock")
    );
    match if shared {
        file.try_lock_shared()
    } else {
        file.try_lock()
    } {
        Ok(()) => (),
        Err(TryLockError::WouldBlock) => return Err(Guard("writer-busy").into()),
        Err(TryLockError::Error(error)) => return Err(error.into()),
    }
    let live = options(false).open(path)?;
    ensure!(identity(&live)? == stamp, Guard("untrusted-lock"));
    Ok(file)
}
