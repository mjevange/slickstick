#!/usr/bin/env python3
"""Unit tests for SlickStick's pure-Python decoders: lib.udif, lib.pbzx, lib.bless.

Zero-dependency (stdlib ``unittest``). Fixtures are synthesized in-code — no
binary blobs — so the tests are deterministic and fast.

Run:  python3 -m unittest discover -s tests
  or: python3 tests/test_decoders.py
"""
import io
import lzma
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import bless, pbzx, udif  # noqa: E402


# --------------------------------------------------------------------------- #
# UDIF (.dmg) — build tiny valid images and check the decode
# --------------------------------------------------------------------------- #
def _mish(base_sector: int, chunks: list[tuple]) -> bytes:
    """A 'mish' block map. chunks: (etype, out_sector, coff, clen)."""
    hdr = bytearray(204)
    hdr[0:4] = udif.MISH_MAGIC
    struct.pack_into(">Q", hdr, 8, base_sector)
    struct.pack_into(">I", hdr, 200, len(chunks))
    body = b"".join(
        struct.pack(">IIQQQQ", etype, 0, sector, 0, coff, clen)
        for (etype, sector, coff, clen) in chunks
    )
    return bytes(hdr) + body


def _dmg(mishes: list[bytes], data_fork: bytes, koly_magic: bytes = udif.KOLY_MAGIC) -> bytes:
    """Assemble data fork + XML plist + koly trailer into a UDIF image."""
    plist = {"resource-fork": {"blkx": [{"Data": m} for m in mishes]}}
    xml = __import__("plistlib").dumps(plist)
    out = bytearray(data_fork)
    xml_off = len(out)
    out += xml
    koly = bytearray(512)
    koly[0:4] = koly_magic
    struct.pack_into(">Q", koly, 24, 0)              # data_fork_off = 0
    struct.pack_into(">QQ", koly, 216, xml_off, len(xml))
    out += koly
    return bytes(out)


def _decode_bytes(dmg: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.dmg"), os.path.join(d, "out.img")
        with open(src, "wb") as f:
            f.write(dmg)
        udif.decode(src, dst)
        with open(dst, "rb") as f:
            return f.read()


class TestUDIF(unittest.TestCase):
    def test_raw_zero_zlib_roundtrip(self):
        raw = b"A" * 512
        zsrc = b"B" * 1024
        zdata = zlib.compress(zsrc)
        data_fork = raw + zdata
        mish = _mish(0, [
            (udif.T_RAW, 0, 0, 512),                 # sector 0: raw 512 B
            (udif.T_ZERO, 1, 0, 0),                  # sector 1: hole
            (udif.T_ZLIB, 2, 512, len(zdata)),       # sectors 2-3: zlib -> 1024 B
            (udif.T_TERM, 0, 0, 0),                  # terminator (ignored)
        ])
        out = _decode_bytes(_dmg([mish], data_fork))
        self.assertEqual(out, raw + b"\x00" * 512 + zsrc)

    def test_multiple_blkx_entries_with_base_sector(self):
        a, b = b"X" * 512, b"Y" * 512
        data_fork = a + b
        m1 = _mish(0, [(udif.T_RAW, 0, 0, 512), (udif.T_TERM, 0, 0, 0)])
        m2 = _mish(1, [(udif.T_RAW, 0, 512, 512), (udif.T_TERM, 0, 0, 0)])  # base_sector 1
        out = _decode_bytes(_dmg([m1, m2], data_fork))
        self.assertEqual(out, a + b)

    def test_ignore_type_is_a_hole(self):
        raw = b"Z" * 512
        mish = _mish(0, [
            (udif.T_IGNORE, 0, 0, 0),                # hole
            (udif.T_RAW, 1, 0, 512),
        ])
        out = _decode_bytes(_dmg([mish], raw))
        self.assertEqual(out, b"\x00" * 512 + raw)

    def test_rejects_missing_koly(self):
        with self.assertRaises(ValueError):
            _decode_bytes(_dmg([_mish(0, [])], b"", koly_magic=b"XXXX"))

    def test_rejects_bad_mish_magic(self):
        bad = bytearray(_mish(0, [(udif.T_RAW, 0, 0, 4)]))
        bad[0:4] = b"nope"
        with self.assertRaises(ValueError):
            _decode_bytes(_dmg([bytes(bad)], b"data"))

    def test_rejects_unsupported_chunk_type(self):
        T_BZIP2 = 0x80000006
        mish = _mish(0, [(T_BZIP2, 0, 0, 4)])
        with self.assertRaises(ValueError):
            _decode_bytes(_dmg([mish], b"ZZZZ"))


# --------------------------------------------------------------------------- #
# pbzx
# --------------------------------------------------------------------------- #
def _pbzx(pieces: list[bytes], stored: set[int] = frozenset(), flag: int = 0x1000000) -> bytes:
    out = bytearray(b"pbzx")
    out += struct.pack(">Q", flag)
    for i, piece in enumerate(pieces):
        body = piece if i in stored else lzma.compress(piece)
        out += struct.pack(">QQ", len(piece), len(body)) + body
    return bytes(out)


class TestPBZX(unittest.TestCase):
    def _decode(self, data: bytes) -> bytes:
        dst = io.BytesIO()
        pbzx.decode(io.BytesIO(data), dst)
        return dst.getvalue()

    def test_single_xz_chunk_roundtrip(self):
        payload = b"the quick brown fox" * 100
        self.assertEqual(self._decode(_pbzx([payload])), payload)

    def test_multiple_chunks_concatenate(self):
        pieces = [b"alpha" * 50, b"beta" * 60, b"gamma" * 70]
        self.assertEqual(self._decode(_pbzx(pieces)), b"".join(pieces))

    def test_stored_chunk_passthrough(self):
        # A non-XZ chunk (compressed size == bytes) is copied verbatim.
        pieces = [b"not compressed, stored as-is"]
        self.assertEqual(self._decode(_pbzx(pieces, stored={0})), pieces[0])

    def test_rejects_bad_magic(self):
        with self.assertRaises(ValueError):
            self._decode(b"XXXX" + struct.pack(">Q", 0))

    def test_rejects_truncated_body(self):
        good = _pbzx([b"hello world" * 20])
        with self.assertRaises(ValueError):
            self._decode(good[:-5])          # chop the last chunk's body


# --------------------------------------------------------------------------- #
# bless
# --------------------------------------------------------------------------- #
def _sudo_dd_ok() -> bool:
    """True only if sudo can actually dd-write a file — not just authenticate.
    (Sandboxes may allow `sudo -n true` yet block the real write.)"""
    fd, p = tempfile.mkstemp()
    try:
        os.write(fd, b"\x00" * 64)
        os.close(fd)
        r = subprocess.run(
            ["sudo", "-n", "dd", f"of={p}", "bs=1", "seek=0", "count=1",
             "conv=notrunc", "status=none"],
            input=b"X", capture_output=True)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass


class TestBless(unittest.TestCase):
    def test_cnid_is_inode(self):
        with tempfile.NamedTemporaryFile() as t:
            self.assertEqual(bless.cnid(t.name), os.stat(t.name).st_ino)

    def test_offsets(self):
        self.assertEqual(bless.FINDERINFO_ABS, 1104)   # 1024 + 80
        self.assertEqual(bless.FINDERINFO_SIZE, 32)

    @unittest.skipUnless(_sudo_dd_ok(), "needs passwordless sudo (dd on the image)")
    def test_finderinfo_roundtrip(self):
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(b"\x00" * 2048)              # room for VH + finderInfo
            bless.patch_finderinfo(path, {0: 36, 1: 60, 5: 36})
            self.assertEqual(bless.read_finderinfo(path), [36, 60, 0, 0, 0, 36, 0, 0])
            with open(path, "rb") as f:
                f.seek(bless.FINDERINFO_ABS)
                raw = f.read(bless.FINDERINFO_SIZE)
            self.assertEqual(list(struct.unpack(">8I", raw)), [36, 60, 0, 0, 0, 36, 0, 0])
        finally:
            os.unlink(path)

    @unittest.skipUnless(_sudo_dd_ok(), "needs passwordless sudo (reads the image first)")
    def test_patch_rejects_bad_index(self):
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(b"\x00" * 2048)
            with self.assertRaises(ValueError):
                bless.patch_finderinfo(path, {8: 1})   # index 8 is out of range
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
