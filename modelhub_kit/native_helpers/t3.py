#!/usr/bin/env python3
"""Inspect the official Linux T3 binary for Oracle UEK8's ELF note limit."""
import io
import re
import struct
import sys
import tarfile
import urllib.request
import zlib


PREFIX_BYTES = 1024 * 1024
EXPANDED_BYTES = 16 * 1024 * 1024
UEK8_NOTE_LIMIT = 4 * 1024 * 1024
PLATFORMS = {'aarch64': ('arm64', 183), 'x86_64': ('x64', 62)}


def published_binary(version, architecture):
    platform, machine = PLATFORMS[architecture]
    name = f't3-{version}-linux-{platform}'
    url = f'https://github.com/pingdotgg/t3code/releases/download/v{version}/{name}.tar.gz'
    request = urllib.request.Request(url, headers={'Range': f'bytes=0-{PREFIX_BYTES - 1}'})
    with urllib.request.urlopen(request, timeout=8) as response:
        compressed = response.read(PREFIX_BYTES)
    prefix = zlib.decompressobj(31).decompress(compressed, EXPANDED_BYTES)
    with tarfile.open(fileobj=io.BytesIO(prefix), mode='r|') as archive:
        for entry in archive:
            if entry.name == f'{name}/t3' and entry.isfile():
                with archive.extractfile(entry) as binary:
                    return notes_fit(binary, machine)
    raise ValueError('native binary absent from release prefix')


def notes_fit(binary, machine):
    header = binary.read(64)
    if len(header) != 64 or header[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', header, 18)[0] != machine:
        raise ValueError('unsupported native binary header')
    offset = struct.unpack_from('<Q', header, 32)[0]
    width, count = struct.unpack_from('<HH', header, 54)
    if width != 56 or not 1 <= count <= 128 or not 64 <= offset <= EXPANDED_BYTES - width * count:
        raise ValueError('invalid native program header table')
    skip = offset - len(header)
    entries = binary.read(skip + width * count)[skip:]
    if len(entries) != width * count:
        raise ValueError('truncated native program header table')
    return all(kind != 4 or size <= UEK8_NOTE_LIMIT
               for kind, _, _, _, _, size, _, _ in struct.iter_unpack('<II6Q', entries))


def main():
    try:
        version, architecture = sys.argv[1:]
        if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?', version):
            raise ValueError('unsupported release tag')
        if published_binary(version, architecture):
            return 0
        print('oracle-uek8-elf-note-limit')
    except (OSError, ValueError, KeyError, tarfile.TarError, zlib.error, struct.error):
        print('official-t3-asset-metadata-unavailable')
    return 75


if __name__ == '__main__':
    raise SystemExit(main())
