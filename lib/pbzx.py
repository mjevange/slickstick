"""Pure-Python pbzx decoder — no external `pbzx` tool needed.

An InstallAssistant.pkg stores the installer-app skeleton as a `Payload` member
in Apple's **pbzx** format: the ASCII magic ``pbzx``, a uint64 block-size flag,
then a series of chunks, each ``[uint64 uncompressed_size][uint64 compressed_size]``
followed by that many bytes — an XZ stream (``fd 37 7a 58 5a 00``) we inflate
with the stdlib :mod:`lzma`, or stored bytes when the two sizes are equal.

Decoding yields a cpio archive (``bsdtar``/``cpio`` can then extract it). The big
``SharedSupport.dmg`` is a *separate* plain xar member and does not go through
here — extract it directly with bsdtar.

Not every pkg uses pbzx: some ship a plain **gzip**-compressed Payload (magic
``1f 8b 08``). The ``python -m lib.pbzx`` pipe stage detects this and passes such
bytes straight through, letting the downstream ``bsdtar`` auto-decompress them, so
the same ``bsdtar Payload | python -m lib.pbzx | bsdtar -x`` pipe works for both.
"""
from __future__ import annotations

import lzma
import struct
from typing import BinaryIO

_MAGIC = b"pbzx"
_XZ = b"\xfd7zXZ\x00"


def decode(src: BinaryIO, dst: BinaryIO) -> int:
    """Decode a pbzx stream from ``src`` to ``dst``. Returns bytes written.

    Chunks are bounded (typically 16 MiB), so memory use stays small regardless
    of total size.
    """
    magic = src.read(4)
    if magic != _MAGIC:
        raise ValueError(f"not a pbzx stream (magic={magic!r})")
    return _decode_body(src, dst)


def _decode_body(src: BinaryIO, dst: BinaryIO) -> int:
    """Decode a pbzx stream whose 4-byte magic has already been consumed."""
    src.read(8)  # block-size flag; not needed to decode

    written = 0
    while True:
        header = _read_exact(src, 16)
        if not header:
            break
        if len(header) < 16:
            raise ValueError("truncated pbzx chunk header")
        _usize, csize = struct.unpack(">QQ", header)
        chunk = _read_exact(src, csize)
        if len(chunk) < csize:
            raise ValueError("truncated pbzx chunk body")
        data = lzma.decompress(chunk) if chunk[:6] == _XZ else chunk
        dst.write(data)
        written += len(data)
    return written


def _read_exact(f: BinaryIO, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        block = f.read(n - len(buf))
        if not block:
            break
        buf += block
    return bytes(buf)


if __name__ == "__main__":  # `… | python -m lib.pbzx` as a pipe stage
    import shutil
    import sys
    src, dst = sys.stdin.buffer, sys.stdout.buffer
    magic = src.read(4)
    if magic == _MAGIC:
        _decode_body(src, dst)
    else:
        # Not pbzx — some pkgs ship a gzip-compressed Payload (magic 1f 8b 08)
        # instead. Pass the bytes straight through so the downstream bsdtar
        # auto-decompresses them (gzip + cpio), keeping this one pipe stage
        # correct for both formats.
        dst.write(magic)
        shutil.copyfileobj(src, dst)
    dst.flush()
