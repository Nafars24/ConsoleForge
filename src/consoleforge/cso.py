"""CSO (CISO, deflate) dan ZSO (LZ4) - PSP / PS2 (OPL). Blok 2048 byte, index tabel u32."""
import os
import struct
import zlib

import lz4block
from common import (ForgeError, check_cancel, human, iter_blocks, noop, ordered_map)

HDR = struct.Struct("<4sIQIBB2x")          # magic, header_size, total, block, ver, align
MAGICS = {"cso": b"CISO", "zso": b"ZISO"}


def pick_align(total, block):
    n = -(-total // block)
    for a in range(8):
        if 24 + (n + 1) * 4 + n * (block + (1 << a)) < (1 << 31) << a:
            return a
    raise ForgeError("file terlalu besar untuk CSO/ZSO")


def compress(src, dst, fmt="cso", block=2048, level=9, workers=None, align=None,
             progress=None, cancel=None):
    progress = progress or noop
    if fmt not in MAGICS:
        raise ForgeError("format harus cso atau zso")
    total = os.path.getsize(src)
    if total == 0:
        raise ForgeError("input kosong")
    if block < 512 or block & (block - 1):
        raise ForgeError("ukuran blok harus pangkat 2 (>= 512)")
    n = -(-total // block)
    a = pick_align(total, block) if align is None else align
    lz = fmt == "zso"

    def work(b):
        if lz:
            z = lz4block.compress(b)
            return (True, b) if len(z) + 12 >= len(b) else (False, z)
        c = zlib.compressobj(level, zlib.DEFLATED, -15)
        z = c.compress(b) + c.flush()
        return (True, b) if len(z) >= len(b) else (False, z)

    tmp = dst + ".part"
    try:
        with open(tmp, "wb") as fo:
            fo.write(HDR.pack(MAGICS[fmt], 24, total, block, 1, a))
            fo.write(bytes((n + 1) * 4))
            pos, al = 24 + (n + 1) * 4, 1 << a
            pad = (-pos) % al
            fo.write(bytes(pad))
            pos += pad
            idx = []
            for i, (plain, data) in enumerate(ordered_map(work, iter_blocks(src, block), workers)):
                check_cancel(cancel)
                idx.append((pos >> a) | (0x80000000 if plain else 0))
                fo.write(data)
                pos += len(data)
                pad = (-pos) % al
                if pad:
                    fo.write(bytes(pad))
                    pos += pad
                if i % 256 == 0:
                    progress(i / n, f"blok {i:,}/{n:,}")
            idx.append(pos >> a)
            if len(idx) != n + 1:
                raise ForgeError("jumlah blok tidak sesuai")
            fo.seek(24)
            fo.write(struct.pack(f"<{n + 1}I", *idx))
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return dst


class CsoReader:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        try:
            h = self.f.read(24)
            if len(h) < 24:
                raise ForgeError("bukan file CSO/ZSO")
            magic, hs, self.size, self.block_size, self.ver, self.align = HDR.unpack(h)
            if magic == b"CISO":
                self.fmt = "cso"
            elif magic == b"ZISO":
                self.fmt = "zso"
            else:
                raise ForgeError("magic bukan CISO/ZISO")
            bs = self.block_size
            if bs < 512 or bs & (bs - 1) or self.align > 12:
                raise ForgeError("header CSO tidak valid")
            self.n = -(-self.size // bs)
            raw = self.f.read((self.n + 1) * 4)
            if len(raw) != (self.n + 1) * 4:
                raise ForgeError("index CSO terpotong")
            self.idx = struct.unpack(f"<{self.n + 1}I", raw)
        except Exception:
            self.f.close()
            raise

    def close(self):
        self.f.close()

    def read_block(self, i):
        a, bs = self.align, self.block_size
        e = self.idx[i]
        pos = (e & 0x7FFFFFFF) << a
        size = ((self.idx[i + 1] & 0x7FFFFFFF) << a) - pos
        if size <= 0:
            raise ForgeError(f"blok {i}: ukuran index tidak valid")
        self.f.seek(pos)
        if e & 0x80000000:                       # blok plain
            data = self.f.read(bs)
        else:
            raw = self.f.read(size)
            try:
                if self.fmt == "cso":
                    data = zlib.decompressobj(-15).decompress(raw, bs)
                else:
                    data = lz4block.decompress(raw, bs)
            except zlib.error as ex:
                raise ForgeError(f"blok {i}: data korup ({ex})")
        need = bs if i < self.n - 1 else self.size - (self.n - 1) * bs
        if len(data) < need:
            raise ForgeError(f"blok {i}: data terpotong/korup")
        return data[:need]

    def blocks(self):
        for i in range(self.n):
            yield self.read_block(i)

    def info(self):
        plain = sum(1 for e in self.idx[:-1] if e & 0x80000000)
        fsz = os.path.getsize(self.path)
        return "\n".join([
            f"File        : {self.path}",
            f"Format      : {self.fmt.upper()} v{self.ver}  ({'deflate' if self.fmt == 'cso' else 'LZ4'})",
            f"Ukuran asli : {self.size:,} bytes ({human(self.size)})",
            f"Block / align: {self.block_size} / {1 << self.align}",
            f"Blok        : {self.n:,} (plain {plain:,})",
            f"Ukuran file : {fsz:,} bytes ({100 * fsz / max(1, self.size):.1f}%)",
        ])
