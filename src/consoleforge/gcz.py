"""GCZ (Dolphin) - GameCube / Wii. zlib per blok + Adler32 per blok."""
import os
import struct
import zlib

from common import (ForgeError, check_cancel, human, iter_blocks, noop, ordered_map)

MAGIC = 0xB10BC001
HDR = struct.Struct("<IIQQII")        # magic, subtype, comp_size, data_size, block, nblocks
RAW_FLAG = 1 << 63


def disc_subtype(path):
    """0 = GameCube, 1 = Wii, None = bukan image GC/Wii."""
    with open(path, "rb") as f:
        h = f.read(0x20)
    if len(h) < 0x20:
        return None
    if struct.unpack(">I", h[0x18:0x1C])[0] == 0x5D1C9EA3:
        return 1
    if struct.unpack(">I", h[0x1C:0x20])[0] == 0xC2339F3D:
        return 0
    return None


def compress(src, dst, block=None, level=9, workers=None, progress=None, cancel=None):
    progress = progress or noop
    sub = disc_subtype(src)
    if sub is None:
        raise ForgeError("bukan image GameCube/Wii (magic disc tidak ditemukan)")
    block = block or (0x20000 if sub else 0x4000)
    if block < 0x800 or block & (block - 1):
        raise ForgeError("ukuran blok harus pangkat 2")
    size = os.path.getsize(src)
    n = -(-size // block)

    def work(b):
        z = zlib.compress(b, level)
        return (True, b) if len(z) >= len(b) else (False, z)

    tmp = dst + ".part"
    try:
        with open(tmp, "wb") as fo:
            data_off = HDR.size + n * 12
            fo.write(bytes(data_off))
            ptrs, hashes, pos = [], [], 0
            for i, (raw, data) in enumerate(ordered_map(work, iter_blocks(src, block), workers)):
                check_cancel(cancel)
                ptrs.append(pos | (RAW_FLAG if raw else 0))
                hashes.append(zlib.adler32(data))
                fo.write(data)
                pos += len(data)
                if i % 128 == 0:
                    progress(i / n, f"blok {i:,}/{n:,}")
            if len(ptrs) != n:
                raise ForgeError("jumlah blok tidak sesuai")
            fo.seek(0)
            fo.write(HDR.pack(MAGIC, sub, pos, size, block, n))
            fo.write(struct.pack(f"<{n}Q", *ptrs))
            fo.write(struct.pack(f"<{n}I", *hashes))
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return dst


class GczReader:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        try:
            h = self.f.read(HDR.size)
            if len(h) < HDR.size:
                raise ForgeError("bukan file GCZ")
            magic, self.subtype, self.comp_size, self.size, self.block_size, self.n = HDR.unpack(h)
            if magic != MAGIC:
                raise ForgeError("magic bukan GCZ")
            if self.block_size < 0x800 or self.n != -(-self.size // self.block_size):
                raise ForgeError("header GCZ tidak konsisten")
            raw = self.f.read(self.n * 12)
            if len(raw) != self.n * 12:
                raise ForgeError("tabel GCZ terpotong")
            self.ptrs = struct.unpack(f"<{self.n}Q", raw[:self.n * 8])
            self.hashes = struct.unpack(f"<{self.n}I", raw[self.n * 8:])
            self.data_off = HDR.size + self.n * 12
        except Exception:
            self.f.close()
            raise

    def close(self):
        self.f.close()

    def read_block(self, i):
        bs = self.block_size
        start = self.ptrs[i] & ~RAW_FLAG
        end = (self.ptrs[i + 1] & ~RAW_FLAG) if i + 1 < self.n else self.comp_size
        self.f.seek(self.data_off + start)
        raw = self.f.read(end - start)
        if len(raw) != end - start or zlib.adler32(raw) != self.hashes[i]:
            raise ForgeError(f"blok {i}: hash Adler32 tidak cocok (data korup)")
        if self.ptrs[i] & RAW_FLAG:
            data = raw
        else:
            try:
                data = zlib.decompress(raw)
            except zlib.error as ex:
                raise ForgeError(f"blok {i}: zlib error ({ex})")
        need = bs if i < self.n - 1 else self.size - (self.n - 1) * bs
        if len(data) < need:
            raise ForgeError(f"blok {i}: data terpotong")
        return data[:need]

    def blocks(self):
        for i in range(self.n):
            yield self.read_block(i)

    def info(self):
        fsz = os.path.getsize(self.path)
        raw = sum(1 for p in self.ptrs if p & RAW_FLAG)
        return "\n".join([
            f"File        : {self.path}",
            f"Format      : GCZ ({'Wii' if self.subtype else 'GameCube'}), zlib",
            f"Ukuran asli : {self.size:,} bytes ({human(self.size)})",
            f"Blok        : {self.n:,} x {self.block_size:,} (raw {raw:,})",
            f"Ukuran file : {fsz:,} bytes ({100 * fsz / max(1, self.size):.1f}%)",
        ])
