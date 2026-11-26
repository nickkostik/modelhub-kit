#!/usr/bin/env python3
"""Package a checked public source revision with one prebuilt catalog executable."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile


TARGETS = {
    "x86_64-unknown-linux-gnu": ("linux", "x86_64", "linux-x64"),
    "aarch64-unknown-linux-gnu": ("linux", "aarch64", "linux-arm64"),
    "x86_64-apple-darwin": ("macos", "x86_64", "macos-x64"),
    "aarch64-apple-darwin": ("macos", "aarch64", "macos-arm64"),
    "x86_64-pc-windows-gnu": ("windows", "x86_64", "windows-x64"),
    "x86_64-pc-windows-msvc": ("windows", "x86_64", "windows-x64"),
}
PUBLIC_INSTALL_SOURCE = (
    "LICENSE", "README.md", "AGENTS.md", "docs", "modelhub_kit",
    "scripts/install.py", "scripts/install.sh", "scripts/install.ps1",
)
LINUX_ARM64_GLIBC = (2, 28)


def matches_machine(image: bytes, system: str, architecture: str) -> bool:
    x64 = architecture == "x86_64"
    if system == "linux":
        return image.startswith(b"\x7fELF\x02\x01") and image[18:20] == (
            b"\x3e\x00" if x64 else b"\xb7\x00")
    if system == "macos":
        return image[:8] == (b"\xcf\xfa\xed\xfe\x07\x00\x00\x01" if x64
                            else b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01")
    pointer = int.from_bytes(image[60:64], "little")
    return image.startswith(b"MZ") and pointer >= 64 and image[pointer:pointer + 6] == b"PE\x00\x00\x64\x86"


def check_native_abi(image: bytes, system: str, architecture: str) -> None:
    if (system, architecture) != ("linux", "aarch64"):
        return
    readelf = shutil.which("readelf") or shutil.which("llvm-readelf") or shutil.which("greadelf")
    if not readelf:
        raise ValueError("A readelf implementation is required to verify Linux ARM64 releases")
    with tempfile.TemporaryDirectory(prefix="modelhub-kit-abi-") as scratch:
        binary = Path(scratch) / "modelhub-kit-catalog"
        binary.write_bytes(image)
        output = subprocess.check_output(
            [readelf, "--version-info", "--wide", str(binary)], text=True, env=dict(os.environ, LC_ALL="C"),
        )
    versions = re.findall(r"\bName: GLIBC_([0-9]+(?:\.[0-9]+)+)\b", output)
    if not versions:
        raise ValueError("Linux ARM64 release has no verifiable glibc version requirements")
    required = max(tuple(map(int, version.split("."))) for version in versions)
    if required > LINUX_ARM64_GLIBC:
        version = ".".join(map(str, required))
        limit = ".".join(map(str, LINUX_ARM64_GLIBC))
        raise ValueError(f"Linux ARM64 release requires glibc {version}, above the supported baseline {limit}")


def pack(repository: Path, ref: str, target: str, binary: Path, output: Path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    commit = git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    version = re.search(rb"^__version__ = ['\"](\d+\.\d+\.\d+)['\"]\s*$",
                        git("show", commit + ":modelhub_kit/__init__.py"), re.MULTILINE)
    if version is None:
        raise ValueError("The selected source has no public version")
    version = version[1].decode()
    system, architecture, name = TARGETS[target]
    filename = "native/modelhub-kit-catalog" + (".exe" if system == "windows" else "")
    identity = f"modelhub-kit-catalog-v{version}+{commit}:{target}".encode()
    with binary.open("rb") as stream:
        image = stream.read(32 * 1024 * 1024 + 1)
    if not 0 < len(image) <= 32 * 1024 * 1024 or identity not in image or not matches_machine(image, system, architecture):
        raise ValueError("The executable does not match the selected source revision and target")
    check_native_abi(image, system, architecture)
    descriptor = json.dumps({
        "schemaVersion": 1, "version": version, "platform": system, "architecture": architecture,
        "filename": filename, "sha256": hashlib.sha256(image).hexdigest(), "gitCommit": commit,
    }, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    prefix = f"modelhub-kit-v{version}/"
    archive = git("archive", "--format=tar", "--prefix=" + prefix, commit, *PUBLIC_INSTALL_SOURCE)
    content = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        members = source.getmembers()
        if any(not (member.isdir() or member.isfile()) for member in members):
            raise ValueError("The selected public source contains a special file")
        if system == "windows":
            extension = ".zip"
            with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as result:
                for member in members:
                    if member.isfile():
                        with source.extractfile(member) as file:
                            result.writestr(member.name, file.read())
                result.writestr(prefix + filename, image)
                result.writestr(prefix + "native/bundle.json", descriptor)
        else:
            extension = ".tar.gz"
            with tarfile.open(fileobj=content, mode="w:gz") as result:
                for member in members:
                    if member.isfile():
                        with source.extractfile(member) as file:
                            result.addfile(member, file)
                    else:
                        result.addfile(member)
                for relative, payload, mode in ((filename, image, 0o755), ("native/bundle.json", descriptor, 0o644)):
                    member = tarfile.TarInfo(prefix + relative)
                    member.size, member.mode = len(payload), mode
                    result.addfile(member, io.BytesIO(payload))
    destination = output / f"modelhub-kit-v{version}-{name}{extension}"
    with destination.open("xb") as file:
        file.write(content.getbuffer())
    return destination, hashlib.sha256(content.getbuffer()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    destination, digest = pack(repository, args.ref, args.target, args.binary, args.output)
    print(f"{digest}  {destination.name}")


if __name__ == "__main__":
    main()
