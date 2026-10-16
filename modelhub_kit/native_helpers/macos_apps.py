"""Signed vendor DMG transactions, without running or opening an application.

Caller holds core.writer and explicitly selects missing installations. Only
direct app bundles belong here; Homebrew ownership must be handled first by the
desktop reconciler. Transport digests are never described as publisher hashes.
See cursor.com/docs/get-started/quickstart, docs.devin.ai/desktop/install and
prod.cursor.com/docs/grok-bot/deployment. No installer scripts are evaluated.
"""
import ctypes
import hashlib
import os
import pathlib
import platform
import plistlib
import re
import shutil
import stat
import tempfile
import time
import urllib.parse
import urllib.request

from .. import core
from . import desktop


MAX_DMG = 1024 * 1024 * 1024
MAX_TREE = 3 * MAX_DMG
MAX_ENTRIES = 100000
SPACE_RESERVE = 512 * 1024 * 1024
NAMES = {'cursor-desktop': 'Cursor.app', 'devin-desktop': 'Devin.app',
         'grokbot': 'Grok Bot.app'}
FEEDS = {'cursor-desktop': 'cursor', 'grokbot': 'sand'}
CASKS = {'cursor-desktop': 'cursor', 'devin-desktop': 'devin-desktop'}
MAC_TAGS = ('golden_gate', 'tahoe', 'sequoia', 'sonoma', 'ventura', 'monterey', 'big_sur')


def _guard(reason):
    raise core.GuardError(reason)


def _arch(value=None):
    value = (value or platform.machine()).lower()
    value = {'aarch64': 'arm64', 'x86_64': 'x64', 'amd64': 'x64'}.get(value, value)
    if value not in ('arm64', 'x64'):
        _guard('vendor-desktop-architecture-unsupported')
    return value


def _url(product, url):
    if not desktop.approved_url(product, url):
        _guard('official-dmg-url-unverified')
    parsed = urllib.parse.urlsplit(url)
    if parsed.port not in (None, 443) or not parsed.path.lower().endswith('.dmg'):
        _guard('official-dmg-url-unverified')
    return url


def _digest(path):
    value = hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _space(path, required=0):
    if shutil.disk_usage(path).free < SPACE_RESERVE + required:
        _guard('native-dmg-insufficient-free-space')


def _pending_path(product, state):
    if product not in NAMES:
        _guard('unknown-native-desktop-product')
    return pathlib.Path(state) / 'native-desktop' / (product + '-dmg-pending.json')


def _pending_exists(product, state):
    path = _pending_path(product, state)
    return path.exists() or path.is_symlink()


def cleanup_pending(product, *, home, state, command=core.run, processes=None):
    """Finish one known transaction before planning, including current apps.

    Caller holds core.writer. Unknown stage paths, identities and mounts are
    preserved. This performs no network request and never creates an app.
    """
    receipt = _pending_path(product, state)
    if not _pending_exists(product, state):
        return False
    pending = desktop._json(receipt)
    if not isinstance(pending, dict) or pending.get('product') != product or pending.get('schema') != 1:
        _guard('native-dmg-pending-record-unverified')
    stage, root = pathlib.Path(pending['stage']), pathlib.Path(pending['root'])
    _parent(root.parent, home)
    if (root.name not in tuple(name + '.app' for name in desktop.PRODUCTS[product]['names'])
            or stage.parent != root.parent or not stage.name.startswith('.modelhub-dmg-')
            or stage.resolve() != stage or stage.is_symlink()):
        _guard('native-dmg-pending-stage-unverified')
    # A crash between complete cleanup and receipt removal leaves no payload.
    if not stage.exists():
        receipt.unlink()
        return True
    stage_info = stage.lstat()
    if (not stat.S_ISDIR(stage_info.st_mode) or stage_info.st_uid != os.geteuid()
            or stage_info.st_mode & 0o077 or list(_fingerprint(stage)[:2]) != pending.get('stageID')):
        _guard('native-dmg-pending-stage-unverified')
    app = desktop.discover(product, home, 'Darwin', command)
    _idle(product, app, command, processes)
    if pending.get('mounted'):
        # A saved /dev/diskN may have been reused after reboot. Reconcile the
        # live image path and private mount before detaching any device.
        output = desktop._checked(command, ['/usr/bin/hdiutil', 'info', '-plist'], 60)
        info = plistlib.loads(output.encode('utf-8'))
        images = info.get('images')
        if not isinstance(images, list):
            _guard('native-dmg-pending-mount-unverified')
        matching = [image for image in images if image.get('image-path') == str(stage / 'release.dmg')]
        if len(matching) > 1:
            _guard('native-dmg-pending-mount-unverified')
        if matching:
            entities = matching[0].get('system-entities', [])
            mounts = [row.get('mount-point') for row in entities if row.get('mount-point')]
            devices = [row.get('dev-entry') for row in entities if row.get('dev-entry')]
            saved_device = pending.get('device')
            if (mounts != [str(stage / 'volume')] or
                    (saved_device and saved_device not in devices)):
                _guard('native-dmg-pending-mount-unverified')
            target = saved_device or str(stage / 'volume')
            if desktop._run(command, ['/usr/bin/hdiutil', 'detach', target], 60)['exit']:
                _guard('native-dmg-detach-failed')
        pending['mounted'] = False
        core.atomic_json(receipt, pending)
    exchange = pending.get('exchange')
    staged = stage / root.name
    if exchange:
        before, candidate = exchange.get('before'), exchange['candidate']
        installed_id = list(_fingerprint(root)[:2]) if root.exists() and not root.is_symlink() else None
        staged_id = list(_fingerprint(staged)[:2]) if staged.exists() and not staged.is_symlink() else None
        if installed_id == candidate['id']:
            if _signature(product, root, command) != candidate['identity']:
                _guard('native-dmg-pending-installed-identity-changed')
            if before:
                # Retirement may have finished just before a crash/failed
                # detach. An absent old bundle needs no further deletion.
                if staged_id is not None and (staged_id != before['id'] or
                        _signature(product, staged, command) != before['identity']):
                    _guard('native-dmg-pending-retired-identity-changed')
            elif staged_id is not None:
                _guard('native-dmg-pending-stage-unverified')
        elif before and installed_id == before['id'] and staged_id == candidate['id']:
            if (_signature(product, root, command) != before['identity'] or
                    _signature(product, staged, command) != candidate['identity']):
                _guard('native-dmg-pending-stage-unverified')
        elif before is None and installed_id is None and staged_id == candidate['id']:
            if _signature(product, staged, command) != candidate['identity']:
                _guard('native-dmg-pending-stage-unverified')
        else:
            _guard('native-dmg-pending-stage-unverified')
    _idle(product, app, command, processes)
    shutil.rmtree(stage)
    receipt.unlink()
    return True


class _VendorRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, product):
        self.product = product

    def redirect_request(self, request, response, code, message, headers, newurl):
        # CDN changes need an explicit vendor allowlist change, not an open
        # redirect or an arbitrary URL supplied by the download response.
        _url(self.product, newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def _download(product, url, destination):
    """Stream a bounded HTTPS object, then independently hash its disk bytes."""
    _url(product, url)
    request = urllib.request.Request(url, headers={'Accept-Encoding': 'identity',
                                                  'User-Agent': 'ModelHub-Kit'})
    opener = urllib.request.build_opener(_VendorRedirect(product))
    total, digest, deadline = 0, hashlib.sha256(), time.monotonic() + 300
    with opener.open(request, timeout=30) as response:
        _url(product, response.url)
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdigit() or not 0 < int(length) <= MAX_DMG):
            _guard('dmg-download-bound-exceeded')
        _space(pathlib.Path(destination).parent, int(length) if length is not None else 1024 * 1024)
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            _guard('dmg-download-content-encoding-unverified')
        with pathlib.Path(destination).open('xb') as output:
            while True:
                if time.monotonic() > deadline:
                    _guard('dmg-download-time-bound-exceeded')
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_DMG:
                    _guard('dmg-download-bound-exceeded')
                _space(pathlib.Path(destination).parent, len(block))
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        if not total or (length is not None and total != int(length)):
            _guard('dmg-download-size-mismatch')
    actual = _digest(destination)
    if actual != digest.hexdigest():
        _guard('dmg-download-readback-mismatch')
    return actual


def _fingerprint(path):
    value = pathlib.Path(path).lstat()
    return (value.st_dev, value.st_ino, value.st_uid, value.st_gid,
            value.st_mode, value.st_mtime_ns, value.st_ctime_ns)


def _parent(path, home):
    path, home = pathlib.Path(path), pathlib.Path(home).absolute()
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        _guard('native-app-parent-unverified')
    # Only the two normal Applications locations are automatic targets.
    if path not in (pathlib.Path('/Applications'), home / 'Applications'):
        _guard('native-app-parent-unverified')
    for entry in (path, *path.parents):
        info = entry.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o002:
            _guard('native-app-parent-owner-unverified')
        if info.st_mode & 0o020 and not (entry == pathlib.Path('/Applications') and info.st_gid in (0, 80)):
            _guard('native-app-parent-owner-unverified')
    if not os.access(path, os.W_OK | os.X_OK):
        _guard('native-app-parent-not-writable')
    return path


def _signature(product, path, command):
    """Verify the sealed bundle and return static Apple signing identity."""
    metadata = desktop.native_metadata(product, path, 'Darwin')
    desktop._checked(command, ['/usr/bin/codesign', '--verify', '--deep', '--strict', str(path)], 120)
    text = desktop._checked(command, ['/usr/bin/codesign', '--display', '--verbose=4', str(path)])
    fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    team, identifier = fields.get('TeamIdentifier', ''), fields.get('Identifier', '')
    if not re.fullmatch(r'[A-Z0-9]{10}', team) or not identifier or 'Signature=adhoc' in text:
        _guard('native-app-signing-identity-unverified')
    if not any(line.startswith('Authority=Developer ID Application:') for line in text.splitlines()):
        _guard('native-app-developer-signature-required')
    bundle = plistlib.loads(desktop._file(pathlib.Path(path) / 'Contents/Info.plist').read_bytes())
    if identifier != bundle.get('CFBundleIdentifier'):
        _guard('native-app-bundle-signature-mismatch')
    # Candidate payload name/version are independently checked by native_metadata.
    # An existing bundle additionally pins both publisher team and bundle ID.
    return {'team': team, 'bundle': identifier, 'version': metadata['version']}


def _current(product, app, root, command):
    if app is None:
        if root.exists() or root.is_symlink():
            _guard('native-installation-changed')
        return None
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        _guard('native-app-root-unverified')
    info = root.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        _guard('native-app-owner-or-permissions-unverified')
    identity = _signature(product, root, command)
    if identity['version'] != app['version']:
        _guard('native-installation-changed')
    return {'file': _fingerprint(root), 'identity': identity}


def _release(product, architecture, fetch):
    if product in CASKS:
        value = desktop._fetch(fetch, 'https://formulae.brew.sh/api/cask/' + CASKS[product] + '.json')
        if value.get('tap') != 'homebrew/cask' or value.get('token') != CASKS[product]:
            _guard('official-cask-metadata-unverified')
        choices = [value]
        variations = value.get('variations', {})
        if not isinstance(variations, dict):
            _guard('official-cask-metadata-unverified')
        choices += [dict(value, **variations[tag]) for tag in MAC_TAGS
                    if isinstance(variations.get(tag), dict)]
        # Do not rewrite another architecture's URL or infer a DMG checksum from
        # a ZIP checksum. Only metadata that actually selects this asset wins.
        for candidate in choices:
            url = candidate.get('url', '')
            if not isinstance(url, str) or not url.lower().endswith('.dmg'):
                continue
            if not re.search(r'(?:darwin[-/]' + architecture + r'|darwin[-/]universal)(?:[-/.]|$)', url):
                continue
            _url(product, url)
            sha = candidate.get('sha256')
            if not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', sha):
                _guard('official-dmg-checksum-unavailable')
            version = str(candidate.get('version', '')).split(',', 1)[0]
            desktop.version_tuple(version)
            return version, url, sha.lower(), 'homebrew-cask-sha256'
    if product not in FEEDS:
        _guard('official-dmg-release-unavailable')
    value = desktop._fetch(fetch, 'https://api2.cursor.sh/updates/api/download/stable/darwin-' +
                           architecture + '/' + FEEDS[product])
    version, url = value.get('version'), value.get('downloadUrl')
    desktop.version_tuple(version)
    _url(product, url)
    sha = value.get('sha256')
    if sha is not None and (not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', sha)):
        _guard('official-dmg-checksum-invalid')
    return version, url, sha.lower() if sha else None, ('vendor-sha256' if sha else 'official-https-readback-sha256')


def plan(product, app, *, home, state, command=core.run, fetch=core.http_json, architecture=None):
    """Plan an owned replacement, or an explicitly selected user installation.

    No filesystem creation/download occurs here. ``state`` is accepted for the
    reconciler interface; the caller writes its single outcome receipt.
    """
    if product not in NAMES:
        _guard('unknown-native-desktop-product')
    if _pending_exists(product, state):
        _guard('native-dmg-pending-transaction')
    architecture = _arch(architecture)
    version, url, sha, source = _release(product, architecture, fetch)
    root = pathlib.Path(app['root']) if app else pathlib.Path(home).absolute() / 'Applications' / NAMES[product]
    # A missing user Applications directory is created only during apply.
    if not root.parent.exists() and app is None:
        if root.parent != pathlib.Path(home).absolute() / 'Applications':
            _guard('native-app-parent-unverified')
        _home = pathlib.Path(home).absolute()
        if _home.resolve() != _home or _home.stat().st_uid != os.getuid() or _home.stat().st_mode & 0o022:
            _guard('native-app-parent-owner-unverified')
    else:
        _parent(root.parent, home)
    if root.name not in tuple(name + '.app' for name in desktop.PRODUCTS[product]['names']):
        _guard('native-app-root-unverified')
    current = _current(product, app, root, command)
    return {'target': version, 'channel': 'signed-dmg', '_dmg': {
        'product': product, 'root': str(root), 'current': current,
        'url': url, 'sha256': sha, 'digestSource': source, 'architecture': architecture}}


def _idle(product, app, command, processes):
    value = desktop.process_state(product, app, 'Darwin', command, processes)
    if value != 'idle':
        _guard('application-active' if value == 'busy' else 'process-state-unknown')


def _tree(root):
    """Bound expanded work; reject special files and escaping bundle symlinks."""
    size, count, root = 0, 0, pathlib.Path(root)
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            entry = pathlib.Path(directory) / name
            info = entry.lstat()
            count += 1
            if count > MAX_ENTRIES:
                _guard('dmg-expanded-bound-exceeded')
            if stat.S_ISLNK(info.st_mode):
                if not entry.resolve().is_relative_to(root):
                    _guard('dmg-bundle-symlink-escape')
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    _guard('dmg-bundle-hardlink-unverified')
                size += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                _guard('dmg-bundle-special-file-unverified')
            if size > MAX_TREE:
                _guard('dmg-expanded-bound-exceeded')
    return size, count


def _rename(source, target, swap):
    """macOS atomic exchange, or exclusive creation; never overwrite by rename."""
    try:
        library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        rename = library.renamex_np
    except (OSError, AttributeError):
        _guard('native-atomic-bundle-transaction-unavailable')
    rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    # xnu bsd/sys/stdio.h: RENAME_SWAP=2, RENAME_EXCL=4.
    if rename(os.fsencode(source), os.fsencode(target), 2 if swap else 4):
        _guard('native-atomic-bundle-transaction-failed')


def apply(product, app, release, *, home, state, command=core.run,
          fetch=core.http_json, processes=None):
    """Apply a plan with fresh identity/idle gates and return static version.

    Tests can replace _download and _rename; neither hook starts an application.
    A transaction that cannot safely retire its exchanged old bundle preserves
    that bundle rather than deleting possibly active code. No routine backup,
    download cache or per-run log is retained.
    """
    data = release['_dmg']
    if product not in NAMES or data.get('product') != product or release.get('channel') != 'signed-dmg':
        _guard('native-dmg-plan-unverified')
    if _pending_exists(product, state):
        _guard('native-dmg-pending-transaction')
    _url(product, data['url'])
    root = pathlib.Path(data['root'])
    expected_root = pathlib.Path(app['root']) if app else pathlib.Path(home).absolute() / 'Applications' / NAMES[product]
    if root != expected_root:
        _guard('native-dmg-plan-unverified')
    _idle(product, app, command, processes)
    if _current(product, app, root, command) != data['current']:
        _guard('native-installation-changed')
    if app and desktop.version_tuple(app['version']) >= desktop.version_tuple(release['target']):
        return {'exit': 0, 'version': app['version']}
    if not root.parent.exists() and app is None:
        # Revalidate the absent directory's owner immediately before creation.
        owner = pathlib.Path(home).absolute()
        if owner.resolve() != owner or owner.stat().st_uid != os.getuid() or owner.stat().st_mode & 0o022:
            _guard('native-app-parent-owner-unverified')
        root.parent.mkdir(mode=0o755)
    _parent(root.parent, home)
    _space(root.parent)
    parent_id = _fingerprint(root.parent)[:4]
    temporary = pathlib.Path(tempfile.mkdtemp(prefix='.modelhub-dmg-', dir=root.parent))
    mount, staged = temporary / 'volume', temporary / root.name
    mount.mkdir(mode=0o700)
    mounted, old_present, detach_target = False, False, str(mount)
    receipt = _pending_path(product, state)
    pending = {'schema': 1, 'product': product, 'stage': str(temporary),
               'stageID': list(_fingerprint(temporary)[:2]), 'root': str(root),
               'mounted': False, 'device': None, 'exchange': None}
    # Registration precedes every large write and mount. One unresolved record
    # blocks subsequent transactions, so exceptional old bundles cannot grow.
    core.atomic_json(receipt, pending)
    try:
        artifact = temporary / 'release.dmg'
        digest = _download(product, data['url'], artifact)
        if data['sha256'] is not None and digest != data['sha256']:
            _guard('official-dmg-checksum-mismatch')
        desktop._checked(command, ['/usr/bin/hdiutil', 'verify', str(artifact)], 120)
        # Always try detach even when attach returned an error after mounting.
        mounted = True
        pending['mounted'] = True
        core.atomic_json(receipt, pending)
        output = desktop._checked(command, ['/usr/bin/hdiutil', 'attach', str(artifact),
            '-readonly', '-nobrowse', '-noautoopen', '-mountpoint', str(mount), '-plist'], 120)
        volume = plistlib.loads(output.encode('utf-8'))
        entities = volume.get('system-entities', [])
        # Detach the image device as a whole, including unexpected secondary
        # volumes. An attach failure still tries the requested private mount.
        devices = [row.get('dev-entry') for row in entities
                   if isinstance(row, dict) and re.fullmatch(r'/dev/disk\d+', str(row.get('dev-entry', '')))]
        if devices:
            detach_target = devices[0]
            pending['device'] = detach_target
            core.atomic_json(receipt, pending)
        mounts = [row.get('mount-point') for row in entities if row.get('mount-point')]
        if mounts != [str(mount)]:
            _guard('dmg-mount-layout-unverified')
        candidates = [entry for entry in mount.iterdir() if entry.suffix == '.app']
        if len(candidates) != 1 or candidates[0].name not in tuple(
                name + '.app' for name in desktop.PRODUCTS[product]['names']):
            _guard('dmg-app-layout-unverified')
        candidate = candidates[0]
        size, count = _tree(candidate)
        identity = _signature(product, candidate, command)
        if identity['version'] != release['target']:
            _guard('dmg-app-version-mismatch')
        current = data['current']
        if current and any(identity[key] != current['identity'][key] for key in ('team', 'bundle')):
            _guard('dmg-app-publisher-or-bundle-changed')
        # The DMG remains present during copying. Account for the full bundle
        # plus filesystem blocks for its entries, and leave a fixed reserve.
        _space(temporary, size + count * 4096)
        desktop._checked(command, ['/usr/bin/ditto', str(candidate), str(staged)], 180)
        _tree(staged)
        if _signature(product, staged, command) != identity:
            _guard('dmg-staged-app-verification-failed')
        _parent(root.parent, home)
        if _fingerprint(root.parent)[:4] != parent_id or _current(product, app, root, command) != current:
            _guard('native-installation-changed')
        _idle(product, app, command, processes)
        old_id = _fingerprint(root)[:2] if app else None
        pending['exchange'] = {
            'before': {'id': list(old_id), 'identity': current['identity']} if app else None,
            'candidate': {'id': list(_fingerprint(staged)[:2]), 'identity': identity}}
        core.atomic_json(receipt, pending)
        _rename(staged, root, bool(app))
        old_present = app is not None
        if _signature(product, root, command) != identity:
            _guard('native-installed-version-verification-failed')
        if old_present:
            if _fingerprint(staged)[:2] != old_id or _signature(product, staged, command) != current['identity']:
                _guard('native-retired-bundle-identity-changed')
            _idle(product, app, command, processes)
            shutil.rmtree(staged)
            old_present = False
        return {'exit': 0, 'version': identity['version'], 'sha256': digest,
                'digestSource': data['digestSource']}
    finally:
        detached = not mounted
        if mounted:
            detached = desktop._run(command, ['/usr/bin/hdiutil', 'detach', detach_target], 60)['exit'] == 0
        if detached:
            pending['mounted'] = False
            core.atomic_json(receipt, pending)
        # Never traverse/delete a still-mounted filesystem, or an old bundle
        # whose identity/activity gate failed. These are exceptional deferrals.
        if detached and not old_present:
            shutil.rmtree(temporary)
            receipt.unlink()
        if not detached:
            _guard('native-dmg-detach-failed')
