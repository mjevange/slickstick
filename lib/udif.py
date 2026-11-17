"""Minimal pure-Python UDIF (Apple .dmg) decompressor → raw disk image.

Enough to turn a recovery ``BaseSystem.dmg`` into a raw HFS+ image we can
loop-mount on Linux — no dmg2img/qemu-img. Handles the block types Apple uses for
these images: raw (0x01), zero-fill (0x00/0x02), and zlib (0x80000005). Other
codecs (bzip2/lzfse/ADC) raise, since recovery BaseSystem images don't use them.

Format: a "koly" trailer (last 512 bytes) points at an XML property list whose
``resource-fork`` → ``blkx`` entries each carry a base64 "mish" block map. Each
mish lists chunks (type, output sector, compressed offset/length into the data
fork). We decompress each chunk to its place in the output. See the UDIF format.
"""
from __future__ import annotations

import plistlib
import struct
import sys
import zlib

SECTOR = 512
KOLY_MAGIC = b"koly"
MISH_MAGIC = b"mish"

T_ZERO = 0x00000000
T_RAW = 0x00000001
T_IGNORE = 0x00000002
T_ZLIB = 0x80000005
T_COMMENT = 0x7FFFFFFF
T_TERM = 0xFFFFFFFF


def decode(dmg_path: str, out_path: str) -> int:
    """Decompress ``dmg_path`` to a raw image at ``out_path``. Returns byte size."""
    with open(dmg_path, "rb") as f:
        f.seek(-512, 2)
        koly = f.read(512)
        if koly[:4] != KOLY_MAGIC:
            raise ValueError("not a UDIF dmg (no koly trailer)")
        data_fork_off = struct.unpack(">Q", koly[24:32])[0]
        xml_off, xml_len = struct.unpack(">QQ", koly[216:232])
        f.seek(xml_off)
        plist = plistlib.loads(f.read(xml_len))

        blkx = plist["resource-fork"]["blkx"]
        total = 0
        with open(out_path, "wb") as out:
            for entry in blkx:
                mish = entry["Data"]  # bytes (plistlib decodes <data> base64)
                if mish[:4] != MISH_MAGIC:
                    raise ValueError("bad blkx (no mish magic)")
                base_sector = struct.unpack(">Q", mish[8:16])[0]
                n_chunks = struct.unpack(">I", mish[200:204])[0]
                pos = 204
                for _ in range(n_chunks):
                    etype, _comment, sector, _scount, coff, clen = struct.unpack(
                        ">IIQQQQ", mish[pos:pos + 40])
                    pos += 40
                    if etype in (T_COMMENT, T_TERM):
                        continue
                    out_off = (base_sector + sector) * SECTOR
                    out.seek(out_off)
                    if etype in (T_ZERO, T_IGNORE):
                        continue  # sparse: leave a hole
                    f.seek(data_fork_off + coff)
                    raw = f.read(clen)
                    if etype == T_RAW:
                        out.write(raw)
                    elif etype == T_ZLIB:
                        out.write(zlib.decompress(raw))
                    else:
                        raise ValueError(f"unsupported UDIF chunk type 0x{etype:08x}")
                    total = max(total, out.tell())
            out.truncate(total)
    return total


if __name__ == "__main__":
    size = decode(sys.argv[1], sys.argv[2])
    print(f"wrote {sys.argv[2]}: {size} bytes")
