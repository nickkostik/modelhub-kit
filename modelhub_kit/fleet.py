"""Explicit public-source delivery; strict SSH and per-boot fences precede writes."""
import ast
import base64
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import stat
import tarfile
import uuid
from .core import GuardError, atomic_json, now, protected, read_json, run, writer

SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8',
               '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
MAX_DELIVERY = 8 * 1024 * 1024
INSTALLERS = ('scripts/install.py', 'scripts/install.sh', 'scripts/install.ps1')
INSTALL_FAILURES = {('install', 'operation-error'), ('initialize', 'command-failed'),
                    ('schedule', 'command-failed'), ('schedule', 'not-ready'),
                    ('prerequisite', 'not-ready')}


def _installer_failure(response, output):
    """Reported-only constants; never use diagnostics as terminal/custody proof."""
    code = response.get('installerExit')
    if type(code) is not int or not -2147483648 <= code <= 4294967295 or code in (0, 75):
        return {}
    found = set()
    # run() already drains into a 64 KiB tail. Reject an unbounded alternate runner.
    if isinstance(output, str) and len(output) <= 65536:
        for line in output.splitlines():
            if len(line) > 128:
                continue
            match = re.fullmatch(r'modelhub-kit-install stage=([a-z-]+) category=([a-z-]+)', line)
            if match and match.groups() in INSTALL_FAILURES:
                found.add(match.groups())
    stage, category = next(iter(found)) if len(found) == 1 else ('installer', 'nonzero-exit')
    return {'installerExit': code, 'installerDiagnostic': {'stage': stage, 'category': category}}


def _source_file(root, relative):
    path = root / relative
    info = protected(path)
    if info.st_size > 1024 * 1024:
        raise GuardError('oversized-delivery-source')
    before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != before:
            raise GuardError('delivery-source-changed')
        value = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    if any((item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns) != before for item in (after, current)):
        raise GuardError('delivery-source-changed')
    return value


def delivery_package(source):
    """Package literal installer inputs only; never walk a checkout or copy state."""
    root = pathlib.Path(source).absolute()
    protected(root, True)
    installer = _source_file(root, 'scripts/install.py')
    declarations = {}
    try:
        for item in ast.parse(installer).body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id in ('FILES', 'OPTIONAL_FILES'):
                        declarations[target.id] = ast.literal_eval(item.value)
    except (SyntaxError, ValueError, TypeError):
        raise GuardError('literal-installer-allowlist-required') from None
    required = declarations.get('FILES')
    optional = declarations.get('OPTIONAL_FILES', ())
    if not isinstance(required, (list, tuple)) or not required or not isinstance(optional, (list, tuple)):
        raise GuardError('literal-installer-allowlist-required')
    files = set(INSTALLERS)
    for relative in tuple(required) + tuple(optional):
        if not isinstance(relative, str):
            raise GuardError('unsafe-installer-source-allowlist')
        # Documentation is optional, and need not travel to runtime hosts.
        if relative in ('LICENSE', 'AGENTS.md', 'README.md') or re.fullmatch(r'docs/[A-Z][A-Z_]*\.md', relative):
            if relative in required:
                raise GuardError('unexpected-required-document')
            continue
        if not re.fullmatch(r'modelhub_kit/(?:[a-z][a-z0-9_]*/)?(?:[a-z_][a-z0-9_]*\.py|tools\.json)', relative):
            raise GuardError('unsafe-installer-source-allowlist')
        if relative in required or (root / relative).exists() or (root / relative).is_symlink():
            files.add(relative)
    if len(files) > 96:
        raise GuardError('delivery-file-budget')
    bodies = {name: _source_file(root, name) for name in sorted(files)}
    if bodies['scripts/install.py'] != installer:
        raise GuardError('delivery-source-changed')
    manifest = {name: {'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)} for name, body in bodies.items()}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, body in bodies.items():
            entry = tarfile.TarInfo(name)
            entry.mode, entry.size, entry.mtime = 0o600, len(body), 0
            archive.addfile(entry, io.BytesIO(body))
    payload = output.getvalue()
    if len(payload) > MAX_DELIVERY:
        raise GuardError('delivery-source-budget')
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    return payload, {'files': manifest, 'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload),
                     'manifestSha256': hashlib.sha256(canonical).hexdigest()}


def _powershell(script):
    return 'powershell.exe -NoProfile -NonInteractive -EncodedCommand ' + base64.b64encode(("$ProgressPreference='SilentlyContinue';\n" + script).encode('utf-16le')).decode('ascii')


def _shell(script):
    return 'sh -c ' + shlex.quote(script)


_UNIX_IDENTITY = r'''actual_os=unknown
actual_variant=
actual_boot=
case "$(uname -s)" in
 Linux) actual_os=linux
   actual_variant=$(sed -n 's/^ID=//p' /etc/os-release | head -n 1 | tr -d '\042\047')
   actual_boot=$(cat /proc/sys/kernel/random/boot_id);;
 Darwin) actual_os=macos; actual_boot=$(/usr/sbin/sysctl -n kern.bootsessionuuid);;
esac
'''
_WINDOWS_IDENTITY = '''if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'different-os-boot' }
$actualBoot=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
'''


def _probe_command(expected):
    if expected == 'windows':
        return _powershell("$ErrorActionPreference='Stop';[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false);" +
                           _WINDOWS_IDENTITY + "@{os='windows';variant=$null;boot=$actualBoot}|ConvertTo-Json -Compress")
    return _shell(_UNIX_IDENTITY + 'printf "modelhub-kit-probe-v1\\n%s\\n%s\\n%s\\n" "$actual_os" "$actual_variant" "$actual_boot"')


def _json_response(output):
    decoder = json.JSONDecoder()
    for index, character in enumerate(output):
        if character == '{':
            try:
                value, end = decoder.raw_decode(output[index:])
                if isinstance(value, dict) and not output[index + end:].strip():
                    return value
            except ValueError:
                pass
    return None


def _probe(expected, output):
    if expected == 'windows':
        value = _json_response(output)
    else:
        lines = output.strip().splitlines()
        value = {'os': lines[-3], 'variant': lines[-2], 'boot': lines[-1]} if len(lines) >= 4 and lines[-4] == 'modelhub-kit-probe-v1' else None
    if (not isinstance(value, dict) or value.get('os') not in ('linux', 'macos', 'windows')
            or not isinstance(value.get('boot'), str) or not re.fullmatch(r'[a-zA-Z0-9.:+-]{1,100}', value['boot'])
            or value.get('variant') and not re.fullmatch(r'[a-z0-9_-]{1,80}', value['variant'])):
        raise GuardError('remote-identity-unavailable')
    return value


def _guard_unix(expected, boot):
    parts = expected.split(':', 1)
    return (_UNIX_IDENTITY + '[ "$actual_os" = ' + shlex.quote(parts[0]) + ' ] || exit 78\n' +
            ('[ "$actual_variant" = ' + shlex.quote(parts[1]) + ' ] || exit 78\n' if len(parts) == 2 else '') +
            '[ "$actual_boot" = ' + shlex.quote(boot) + ' ] || exit 78\n')


def _directories(manifest):
    return sorted({str(parent) for name in manifest for parent in pathlib.PurePosixPath(name).parents if str(parent) != '.'},
                  key=lambda name: (-len(pathlib.PurePosixPath(name).parts), name))


def _delivery_context(operation_id):
    if operation_id is None:
        return {}
    if not isinstance(operation_id, str) or not re.fullmatch(r'[a-f0-9]{32}', operation_id):
        raise GuardError('invalid-fleet-operation-id')
    return {'format': 'modelhub-kit-delivery-v1', 'action': 'deliver-and-enroll',
            'operationId': operation_id, 'operationTerminal': True}


def _unix_delivery(expected, boot, metadata, prerequisites, schedule, operation_id=None):
    files = metadata['files']
    verify = '\n'.join('[ "$(hash_file "$stage/' + name + '")" = ' + item['sha256'] + ' ] || fail delivery-hash-mismatch'
                       for name, item in files.items())
    cleanup = '\n'.join('if [ -e "$stage/' + name + '" ] || [ -L "$stage/' + name + '" ]; then\n'
                        ' [ ! -L "$stage/' + name + '" ] && [ -f "$stage/' + name + '" ] && '
                        '[ "$(hash_file "$stage/' + name + '")" = ' + item['sha256'] + ' ] && '
                        'rm -f "$stage/' + name + '" || cleanup_ok=0\nfi' for name, item in files.items())
    directory_checks = '\n'.join('[ ! -L "$stage/' + name + '" ] || return 1' for name in _directories(files))
    cleanup += r'''
if [ -e "$stage/payload.tar" ] || [ -L "$stage/payload.tar" ]; then
 if [ -n "$payload_snapshot" ] && [ ! -L "$stage/payload.tar" ] &&
    [ "$(file_identity "$stage/payload.tar")" = "$payload_snapshot" ] &&
    [ "$(hash_file "$stage/payload.tar")" = "$payload_digest" ] &&
    [ "$(file_identity "$stage/payload.tar")" = "$payload_snapshot" ]; then
  rm -f "$stage/payload.tar"
 else cleanup_ok=0; fi
fi
''' + '\n'.join('rmdir "$stage/' + directory + '" 2>/dev/null || cleanup_ok=0' for directory in _directories(files))
    options = (' --prerequisites' if prerequisites else '') + (' --schedule' if schedule else '')
    context = _delivery_context(operation_id)
    success = shlex.quote(json.dumps(dict(context, status='installed-or-current', cleanupComplete=True, exitCode=0)))
    busy = shlex.quote(json.dumps(dict(context, status='queued', reason='writer-busy', cleanupComplete=True, exitCode=75)))
    deferred = shlex.quote(json.dumps(dict(context, status='queued', reason='installer-deferred', cleanupComplete=True, exitCode=75))[:-1])
    return _shell('set -eu\n' + _guard_unix(expected, boot) + r'''
fail() { printf '{"status":"queued","reason":"%s"}\n' "$1"; exit 75; }
hash_file() {
 [ ! -L "$1" ] && [ -f "$1" ] || return 1
 if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d ' ' -f 1
 elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d ' ' -f 1
 else return 1; fi
}
command -v tar >/dev/null 2>&1 || fail tar-unavailable
command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1 || fail sha256-unavailable
umask 077
stage=$(mktemp -d "$HOME/.modelhub-kit-delivery.XXXXXXXX")
dir_identity() {
 case "$actual_os" in
  linux) stat -c '%u:%d:%i:%a' "$1";;
  macos) stat -f '%u:%d:%i:%Lp' "$1";;
 esac
}
file_identity() {
 case "$actual_os" in
  linux) stat -c '%u:%d:%i:%a:%h:%s:%y:%z' "$1";;
  macos) stat -f '%u:%d:%i:%Lp:%l:%z:%m:%c:%B' "$1";;
 esac
}
stage_identity=$(dir_identity "$stage")
safe_stage() {
 [ ! -L "$stage" ] && [ -d "$stage" ] && [ "$(dir_identity "$stage")" = "$stage_identity" ] || return 1
''' + directory_checks + r'''
}
cleanup_ok=1
payload_snapshot=
payload_digest=
cleanup() {
 safe_stage || { cleanup_ok=0; return; }
''' + cleanup + r'''
 rmdir "$stage" 2>/dev/null || cleanup_ok=0
}
trap cleanup EXIT
: > "$stage/payload.tar"
payload_born=$(dir_identity "$stage/payload.tar")
received=0
''' + 'while [ "$received" -lt ' + str(metadata['size']) + r''' ]; do
 remaining=$((EXPECTED_SIZE-received))
 chunk=65536
 [ "$remaining" -ge "$chunk" ] || chunk=$remaining
 dd bs="$chunk" count=1 >> "$stage/payload.tar" 2>/dev/null
 next=$(wc -c < "$stage/payload.tar" | tr -d ' ')
 [ "$next" -gt "$received" ] || fail incomplete-delivery
 received=$next
done
'''.replace('EXPECTED_SIZE', str(metadata['size'])) +
        '[ ! -L "$stage/payload.tar" ] && [ "$(dir_identity "$stage/payload.tar")" = "$payload_born" ] || fail delivery-hash-mismatch\n' +
        'payload_snapshot=$(file_identity "$stage/payload.tar")\npayload_digest=$(hash_file "$stage/payload.tar")\n' +
        '[ "$(file_identity "$stage/payload.tar")" = "$payload_snapshot" ] || fail delivery-hash-mismatch\n' +
        '[ "$payload_digest" = ' + metadata['sha256'] + ' ] || fail delivery-hash-mismatch\n' +
        'safe_stage || fail unsafe-delivery-directory\ntar -xf "$stage/payload.tar" -C "$stage"\nsafe_stage || fail unsafe-delivery-directory\n' + verify + '\n' + _guard_unix(expected, boot) +
        'set +e\nsh "$stage/scripts/install.sh" --source "$stage"' + options + ' </dev/null >/dev/null\nresult=$?\nset -e\n' +
        'cleanup\ntrap - EXIT\n[ "$cleanup_ok" = 1 ] || fail delivery-cleanup-incomplete\n' +
        'if [ "$result" = 0 ]; then printf \'%s\\n\' ' + success + '; '
        'elif [ "$result" = 75 ]; then printf \'%s\\n\' ' + busy + '; exit 75; '
        'else printf \'%s, "installerExit": %s}\\n\' ' + deferred + ' "$result"; exit 75; fi\n')


def _windows_loader(script, boot, archive_size=0, archive_sha256=None):
    """Buffer the bounded wire before invoking PowerShell's script engine."""
    if type(archive_size) is not int or not 0 <= archive_size <= MAX_DELIVERY:
        raise GuardError('windows-delivery-archive-budget')
    if archive_sha256 is not None and (not isinstance(archive_sha256, str) or
                                        re.fullmatch(r'[0-9a-f]{64}', archive_sha256) is None):
        raise GuardError('windows-delivery-archive-hash')
    code = script.encode('utf-8')
    prefix = gzip.compress(code, mtime=0)
    if len(code) > 128 * 1024 or len(prefix) > 64 * 1024:
        raise GuardError('windows-delivery-code-budget')
    loader = ("$ErrorActionPreference='Stop';\n" + _WINDOWS_IDENTITY +
              "if($actualBoot -ne '" + boot + "'){throw 'different-boot-identity'}\n" +
              "$prop=[Console].GetProperty('ConsoleInputHandle',[Reflection.BindingFlags]'NonPublic,Static');if(-not $prop){throw 'native-stdin-unavailable'}\n" +
              "$handle=[Microsoft.Win32.SafeHandles.SafeFileHandle]::new([IntPtr]$prop.GetValue($null,$null),$false);if($handle.IsInvalid){throw 'native-stdin-unavailable'}\n" +
              "$modelhubWire=[IO.FileStream]::new($handle,[IO.FileAccess]::Read,65536,$false);$zip=New-Object byte[] " + str(len(prefix)) + ";$offset=0\n" +
              "while($offset -lt $zip.Length){$n=$modelhubWire.Read($zip,$offset,[Math]::Min(65536,$zip.Length-$offset));if($n -le 0){throw 'incomplete-delivery-code'};$offset+=$n}\n" +
              "$hash=[Security.Cryptography.SHA256]::Create();try{$digest=([BitConverter]::ToString($hash.ComputeHash($zip))).Replace('-','').ToLowerInvariant()}finally{$hash.Dispose()}\n" +
              "if($digest -ne '" + hashlib.sha256(prefix).hexdigest() + "'){throw 'delivery-code-hash-mismatch'}\n" +
              "$memory=[IO.MemoryStream]::new($zip,$false);$gzip=[IO.Compression.GZipStream]::new($memory,[IO.Compression.CompressionMode]::Decompress)\n" +
              "$code=New-Object byte[] " + str(len(code)) + ";$offset=0\n" +
              "try{while($offset -lt $code.Length){$n=$gzip.Read($code,$offset,[Math]::Min(65536,$code.Length-$offset));if($n -le 0){throw 'incomplete-delivery-code'};$offset+=$n};if($gzip.ReadByte() -ne -1){throw 'delivery-code-size-mismatch'}}finally{$gzip.Dispose();$memory.Dispose()}\n" +
              "$archive=New-Object byte[] " + str(archive_size) + ";$offset=0\n" +
              "while($offset -lt $archive.Length){$n=$modelhubWire.Read($archive,$offset,[Math]::Min(65536,$archive.Length-$offset));if($n -le 0){throw 'incomplete-delivery'};$offset+=$n}\n" +
              ("$hash=[Security.Cryptography.SHA256]::Create();try{$digest=([BitConverter]::ToString($hash.ComputeHash($archive))).Replace('-','').ToLowerInvariant()}finally{$hash.Dispose()}\n" +
               "if($digest -ne '" + archive_sha256 + "'){throw 'delivery-hash-mismatch'}\n" if archive_sha256 is not None else "") +
              _WINDOWS_IDENTITY + "if($actualBoot -ne '" + boot + "'){throw 'different-boot-identity'}\n" +
              "$modelhubWire=[IO.MemoryStream]::new($archive,$false)\n" +
              "$text=[Text.UTF8Encoding]::new($false,$true).GetString($code);try{& ([ScriptBlock]::Create($text))}finally{$modelhubWire.Dispose()}")
    command = _powershell(loader)
    if len(command) > 7500:
        raise GuardError('windows-delivery-command-budget')
    return command, prefix


def _windows_delivery(expected, boot, metadata, prerequisites, schedule, operation_id=None):
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    encoded = base64.b64encode(json.dumps(metadata['files'], separators=(',', ':')).encode()).decode()
    directories = base64.b64encode(json.dumps(_directories(metadata['files'])).encode()).decode()
    options = (' -Prerequisites' if prerequisites else '') + (' -Schedule' if schedule else '')
    context = _delivery_context(operation_id)
    binding = ('$operationId=' + quote(context['operationId']) + '\n') if context else '$operationId=$null\n'
    script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)\n" +
        _WINDOWS_IDENTITY + "if($actualBoot -ne " + quote(boot) + "){throw 'different-boot-identity'}\n" + binding +
        "$manifest=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "'))|ConvertFrom-Json\n" +
        "$directories=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + directories + "'))|ConvertFrom-Json\n" + r'''
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class ModelHubDeliveryId {
 [StructLayout(LayoutKind.Sequential,Pack=4)] struct Info {
  public uint Attr;public long Created,Accessed,Written;public uint Volume,SizeHigh,SizeLow,Links,IndexHigh,IndexLow;
 }
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFileW(string p,uint a,uint s,IntPtr t,uint d,uint f,IntPtr x);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
 public static string Handle(SafeFileHandle h,bool dir) {
  Info i;if(!GetFileInformationByHandle(h,out i)||(i.Attr&1024)!=0||((i.Attr&16)!=0)!=dir||(!dir&&i.Links!=1))throw new Exception("unsafe-delivery-identity");
  string id=i.Volume+":"+i.IndexHigh+":"+i.IndexLow+":"+i.Created+":"+i.Attr;
  return dir?id:id+":"+i.SizeHigh+":"+i.SizeLow+":"+i.Links;
 }
 public static string Path(string p,bool dir) {
  using(SafeFileHandle h=CreateFileW(p,128,7,IntPtr.Zero,3,0x02200000,IntPtr.Zero)){
   if(h.IsInvalid)throw new Exception("unsafe-delivery-identity");return Handle(h,dir);
  }
 }
}
'@
function Stage-Acl { (Get-Acl -LiteralPath $stage).GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All) }
function Assert-Stage {
 if(-not $stageId -or [ModelHubDeliveryId]::Path($stage,$true) -ne $stageId -or (Stage-Acl) -ne $stageAcl){throw 'unsafe-delivery-directory'}
}
$stage=$null;$stageId=$null;$stageAcl=$null;$payloadId=$null;$payloadSha=$null
$stage=$null;$cleanupOk=$true;$result=75;$installerExit=$null;$reason='delivery-unavailable'
try {
 $tar=Join-Path ([Environment]::SystemDirectory) 'tar.exe'
 if(-not (Test-Path -LiteralPath $tar -PathType Leaf) -or ((Get-Item -LiteralPath $tar).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'tar-unavailable'}
 $stage=Join-Path $env:USERPROFILE ('.modelhub-kit-delivery-'+[Guid]::NewGuid().ToString('N'))
 $null=New-Item -ItemType Directory -Path $stage -ErrorAction Stop
 $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
 $acl=New-Object Security.AccessControl.DirectorySecurity
 $acl.SetAccessRuleProtection($true,$false);$acl.SetOwner($sid)
 $rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
 $acl.AddAccessRule($rule);Set-Acl -LiteralPath $stage -AclObject $acl
 if((Get-Acl -LiteralPath $stage).GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value){throw 'unsafe-delivery-directory'}
 $stageId=[ModelHubDeliveryId]::Path($stage,$true);$stageAcl=Stage-Acl
 Assert-Stage
 $inputStream=$modelhubWire;$buffer=New-Object byte[] 65536
 $file=[IO.File]::Open((Join-Path $stage 'payload.tar'),[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
 try {
''' + '  $remaining=' + str(metadata['size']) + r'''
  while($remaining -gt 0){$n=$inputStream.Read($buffer,0,[Math]::Min($remaining,$buffer.Length));if($n -le 0){throw 'incomplete-delivery'};$file.Write($buffer,0,$n);$remaining-=$n}
  $file.Flush();$payloadId=[ModelHubDeliveryId]::Handle($file.SafeFileHandle,$false)
  $file.Position=0;$hash=[Security.Cryptography.SHA256]::Create()
  try{$payloadSha=([BitConverter]::ToString($hash.ComputeHash($file))).Replace('-','').ToLowerInvariant()}finally{$hash.Dispose()}
  if([ModelHubDeliveryId]::Handle($file.SafeFileHandle,$false) -ne $payloadId){throw 'delivery-hash-mismatch'}
 } finally {$file.Dispose()}
''' + " if($payloadSha -ne " + quote(metadata['sha256']) + " -or [ModelHubDeliveryId]::Path((Join-Path $stage 'payload.tar'),$false) -ne $payloadId){throw 'delivery-hash-mismatch'}\n Assert-Stage\n" +
        " & $tar -xf (Join-Path $stage 'payload.tar') -C $stage\n if($LASTEXITCODE -ne 0){throw 'delivery-extraction-failed'}\n" + r'''
 foreach($entry in $manifest.PSObject.Properties){
  $p=Join-Path $stage $entry.Name;$f=Get-Item -LiteralPath $p
  if(($f.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $f.Length -ne $entry.Value.size -or
     (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value.sha256){throw 'delivery-hash-mismatch'}
 }
''' + _WINDOWS_IDENTITY + " if($actualBoot -ne " + quote(boot) + "){throw 'different-boot-identity'}\n" +
        " $savedErrorAction=$ErrorActionPreference\n try {\n  $ErrorActionPreference='Continue';$LASTEXITCODE=$null\n" +
        "  & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $stage 'scripts/install.ps1') -Source $stage" + options + " > $null\n" +
        "  if($null -ne $LASTEXITCODE){$result=$LASTEXITCODE;$installerExit=$result}\n } finally {$ErrorActionPreference=$savedErrorAction}\n" +
        " if($null -eq $installerExit){throw 'installer-unavailable'}\n" +
        " $reason=if($result -eq 75){'writer-busy'}elseif($result -eq 0){$null}else{'installer-deferred'}\n" + r'''
} catch { if($_.Exception.Message -match '^(?:different-boot-identity|tar-unavailable|incomplete-delivery|delivery-hash-mismatch|delivery-extraction-failed|unsafe-delivery-directory)$'){$reason=$_.Exception.Message} } finally {
 if($stage){try{
  Assert-Stage
  foreach($directory in $directories){$p=Join-Path $stage $directory;if(Test-Path -LiteralPath $p){
   if((Get-Item -LiteralPath $p).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'unsafe-delivery-directory'}
  }}
  foreach($entry in $manifest.PSObject.Properties){
   $p=Join-Path $stage $entry.Name
   if(Test-Path -LiteralPath $p){$f=Get-Item -LiteralPath $p
    if(($f.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
       (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value.sha256){$cleanupOk=$false}
    else{[IO.File]::Delete($p)}
   }
  }
  $p=Join-Path $stage 'payload.tar'
  if(Test-Path -LiteralPath $p){
   if(-not $payloadId -or -not $payloadSha -or [ModelHubDeliveryId]::Path($p,$false) -ne $payloadId -or
      (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() -ne $payloadSha -or
      [ModelHubDeliveryId]::Path($p,$false) -ne $payloadId){$cleanupOk=$false}else{[IO.File]::Delete($p)}
  }
  foreach($directory in $directories){$p=Join-Path $stage $directory;if(Test-Path -LiteralPath $p){[IO.Directory]::Delete($p)}}
  [IO.Directory]::Delete($stage)
 }catch{$cleanupOk=$false}}
}
if(-not $cleanupOk){$result=75;$reason='delivery-cleanup-incomplete'}
$receipt=@{format='modelhub-kit-delivery-v1';action='deliver-and-enroll';operationId=$operationId;operationTerminal=$cleanupOk;status=$(if($result -eq 0){'installed-or-current'}else{'queued'});reason=$reason;cleanupComplete=$cleanupOk;exitCode=$result}
if($null -ne $installerExit -and $installerExit -notin @(0,75)){$receipt.installerExit=$installerExit}
$receipt|ConvertTo-Json -Compress
exit $result
''')
    return _windows_loader(script, boot, metadata['size'], metadata['sha256'])


def _ssh(alias, command, timeout=60, payload=None):
    options = {'timeout': timeout}
    if payload is not None:
        options['input_data'] = payload
    return run(['ssh', '-T', *SSH_OPTIONS, alias, command], **options)


def _maintenance_complete(response):
    if not isinstance(response, dict):
        return False
    accepted = {'completed', 'current', 'updated', 'newer-installation-preserved', 'ready',
                'not-modified', 'absent', 'within-budget', 'cleaned', 'installed', 'applied', 'exported',
                'not-installed', 'missing', 'no-configured-entries', 'no-configured-servers',
                'no-configured-providers', 'no-configured-agents'}
    def settled(value):
        if isinstance(value, dict):
            return ('status' not in value or isinstance(value['status'], str) and value['status'] in accepted) and all(settled(item) for item in value.values())
        return not isinstance(value, list) or all(settled(item) for item in value)
    if not settled(response):
        return False
    if 'status' in response:
        return response['status'] == 'completed'
    return (response.get('version') == 1 and response.get('modelCalls') == 0
            and isinstance(response.get('startedAt'), str) and bool(response['startedAt'])
            and isinstance(response.get('completedAt'), str) and bool(response['completedAt'])
            and all(isinstance(response.get(key), list) and all(isinstance(item, dict) and item.get('status') in accepted
                                                               for item in response[key]) for key in ('tools', 'catalogs'))
            and isinstance(response.get('cache'), dict) and response['cache'].get('status') in accepted)


def _terminal_observed(response, operation_id, enroll, exit_code):
    """An exact completed observation may be queued; success is independent."""
    if (type(exit_code) is not int or not isinstance(response, dict) or response.get('operationId') != operation_id
            or response.get('operationTerminal') is not True):
        return False
    status = response.get('status')
    if not isinstance(status, str):
        return False
    if enroll:
        return (response.get('format') == 'modelhub-kit-delivery-v1'
                and response.get('action') == 'deliver-and-enroll'
                and response.get('cleanupComplete') is True
                and type(response.get('exitCode')) is int and response['exitCode'] == exit_code
                and ((status == 'installed-or-current' and exit_code == 0)
                     or (status == 'queued' and exit_code != 0)))
    if response.get('action') != 'maintain':
        return False
    if response.get('format') == 'modelhub-kit-terminal-v1':
        return exit_code == {'queued': 75, 'refused': 78, 'error': 1}.get(status)
    return (response.get('format') == 'modelhub-kit-maintenance-v1' and type(response.get('version')) is int and response['version'] == 1
            and type(response.get('modelCalls')) is int and response['modelCalls'] == 0
            and all(isinstance(response.get(key), str) and bool(response[key]) for key in ('startedAt', 'completedAt'))
            and all(isinstance(response.get(key), list)
                    and all(isinstance(item, dict) and isinstance(item.get('status'), str) for item in response[key])
                    for key in ('tools', 'catalogs', 'integrations'))
            and isinstance(response.get('cache'), dict) and isinstance(response['cache'].get('status'), str)
            and exit_code == {'completed': 0, 'queued': 75, 'failed': 1}.get(status))


def reconcile(inventory, state, apply=False, *, source=None, enroll=False, prerequisites=False, schedule=False):
    # Separate from the caller's maintenance lock; direct API users serialize
    # the same durable operation fence as the CLI. Children inherit no lock FD.
    if apply:
        with writer(pathlib.Path(state) / 'fleet-operations'):
            return _reconcile(inventory, state, apply, source=source, enroll=enroll,
                              prerequisites=prerequisites, schedule=schedule)
    return _reconcile(inventory, state, apply, source=source, enroll=enroll,
                      prerequisites=prerequisites, schedule=schedule)


def _reconcile(inventory, state, apply=False, *, source=None, enroll=False, prerequisites=False, schedule=False):
    document = read_json(inventory)
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('hosts'), list):
        raise GuardError('version1-fleet-inventory-required')
    if (prerequisites or schedule or source is not None) and not enroll or enroll and source is None:
        raise GuardError('fleet-delivery-requires-enroll-and-source')
    payload, metadata = delivery_package(source) if enroll else (None, None)
    hosts, names = [], set()
    for host in document['hosts']:
        if not isinstance(host, dict):
            raise GuardError('fleet-host-object-required')
        name, expected = host.get('alias', ''), host.get('os', '')
        if (not isinstance(name, str) or not isinstance(expected, str)
                or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', name) or name in names
                or not re.fullmatch(r'(?:linux(?::[a-z0-9_-]+)?|macos|windows)', expected)):
            raise GuardError('unique-trusted-ssh-alias-and-os-required')
        names.add(name)
        boot = host.get('boot')
        if boot is not None and (not isinstance(boot, str) or not re.fullmatch(r'[a-zA-Z0-9.:+-]{1,100}', boot)):
            raise GuardError('safe-boot-identity-required')
        hosts.append((name, expected, boot))
    if len(hosts) > 256:
        raise GuardError('fleet-host-budget')
    fence_path = pathlib.Path(state) / 'fleet-inflight.json'
    fence = read_json(fence_path, {'version': 1, 'operations': {}}) if apply else None
    if apply and (not isinstance(fence, dict) or fence.get('version') != 1
                  or not isinstance(fence.get('operations'), dict)):
        raise GuardError('fleet-inflight-state-unavailable')
    outcomes = []
    for name, expected, boot in hosts:
        if not apply:
            outcomes.append({'host': name, 'status': 'planned', 'expectedOs': expected,
                             'action': 'deliver-and-enroll' if enroll else 'maintain',
                             'prerequisitesAllowed': prerequisites, 'scheduleRequested': schedule})
            continue
        if name in fence['operations']:
            outcomes.append({'host': name, 'status': 'queued', 'reason': 'remote-operation-inflight',
                             'custodyKnown': False})
            continue
        try:
            observed = _ssh(name, _probe_command(expected), timeout=30)
            if observed['exit'] or observed.get('timedOut'):
                outcomes.append({'host': name, 'status': 'queued', 'reason': 'remote-identity-unavailable',
                                 'timedOut': observed.get('timedOut', False)})
                continue
            identity = _probe(expected, observed['output'])
            parts = expected.split(':', 1)
            mismatch = 'different-os-boot' if identity['os'] != parts[0] or len(parts) == 2 and identity.get('variant') != parts[1] else None
            mismatch = mismatch or ('different-boot-identity' if boot and identity['boot'] != boot else None)
            if mismatch:
                outcomes.append({'host': name, 'status': 'queued', 'reason': mismatch})
                continue
            # Always bind this actual observed boot, including inventories without a fixed boot.
            operation_id = uuid.uuid4().hex
            wire_payload = payload
            if enroll:
                if expected == 'windows':
                    remote, prefix = _windows_delivery(expected, identity['boot'], metadata, prerequisites, schedule, operation_id)
                    wire_payload = prefix + payload
                else:
                    remote = _unix_delivery(expected, identity['boot'], metadata, prerequisites, schedule, operation_id)
            else:
                argv = ['--expected-os', expected, '--expected-boot', identity['boot'], '--operation-id', operation_id, 'maintain']
                if expected == 'windows':
                    remote = _powershell("$p=Join-Path $env:LOCALAPPDATA 'modelhub-kit/bin/modelhub-kit.cmd'; & $p " +
                                         ' '.join("'" + argument + "'" for argument in argv) + '; exit $LASTEXITCODE')
                else:
                    remote = 'exec "$HOME/.local/bin/modelhub-kit" ' + shlex.join(argv)
            # Persist before the first mutating byte. Losing SSH observation
            # cannot authorize another installation or prove remote custody.
            fence['operations'][name] = {'id': operation_id, 'startedAt': now(),
                'action': 'deliver-and-enroll' if enroll else 'maintain',
                'os': identity['os'], 'boot': identity['boot']}
            atomic_json(fence_path, fence, durable=True)
            value = _ssh(name, remote, timeout=900 if prerequisites else 600, payload=wire_payload)
            response = _json_response(value['output'])
            delivered = wire_payload is None or value.get('inputSent') == len(wire_payload) and value.get('inputFailed') is False
            terminal = (not value.get('timedOut') and delivered
                        and _terminal_observed(response, operation_id, enroll, value['exit']))
            complete = (terminal and value['exit'] == 0 and isinstance(response, dict)
                        and (response.get('status') == 'installed-or-current' and response.get('cleanupComplete') is True if enroll
                             else _maintenance_complete(response)))
            if terminal:
                del fence['operations'][name]
                atomic_json(fence_path, fence, durable=True)
            reason = response.get('reason') if isinstance(response, dict) else None
            if not delivered:
                reason = 'delivery-input-incomplete'
            elif not enroll and not complete and not reason:
                reason = 'maintenance-incomplete'
            reasons = ('different-os-boot', 'different-boot-identity', 'writer-busy', 'remote-identity-unavailable',
                       'maintenance-incomplete', 'delivery-input-incomplete', 'incomplete-delivery-code', 'delivery-code-hash-mismatch', 'delivery-code-size-mismatch', 'delivery-cleanup-incomplete', 'delivery-hash-mismatch', 'incomplete-delivery', 'tar-unavailable',
                       'sha256-unavailable', 'unsafe-delivery-directory', 'installer-deferred', 'delivery-unavailable', 'delivery-extraction-failed')
            outcomes.append({'host': name, 'status': 'completed' if complete else 'queued', 'action': 'deliver-and-enroll' if enroll else 'maintain',
                             'exit': value['exit'], 'timedOut': value.get('timedOut', False),
                             'reason': reason if reason in reasons else ('different-boot-identity' if value['exit'] == 78 else None),
                             'custodyKnown': terminal})
            if enroll and terminal and not complete:
                outcomes[-1].update(_installer_failure(response, value['output']))
        except GuardError as error:
            outcomes.append({'host': name, 'status': 'queued', 'reason': str(error)})
        except OSError:
            outcomes.append({'host': name, 'status': 'queued', 'reason': 'ssh-unavailable'})
    report = {'version': 1, 'observedAt': now(), 'hosts': outcomes, 'apply': apply}
    if metadata:
        report['delivery'] = metadata
    if apply:
        atomic_json(pathlib.Path(state) / 'fleet-status.json', report)
    return report
