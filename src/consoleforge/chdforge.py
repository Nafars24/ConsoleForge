#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CHDForge - toolkit CHD v5 murni Python (tanpa chdman).

  BIN/CUE -> CHD    ISO -> CHD (DVD atau CD)    GDI -> CHD (Dreamcast)
  CHD -> ISO / BIN+CUE / GDI  (bit-exact)       verify / info

Jalankan tanpa argumen untuk membuka GUI:   python chdforge.py
CLI:  python chdforge.py convert game.cue -o out/
      python chdforge.py extract game.chd -o out/
      python chdforge.py verify  game.chd
"""
from __future__ import annotations

import argparse
import binascii
import hashlib
import heapq
import itertools
import lzma
import operator
import os
import re
import struct
import sys
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor

__version__ = "0.1.0"


try:                                   # dipakai bersama ConsoleForge; tetap jalan standalone
    from common import ForgeError as ChdError, Cancelled
except ImportError:
    class ChdError(Exception):
        pass

    class Cancelled(Exception):
        pass


# --------------------------------------------------------------------------
# Konstanta
# --------------------------------------------------------------------------
CD_MAIN, CD_SUB, CD_FRAME = 2352, 96, 2448
FRAMES_PER_HUNK = 8
CD_HUNK = CD_FRAME * FRAMES_PER_HUNK          # 19584
DVD_HUNK = 16384
HEADER_LEN = 124
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
GD_HD_START_LBA = 45000

CT_NONE, CT_SELF, CT_PARENT, CT_RLE_SMALL, CT_RLE_LARGE = 4, 5, 6, 7, 8
CT_SELF0, CT_SELF1, CT_PARENT_SELF, CT_PARENT0, CT_PARENT1 = 9, 10, 11, 12, 13

TRACK_DATASIZE = {
    "MODE1": 2048, "MODE1_RAW": 2352, "MODE2": 2336, "MODE2_FORM1": 2048,
    "MODE2_FORM2": 2324, "MODE2_FORM_MIX": 2336, "MODE2_RAW": 2352, "AUDIO": 2352,
}
CUE_TYPE = {
    "MODE1": "MODE1/2048", "MODE1_RAW": "MODE1/2352", "MODE2": "MODE2/2336",
    "MODE2_FORM1": "MODE2/2048", "MODE2_FORM2": "MODE2/2324",
    "MODE2_FORM_MIX": "MODE2/2336", "MODE2_RAW": "MODE2/2352", "AUDIO": "AUDIO",
}
CUE_TO_TYPE = {v: k for k, v in CUE_TYPE.items() if k != "MODE2_FORM_MIX"}


def tag32(s: str) -> int:
    return struct.unpack(">I", s.encode("ascii"))[0]


def tagstr(v: int) -> str:
    return struct.pack(">I", v).decode("latin-1")


def crc16(data) -> int:
    # CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) = yang dipakai CHD
    return binascii.crc_hqx(data, 0xFFFF)


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


# --------------------------------------------------------------------------
# Bitstream + Huffman (dipakai oleh compressed map CHD v5)
# --------------------------------------------------------------------------
class BitWriter:
    def __init__(self):
        self.buf = bytearray()
        self.acc = 0
        self.n = 0

    def write(self, val: int, bits: int):
        if bits == 0:
            return
        self.acc = (self.acc << bits) | (val & ((1 << bits) - 1))
        self.n += bits
        while self.n >= 8:
            self.n -= 8
            self.buf.append((self.acc >> self.n) & 0xFF)
        self.acc &= (1 << self.n) - 1

    def getvalue(self) -> bytes:
        out = bytearray(self.buf)
        if self.n:
            out.append((self.acc << (8 - self.n)) & 0xFF)
        return bytes(out)


class BitReader:
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0
        self.acc = 0
        self.n = 0

    def _fill(self, bits: int):
        while self.n < bits:
            b = self.d[self.i] if self.i < len(self.d) else 0
            self.i += 1
            self.acc = (self.acc << 8) | b
            self.n += 8

    def read(self, bits: int) -> int:
        if bits == 0:
            return 0
        self._fill(bits)
        self.n -= bits
        v = (self.acc >> self.n) & ((1 << bits) - 1)
        self.acc &= (1 << self.n) - 1
        return v

    def huff(self, table) -> int:
        self._fill(8)
        sym, nb = table[(self.acc >> (self.n - 8)) & 0xFF]
        if nb == 0:
            raise ChdError("kode huffman tidak valid di map")
        self.n -= nb
        self.acc &= (1 << self.n) - 1
        return sym


def huff_assign(numbits):
    """Canonical Huffman code assignment (identik dengan MAME/libchdr)."""
    hist = [0] * 33
    for nb in numbits:
        hist[nb] += 1
    curstart = 0
    for codelen in range(32, 0, -1):
        total = curstart + hist[codelen]
        nextstart = total >> 1
        if codelen != 1 and nextstart * 2 != total:
            raise ChdError("pohon huffman tidak valid")
        hist[codelen] = curstart
        curstart = nextstart
    codes = [0] * len(numbits)
    for i, nb in enumerate(numbits):
        if nb > 0:
            codes[i] = hist[nb]
            hist[nb] += 1
    return codes


def huff_table(lens, maxbits=8):
    codes = huff_assign(lens)
    tab = [(0, 0)] * (1 << maxbits)
    for s, nb in enumerate(lens):
        if nb:
            if nb > maxbits:
                raise ChdError("kode huffman > 8 bit")
            base = codes[s] << (maxbits - nb)
            for j in range(1 << (maxbits - nb)):
                tab[base + j] = (s, nb)
    return tab


def _huff_lengths_raw(freq):
    heap = [(f, i, [i]) for i, f in enumerate(freq) if f > 0]
    heapq.heapify(heap)
    lengths = [0] * len(freq)
    counter = len(freq)
    while len(heap) > 1:
        f1, _, s1 = heapq.heappop(heap)
        f2, _, s2 = heapq.heappop(heap)
        for s in s1 + s2:
            lengths[s] += 1
        heapq.heappush(heap, (f1 + f2, counter, s1 + s2))
        counter += 1
    return lengths


def huff_lengths(freq, maxbits=8):
    f = list(freq)
    for i in range(len(f)):                    # minimal 2 simbol supaya kode lengkap
        if sum(1 for x in f if x) >= 2:
            break
        if not f[i]:
            f[i] = 1
    while True:
        lens = _huff_lengths_raw(f)
        if max(lens) <= maxbits:
            return lens
        f = [(x + 1) // 2 if x else 0 for x in f]   # ratakan sampai muat 8 bit


# --------------------------------------------------------------------------
# CD ECC (P/Q parity) - dipakai cdlz/cdzl untuk membuang & membangun ulang ECC
# --------------------------------------------------------------------------
_F = bytes(((i << 1) ^ (0x11D if i & 0x80 else 0)) & 0xFF for i in range(256))
_Bl = bytearray(256)
for _i in range(256):
    _Bl[_i ^ _F[_i]] = _i
_B = bytes(_Bl)


def _swar(n):
    return int.from_bytes(b"\x7f" * n, "big"), int.from_bytes(b"\x01" * n, "big")


_M_P, _M_Q = _swar(86), _swar(52)
_Q_GET = operator.itemgetter(*[12 + ((m >> 1) * 86 + (m & 1) + 88 * k) % 2236
                               for k in range(43) for m in range(52)])


def _parity(rows, n, masks):
    m7, m1 = masks
    a = b = 0
    for r in rows:
        a ^= r
        b ^= r
        a = ((a & m7) << 1) ^ (((a >> 7) & m1) * 0x1D)
    x = ((((a & m7) << 1) ^ (((a >> 7) & m1) * 0x1D)) ^ b).to_bytes(n, "big").translate(_B)
    return x, (int.from_bytes(x, "big") ^ b).to_bytes(n, "big")


def _ecc_calc(sec):
    """Hitung (P, Q) dari sektor 2352 byte. P dihitung dari data, Q dari data+P."""
    rows = [int.from_bytes(sec[12 + 86 * k:98 + 86 * k], "big") for k in range(24)]
    p0, p1 = _parity(rows, 86, _M_P)
    tmp = bytearray(sec)
    tmp[0x81C:0x81C + 86] = p0
    tmp[0x81C + 86:0x81C + 172] = p1
    g = bytes(_Q_GET(tmp))
    rows = [int.from_bytes(g[52 * k:52 * k + 52], "big") for k in range(43)]
    q0, q1 = _parity(rows, 52, _M_Q)
    return p0 + p1, q0 + q1


def ecc_generate(sec: bytearray):
    p, q = _ecc_calc(sec)
    sec[0x81C:0x81C + 172] = p
    sec[0x8C8:0x8C8 + 104] = q


def ecc_verify(sec) -> bool:
    p, q = _ecc_calc(sec)
    return sec[0x81C:0x81C + 172] == p and sec[0x8C8:0x8C8 + 104] == q


# --------------------------------------------------------------------------
# Codec
# --------------------------------------------------------------------------
def _deflate(b: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


def _inflate(b: bytes, n: int) -> bytes:
    out = zlib.decompressobj(-15).decompress(b, n)
    if len(out) != n:
        raise ChdError("zlib: ukuran hasil dekompresi tidak cocok")
    return out


def _lzma_dict(reduce: int) -> int:
    # sama seperti LzmaEncProps_Normalize(level 9, reduceSize=hunkbytes)
    for i in range(11, 31):
        if reduce <= (2 << i):
            return 2 << i
        if reduce <= (3 << i):
            return 3 << i
    return 1 << 26


def _lzma_filter(reduce, preset=False):
    f = {"id": lzma.FILTER_LZMA1, "dict_size": _lzma_dict(reduce), "lc": 3, "lp": 0, "pb": 2}
    if preset:
        f["preset"] = 9
    return [f]


def _lzma_comp(b: bytes, reduce: int) -> bytes:
    c = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=_lzma_filter(reduce, True))
    return c.compress(b) + c.flush()


def _lzma_decomp(b: bytes, n: int, reduce: int) -> bytes:
    d = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=_lzma_filter(reduce))
    out = d.decompress(b, max_length=n)
    if len(out) != n:
        raise ChdError("lzma: ukuran hasil dekompresi tidak cocok")
    return out


class ZlibCodec:
    tag = "zlib"

    def compress(self, h):
        return _deflate(h)

    def decompress(self, c, n):
        return _inflate(c, n)


class LzmaCodec:
    tag = "lzma"

    def __init__(self, hunkbytes):
        self.hb = hunkbytes

    def compress(self, h):
        return _lzma_comp(h, self.hb)

    def decompress(self, c, n):
        return _lzma_decomp(c, n, self.hb)


class CdCodec:
    """cdlz (LZMA) / cdzl (zlib): data sektor dikompres, subcode di-deflate, ECC dibuang."""

    def __init__(self, lz: bool):
        self.lz = lz
        self.tag = "cdlz" if lz else "cdzl"

    def _base_c(self, b, frames):
        return _lzma_comp(b, frames * CD_MAIN) if self.lz else _deflate(b)

    def _base_d(self, b, n, frames):
        return _lzma_decomp(b, n, frames * CD_MAIN) if self.lz else _inflate(b, n)

    def compress(self, hunk):
        frames = len(hunk) // CD_FRAME
        ecc = bytearray((frames + 7) // 8)
        main, sub = bytearray(), bytearray()
        for i in range(frames):
            fr = hunk[i * CD_FRAME:(i + 1) * CD_FRAME]
            sec = bytearray(fr[:CD_MAIN])
            if sec[:12] == SYNC and ecc_verify(sec):
                ecc[i >> 3] |= 1 << (i & 7)
                sec[0:12] = bytes(12)
                sec[0x81C:0x930] = bytes(0x114)
            main += sec
            sub += fr[CD_MAIN:]
        base = self._base_c(bytes(main), frames)
        if len(base) >= 65536:
            return None
        return bytes(ecc) + struct.pack(">H", len(base)) + base + _deflate(bytes(sub))

    def decompress(self, c, n):
        frames = n // CD_FRAME
        eb = (frames + 7) // 8
        cb = 2 if n < 65536 else 3
        hb = eb + cb
        cbase = int.from_bytes(c[eb:eb + cb], "big")
        base = self._base_d(c[hb:hb + cbase], frames * CD_MAIN, frames)
        sub = _inflate(c[hb + cbase:], frames * CD_SUB)
        out = bytearray(n)
        for i in range(frames):
            sec = bytearray(base[i * CD_MAIN:(i + 1) * CD_MAIN])
            if (c[i >> 3] >> (i & 7)) & 1:
                sec[0:12] = SYNC
                ecc_generate(sec)
            o = i * CD_FRAME
            out[o:o + CD_MAIN] = sec
            out[o + CD_MAIN:o + CD_FRAME] = sub[i * CD_SUB:(i + 1) * CD_SUB]
        return bytes(out)


class CdFlacCodec:
    """cdfl (hanya decode): data sektor = FLAC stereo 16-bit (disimpan big-endian), subcode = deflate."""
    tag = "cdfl"

    def compress(self, h):
        return None

    def decompress(self, c, n):
        try:
            import flac
        except ImportError:
            raise ChdError("modul flac.py tidak ditemukan (dibutuhkan untuk codec cdfl)")
        frames = n // CD_FRAME
        chans, off = flac.decode_stream(c, frames * CD_MAIN // 4)
        main = flac.pcm_bytes(chans, True)
        sub = _inflate(c[off:], frames * CD_SUB)
        out = bytearray(n)
        for i in range(frames):
            o = i * CD_FRAME
            out[o:o + CD_MAIN] = main[i * CD_MAIN:(i + 1) * CD_MAIN]
            out[o + CD_MAIN:o + CD_FRAME] = sub[i * CD_SUB:(i + 1) * CD_SUB]
        return bytes(out)


class FlacCodec:
    """flac (hanya decode): byte pertama 'L'/'B' = endianness, lalu frame FLAC stereo 16-bit."""
    tag = "flac"

    def compress(self, h):
        return None

    def decompress(self, c, n):
        try:
            import flac
        except ImportError:
            raise ChdError("modul flac.py tidak ditemukan (dibutuhkan untuk codec flac)")
        if c[:1] not in (b"L", b"B"):
            raise ChdError("codec flac: penanda endianness tidak dikenal")
        chans, _ = flac.decode_stream(c[1:], n // 4)
        return flac.pcm_bytes(chans, c[:1] == b"B")


def make_codec(tag: str, hunkbytes: int):
    if tag == "zlib":
        return ZlibCodec()
    if tag == "lzma":
        return LzmaCodec(hunkbytes)
    if tag == "cdlz":
        return CdCodec(True)
    if tag == "cdzl":
        return CdCodec(False)
    if tag == "cdfl":
        return CdFlacCodec()
    if tag == "flac":
        return FlacCodec()
    raise ChdError(f"codec '{tag}' belum didukung (huff/avhu). Didukung: lzma, zlib, flac, cdlz, cdzl, cdfl.")


# --------------------------------------------------------------------------
# Map v5
# --------------------------------------------------------------------------
def _raw_map(entries) -> bytes:
    return b"".join(struct.pack(">B", t) + l.to_bytes(3, "big") + o.to_bytes(6, "big") +
                    struct.pack(">H", c) for t, l, o, c in entries)


def encode_map(entries, firstoffs: int) -> bytes:
    n = len(entries)
    maxlen = max([e[1] for e in entries if e[0] < 4] or [0])
    lengthbits = max(1, maxlen.bit_length())
    selfbits = max(1, (n - 1).bit_length())
    freq = [0] * 16
    for e in entries:
        freq[e[0]] += 1
    lens = huff_lengths(freq)
    codes = huff_assign(lens)
    bw = BitWriter()
    for nb in lens:                       # pohon: 4 bit per simbol, "1" di-escape jadi "1,1"
        if nb == 1:
            bw.write(1, 4)
            bw.write(1, 4)
        else:
            bw.write(nb, 4)
    for e in entries:
        bw.write(codes[e[0]], lens[e[0]])
    for t, l, o, c in entries:
        if t < 4:
            bw.write(l, lengthbits)
            bw.write(c, 16)
        elif t == CT_NONE:
            bw.write(c, 16)
        elif t == CT_SELF:
            bw.write(o, selfbits)
    body = bw.getvalue()
    hdr = (struct.pack(">I", len(body)) + firstoffs.to_bytes(6, "big") +
           struct.pack(">H", crc16(_raw_map(entries))) + bytes([lengthbits, selfbits, 0, 0]))
    return hdr + body


def decode_map(f, mapoff: int, hunkcount: int, hunkbytes: int):
    f.seek(mapoff)
    h = f.read(16)
    if len(h) < 16:
        raise ChdError("map terpotong")
    mapbytes = struct.unpack(">I", h[:4])[0]
    firstoffs = int.from_bytes(h[4:10], "big")
    mapcrc = struct.unpack(">H", h[10:12])[0]
    lengthbits, selfbits = h[12], h[13]
    br = BitReader(f.read(mapbytes))
    lens = []
    while len(lens) < 16:
        v = br.read(4)
        if v != 1:
            lens.append(v)
        else:
            v = br.read(4)
            if v == 1:
                lens.append(1)
            else:
                lens.extend([v] * (br.read(4) + 3))
    if len(lens) != 16:
        raise ChdError("pohon huffman map rusak")
    tab = huff_table(lens)
    types, rep, last = [0] * hunkcount, 0, 0
    for i in range(hunkcount):
        if rep > 0:
            types[i] = last
            rep -= 1
            continue
        v = br.huff(tab)
        if v == CT_RLE_SMALL:
            types[i] = last
            rep = 2 + br.huff(tab)
        elif v == CT_RLE_LARGE:
            types[i] = last
            rep = 2 + 16 + (br.huff(tab) << 4)
            rep += br.huff(tab)
        else:
            types[i] = last = v
    entries, cur, last_self = [], firstoffs, 0
    for i in range(hunkcount):
        t, length, off, crc = types[i], 0, cur, 0
        if t < 4:
            length = br.read(lengthbits)
            cur += length
            crc = br.read(16)
        elif t == CT_NONE:
            length = hunkbytes
            cur += hunkbytes
            crc = br.read(16)
        elif t == CT_SELF:
            last_self = off = br.read(selfbits)
        elif t in (CT_SELF0, CT_SELF1):
            if t == CT_SELF1:
                last_self += 1
            t, off = CT_SELF, last_self
        else:
            raise ChdError("CHD memakai parent/delta - belum didukung")
        entries.append([t, length, off, crc])
    return entries, mapcrc


# --------------------------------------------------------------------------
# Track / CUE / GDI
# --------------------------------------------------------------------------
class Track:
    def __init__(self, num, ttype, frames=0, path=None, offset=0, subtype="NONE",
                 pregap=0, pgtype=None, pgsub="NONE", postgap=0, pad=None, lba=None):
        self.num, self.ttype, self.frames = num, ttype, frames
        self.path, self.offset, self.subtype = path, offset, subtype
        self.pregap, self.pgsub, self.postgap = pregap, pgsub, postgap
        self.pgtype = pgtype or ttype
        self.pad, self.lba = pad, lba
        self.chdstart = 0

    @property
    def datasize(self):
        return TRACK_DATASIZE[self.ttype]

    @property
    def total(self):
        return self.frames + (self.pad or 0)


def _msf_to_frames(m, s, f):
    return (int(m) * 60 + int(s)) * 75 + int(f)


def _msf(frames):
    return f"{frames // 4500:02d}:{(frames // 75) % 60:02d}:{frames % 75:02d}"


def _find_file(base, name):
    p = os.path.join(base, name.replace("\\", "/"))
    if os.path.exists(p):
        return p
    d, n = os.path.split(p)
    try:
        for e in os.listdir(d or "."):
            if e.lower() == n.lower():
                return os.path.join(d, e)
    except OSError:
        pass
    raise ChdError(f"file tidak ditemukan: {name}")


def parse_cue(path):
    base = os.path.dirname(os.path.abspath(path))
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        lines = fh.read().splitlines()
    tracks, files, curfile, cur = [], {}, None, None
    for raw in lines:
        s = raw.strip()
        u = s.upper()
        if u.startswith("FILE"):
            m = re.match(r'FILE\s+(?:"(.*)"|(\S+))', s, re.I)
            if not m:
                raise ChdError(f"baris FILE tidak valid: {s}")
            curfile = _find_file(base, m.group(1) if m.group(1) is not None else m.group(2))
        elif u.startswith("TRACK"):
            m = re.match(r"TRACK\s+(\d+)\s+(\S+)", s, re.I)
            if not m:
                raise ChdError(f"baris TRACK tidak valid: {s}")
            ct = m.group(2).upper()
            if ct not in CUE_TO_TYPE:
                raise ChdError(f"tipe track tidak didukung: {ct}")
            if curfile is None:
                raise ChdError("TRACK muncul sebelum FILE")
            cur = Track(int(m.group(1)), CUE_TO_TYPE[ct], path=curfile)
            cur.idx, cur.vpregap = {}, 0
            tracks.append(cur)
            files.setdefault(curfile, []).append(cur)
        elif u.startswith("INDEX") and cur:
            m = re.match(r"INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)", s, re.I)
            if m:
                cur.idx[int(m.group(1))] = _msf_to_frames(*m.groups()[1:])
        elif u.startswith("PREGAP") and cur:
            m = re.match(r"PREGAP\s+(\d+):(\d+):(\d+)", s, re.I)
            if m:
                cur.vpregap = _msf_to_frames(*m.groups())
        elif u.startswith("POSTGAP") and cur:
            m = re.match(r"POSTGAP\s+(\d+):(\d+):(\d+)", s, re.I)
            if m:
                cur.postgap = _msf_to_frames(*m.groups())
    if not tracks:
        raise ChdError("CUE tidak berisi track")
    for fpath, tl in files.items():
        ss = {t.datasize for t in tl}
        if len(ss) != 1:
            raise ChdError("satu file BIN berisi track dengan ukuran sektor berbeda - tidak didukung")
        ssize = ss.pop()
        size = os.path.getsize(fpath)
        if size % ssize:
            raise ChdError(f"{os.path.basename(fpath)}: ukuran bukan kelipatan {ssize} byte")
        total = size // ssize
        starts = []
        for i, t in enumerate(tl):
            if 1 not in t.idx:
                raise ChdError(f"track {t.num}: INDEX 01 tidak ada")
            i1, i0 = t.idx[1], t.idx.get(0)
            if i0 is None and i == 0 and i1 > 0:
                i0 = 0
            t.in_file_pregap = (i1 - i0) if i0 is not None else 0
            starts.append(i0 if i0 is not None else i1)
        for i, t in enumerate(tl):
            end = starts[i + 1] if i + 1 < len(tl) else total
            t.frames, t.offset = end - starts[i], starts[i] * ssize
            if t.frames < 0:
                raise ChdError(f"track {t.num}: urutan INDEX salah")
            if t.in_file_pregap:
                t.pregap, t.pgtype = t.in_file_pregap, "V" + t.ttype
            else:
                t.pregap, t.pgtype = t.vpregap, t.ttype
    return tracks


def parse_gdi(path):
    base = os.path.dirname(os.path.abspath(path))
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        lines = [l.strip() for l in fh if l.strip()]
    try:
        count = int(lines[0])
    except (ValueError, IndexError):
        raise ChdError("GDI: baris pertama harus jumlah track")
    tracks = []
    for l in lines[1:1 + count]:
        m = re.match(r'(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(?:"([^"]+)"|(\S+))\s+(\d+)', l)
        if not m:
            raise ChdError(f"GDI: baris tidak valid: {l}")
        num, lba, ctrl, ssize, q, plain, off = m.groups()
        num, lba, ctrl, ssize, off = int(num), int(lba), int(ctrl), int(ssize), int(off)
        fpath = _find_file(base, q or plain)
        if ctrl == 0:
            ttype = "AUDIO"
        else:
            ttype = {2352: "MODE1_RAW", 2048: "MODE1"}.get(ssize)
        if ttype is None or TRACK_DATASIZE[ttype] != ssize:
            raise ChdError(f"GDI track {num}: ukuran sektor {ssize} tidak didukung")
        size = os.path.getsize(fpath) - off
        if size < 0 or size % ssize:
            raise ChdError(f"GDI track {num}: ukuran file bukan kelipatan {ssize}")
        tracks.append(Track(num, ttype, size // ssize, fpath, off, lba=lba))
    if len(tracks) != count:
        raise ChdError("GDI: jumlah track tidak cocok dengan isi file")
    return tracks


# --------------------------------------------------------------------------
# CHD Reader
# --------------------------------------------------------------------------
class ChdReader:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        try:
            self._load()
        except Exception:
            self.f.close()
            raise

    def _load(self):
        h = self.f.read(HEADER_LEN)
        if len(h) < 16 or h[:8] != b"MComprHD":
            raise ChdError("bukan file CHD")
        length, version = struct.unpack(">II", h[8:16])
        if version != 5 or length != HEADER_LEN:
            raise ChdError(f"CHD versi {version} tidak didukung (hanya v5)")
        self.comp_tags = [tagstr(x) if x else None for x in struct.unpack(">4I", h[16:32])]
        self.logical, self.mapoff, self.metaoff = struct.unpack(">QQQ", h[32:56])
        self.hunkbytes, self.unitbytes = struct.unpack(">II", h[56:64])
        self.rawsha1, self.sha1, self.parentsha1 = h[64:84], h[84:104], h[104:124]
        if any(self.parentsha1):
            raise ChdError("CHD dengan parent belum didukung")
        self.hunkcount = -(-self.logical // self.hunkbytes)
        self.compressed = self.comp_tags[0] is not None
        self.codecs = [None] * 4          # dibuat lazy: tag yang tidak didukung hanya error bila benar-benar dipakai
        self._load_map()
        self.metadata = self._load_meta()

    def _load_map(self):
        if not self.compressed:
            self.f.seek(self.mapoff)
            raw = self.f.read(self.hunkcount * 4)
            self.map = [(CT_NONE, self.hunkbytes, struct.unpack(">I", raw[i * 4:i * 4 + 4])[0] * self.hunkbytes, None)
                        for i in range(self.hunkcount)]
            return
        entries, mapcrc = decode_map(self.f, self.mapoff, self.hunkcount, self.hunkbytes)
        raw = _raw_map([(e[0], e[1], e[2], e[3]) for e in entries])
        if crc16(raw) != mapcrc:
            raise ChdError("CRC map CHD tidak cocok (file rusak?)")
        self.map = [tuple(e) for e in entries]

    def _load_meta(self):
        out, off, guard = [], self.metaoff, 0
        while off:
            self.f.seek(off)
            h = self.f.read(16)
            if len(h) < 16:
                raise ChdError("metadata terpotong")
            tg, fl = struct.unpack(">4sI", h[:8])
            nxt = struct.unpack(">Q", h[8:])[0]
            out.append((tg.decode("latin-1"), fl >> 24, self.f.read(fl & 0xFFFFFF)))
            off = nxt
            guard += 1
            if guard > 100000:
                raise ChdError("rantai metadata tidak valid")
        return out

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _codec(self, t, hunk):
        if self.codecs[t] is None:
            tag = self.comp_tags[t]
            if tag is None:
                raise ChdError(f"hunk {hunk}: codec #{t} tidak terdaftar di header")
            try:
                self.codecs[t] = make_codec(tag, self.hunkbytes)
            except ChdError:
                raise ChdError(f"hunk {hunk} memakai codec '{tag}' yang belum didukung "
                               "(Huffman/AV). Sementara: kembalikan dengan chdman "
                               "(extractcd / extractdvd).")
        return self.codecs[t]

    def read_hunk(self, i: int) -> bytes:
        if not 0 <= i < self.hunkcount:
            raise ChdError(f"hunk {i} di luar jangkauan")
        t, l, o, crc = self.map[i]
        hops = 0
        while t == CT_SELF:
            i = o
            t, l, o, crc = self.map[i]
            hops += 1
            if hops > 64:
                raise ChdError("referensi self berantai terlalu dalam")
        self.f.seek(o)
        if t == CT_NONE:
            data = self.f.read(self.hunkbytes)
            comp = None
        elif t < 4:
            comp = self.f.read(l)
            codec = self._codec(t, i)
            try:
                data = codec.decompress(comp, self.hunkbytes)
            except (lzma.LZMAError, zlib.error, IndexError, struct.error) as ex:
                raise ChdError(f"hunk {i}: data terkompresi korup ({ex})")
        else:
            raise ChdError(f"hunk {i}: tipe kompresi {t} tidak didukung")
        if len(data) != self.hunkbytes:
            raise ChdError(f"hunk {i}: data terpotong")
        if crc is not None and crc != crc16(data) and (comp is None or crc != crc16(comp)):
            raise ChdError(f"hunk {i}: CRC16 tidak cocok (data korup)")
        return data

    # ---- metadata CD ----
    def kind(self):
        tags = {m[0] for m in self.metadata}
        if tags & {"CHGD", "CHGT"}:
            return "gd"
        if tags & {"CHT2", "CHTR"}:
            return "cd"
        if "DVD " in tags:
            return "dvd"
        return "raw"

    def cd_tracks(self):
        tracks, lbas = [], {}
        for tg, _fl, data in self.metadata:
            txt = data.rstrip(b"\0").decode("ascii", "replace")
            kv = dict(re.findall(r"(\w+):(\S+)", txt))
            if tg in ("CHT2", "CHTR", "CHGD", "CHGT"):
                ttype = kv.get("TYPE", "MODE1")
                if ttype not in TRACK_DATASIZE:
                    raise ChdError(f"tipe track '{ttype}' tidak dikenal")
                frames = int(kv["FRAMES"])
                t = Track(int(kv["TRACK"]), ttype, frames, subtype=kv.get("SUBTYPE", "NONE"),
                          pregap=int(kv.get("PREGAP", 0)), pgtype=kv.get("PGTYPE", ttype),
                          pgsub=kv.get("PGSUB", "NONE"), postgap=int(kv.get("POSTGAP", 0)),
                          pad=int(kv["PAD"]) if "PAD" in kv else (-frames) % 4)
                tracks.append(t)
            elif tg == "FGLB" and "TRACK" in kv and "LBA" in kv:
                lbas[int(kv["TRACK"])] = int(kv["LBA"])
        pos = 0
        for t in tracks:
            t.chdstart = pos
            pos += t.total
            t.lba = lbas.get(t.num)
        return tracks

    def info(self) -> str:
        comp = sum(self.map[i][1] for i in range(self.hunkcount) if self.map[i][0] < 4)
        none = sum(1 for e in self.map if e[0] == CT_NONE)
        selfn = sum(1 for e in self.map if e[0] == CT_SELF)
        size = os.path.getsize(self.path)
        lines = [
            f"File          : {self.path}",
            f"CHD version   : 5   ({self.kind().upper()})",
            f"Logical size  : {self.logical:,} bytes ({human(self.logical)})",
            f"Hunk / Unit   : {self.hunkbytes:,} / {self.unitbytes:,} bytes",
            f"Hunks         : {self.hunkcount:,} (compressed {self.hunkcount - none - selfn:,}, raw {none:,}, dedup {selfn:,})",
            f"Compressors   : {', '.join(t for t in self.comp_tags if t) or 'none'}",
            f"File size     : {size:,} bytes ({100 * size / max(1, self.logical):.1f}% dari logical)",
            f"Raw SHA1      : {self.rawsha1.hex()}",
            f"SHA1          : {self.sha1.hex()}",
        ]
        for tg, fl, data in self.metadata:
            txt = data.rstrip(b"\0").decode("ascii", "replace")
            lines.append(f"Metadata [{tg}] : {txt}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# CHD Writer
# --------------------------------------------------------------------------
def write_chd(path, hunks, logical, hunkbytes, unitbytes, codec, metadata,
              workers=2, progress=None, cancel=None):
    progress = progress or (lambda f, m: None)
    hunkcount = -(-logical // hunkbytes)
    if hunkcount == 0:
        raise ChdError("input kosong")
    tmp = path + ".part"
    entries, seen = [], {}
    sha = hashlib.sha1()
    remaining, cur, idx = logical, HEADER_LEN, 0
    batch_n = max(8, workers * 8)
    try:
        with open(tmp, "wb") as f, ThreadPoolExecutor(workers) as ex:
            f.write(bytes(HEADER_LEN))
            it = iter(hunks)
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                batch = list(itertools.islice(it, batch_n))
                if not batch:
                    break
                pend = []
                for h in batch:
                    if len(h) != hunkbytes:
                        raise ChdError("ukuran hunk dari sumber salah")
                    take = min(hunkbytes, remaining)
                    sha.update(h if take == hunkbytes else h[:take])
                    remaining -= take
                    dg = hashlib.sha1(h).digest()
                    crc = crc16(h)
                    if dg in seen:
                        pend.append((h, crc, seen[dg], None))
                    else:
                        seen[dg] = idx
                        pend.append((h, crc, None, ex.submit(codec.compress, h)))
                    idx += 1
                for h, crc, sidx, fut in pend:
                    if sidx is not None:
                        entries.append((CT_SELF, 0, sidx, 0))
                        continue
                    comp = fut.result()
                    if comp is None or len(comp) >= hunkbytes:
                        f.write(h)
                        entries.append((CT_NONE, hunkbytes, cur, crc))
                        cur += hunkbytes
                    else:
                        f.write(comp)
                        entries.append((0, len(comp), cur, crc))
                        cur += len(comp)
                progress(min(1.0, idx / hunkcount), f"hunk {idx:,}/{hunkcount:,}")
            if idx != hunkcount or remaining:
                raise ChdError("jumlah data sumber tidak sesuai ukuran logical")
            metaoff = 0
            if metadata:
                metaoff, pos = cur, cur
                for i, (tg, flags, data) in enumerate(metadata):
                    nxt = pos + 16 + len(data) if i < len(metadata) - 1 else 0
                    f.write(struct.pack(">4sIQ", tg.encode("ascii"), (flags << 24) | len(data), nxt))
                    f.write(data)
                    pos += 16 + len(data)
                cur = pos
            mapoff = cur
            f.write(encode_map([(e[0], 0 if e[0] == CT_SELF else e[1], e[2], e[3]) for e in entries], HEADER_LEN))
            rawsha = sha.digest()
            mh = sorted(struct.pack(">4s", tg.encode("ascii")) + hashlib.sha1(d).digest()
                        for tg, fl, d in metadata if fl & 1)
            overall = hashlib.sha1(rawsha + b"".join(mh)).digest()
            hdr = (b"MComprHD" + struct.pack(">II", HEADER_LEN, 5) +
                   struct.pack(">4I", tag32(codec.tag), 0, 0, 0) +
                   struct.pack(">QQQ", logical, mapoff, metaoff) +
                   struct.pack(">II", hunkbytes, unitbytes) + rawsha + overall + bytes(20))
            assert len(hdr) == HEADER_LEN
            f.seek(0)
            f.write(hdr)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Sumber data -> hunk
# --------------------------------------------------------------------------
def iter_file_hunks(path, hunkbytes):
    with open(path, "rb") as f:
        while True:
            b = f.read(hunkbytes)
            if not b:
                return
            if len(b) < hunkbytes:
                b += bytes(hunkbytes - len(b))
            yield b


def iter_cd_frames(tracks):
    tail_sub = bytes(CD_SUB)
    for t in tracks:
        ds = t.datasize
        tail = bytes(CD_MAIN - ds) + tail_sub
        left = t.frames
        if left:
            with open(t.path, "rb") as f:
                f.seek(t.offset)
                while left:
                    n = min(left, 256)
                    buf = f.read(n * ds)
                    if len(buf) != n * ds:
                        raise ChdError(f"{os.path.basename(t.path)}: file lebih pendek dari yang diharapkan")
                    for i in range(n):
                        yield buf[i * ds:(i + 1) * ds] + tail
                    left -= n
        for _ in range(t.pad):
            yield bytes(CD_FRAME)


def iter_cd_hunks(tracks):
    buf = []
    for fr in iter_cd_frames(tracks):
        buf.append(fr)
        if len(buf) == FRAMES_PER_HUNK:
            yield b"".join(buf)
            buf = []
    if buf:
        yield b"".join(buf) + bytes(CD_FRAME * (FRAMES_PER_HUNK - len(buf)))


def cd_metadata(tracks, gd):
    md = []
    for t in tracks:
        if gd:
            s = (f"TRACK:{t.num} TYPE:{t.ttype} SUBTYPE:{t.subtype} FRAMES:{t.frames} PAD:{t.pad} "
                 f"PREGAP:{t.pregap} PGTYPE:{t.pgtype} PGSUB:{t.pgsub} POSTGAP:{t.postgap}")
            md.append(("CHGD", 1, s.encode() + b"\0"))
        else:
            s = (f"TRACK:{t.num} TYPE:{t.ttype} SUBTYPE:{t.subtype} FRAMES:{t.frames} "
                 f"PREGAP:{t.pregap} PGTYPE:{t.pgtype} PGSUB:{t.pgsub} POSTGAP:{t.postgap}")
            md.append(("CHT2", 1, s.encode() + b"\0"))
    if gd:   # tag privat (diabaikan MAME/emulator): menjamin LBA GDI kembali persis
        for t in tracks:
            md.append(("FGLB", 0, f"TRACK:{t.num} LBA:{t.lba}".encode() + b"\0"))
    return md


# --------------------------------------------------------------------------
# API tingkat atas
# --------------------------------------------------------------------------
def _noop(f, m):
    pass


def convert(src, dst, mode="auto", codec="lzma", workers=None, progress=None, cancel=None):
    """src: .cue / .gdi / .iso / .img  ->  dst .chd.  mode: auto|dvd|cd (untuk ISO)."""
    progress = progress or _noop
    workers = workers or os.cpu_count() or 2
    ext = os.path.splitext(src)[1].lower()
    if ext in (".cue", ".gdi") or (ext in (".iso", ".img") and mode == "cd"):
        gd = ext == ".gdi"
        if ext == ".cue":
            tracks = parse_cue(src)
        elif gd:
            tracks = parse_gdi(src)
        else:
            size = os.path.getsize(src)
            if size % 2048:
                raise ChdError("ISO bukan kelipatan 2048 byte - bukan ISO data valid")
            tracks = [Track(1, "MODE1", size // 2048, src, 0)]
        for t in tracks:
            if t.pad is None:
                t.pad = (-t.frames) % 4
        total = sum(t.total for t in tracks)
        if total == 0:
            raise ChdError("tidak ada data")
        c = make_codec("cdlz" if codec == "lzma" else "cdzl", CD_HUNK)
        write_chd(dst, iter_cd_hunks(tracks), total * CD_FRAME, CD_HUNK, CD_FRAME, c,
                  cd_metadata(tracks, gd), workers, progress, cancel)
    elif ext in (".iso", ".img", ".dvd"):
        size = os.path.getsize(src)
        if size % 2048:
            progress(0, "peringatan: ukuran ISO bukan kelipatan 2048 byte (tetap diproses apa adanya)")
        c = make_codec(codec, DVD_HUNK)
        write_chd(dst, iter_file_hunks(src, DVD_HUNK), size, DVD_HUNK, 2048, c,
                  [("DVD ", 1, b"\0")], workers, progress, cancel)
    elif ext == ".bin":
        raise ChdError("file .bin harus lewat .cue-nya (pilih file .cue)")
    else:
        raise ChdError(f"ekstensi '{ext}' tidak didukung")
    return dst


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _iter_frames(chd, start, count):
    fph = chd.hunkbytes // CD_FRAME
    curh, data = -1, b""
    for fno in range(start, start + count):
        h = fno // fph
        if h != curh:
            data, curh = chd.read_hunk(h), h
        o = (fno % fph) * CD_FRAME
        yield data[o:o + CD_FRAME]


def _dump_track(chd, t, fh, state, progress, cancel):
    ds, buf = t.datasize, []
    for fr in _iter_frames(chd, t.chdstart, t.frames):
        buf.append(fr[:ds])
        state[0] += 1
        if len(buf) >= 256:
            fh.write(b"".join(buf))
            buf = []
            _check_cancel(cancel)
            progress(state[0] / state[1], f"track {t.num}")
    if buf:
        fh.write(b"".join(buf))


def extract(chd_path, outdir=None, split=False, progress=None, cancel=None, force=False):
    """CHD -> ISO / BIN+CUE / GDI / IMG. Mengembalikan daftar file yang dibuat."""
    progress = progress or _noop
    name = os.path.splitext(os.path.basename(chd_path))[0]
    outdir = outdir or os.path.dirname(os.path.abspath(chd_path))
    created = []
    with ChdReader(chd_path) as chd:
        kind = chd.kind()
        tracks = chd.cd_tracks() if kind in ("cd", "gd") else []
        if kind in ("dvd", "raw") or (kind == "cd" and len(tracks) == 1 and tracks[0].ttype == "MODE1"
                                       and not tracks[0].pregap and not tracks[0].postgap):
            ext = ".img" if kind == "raw" else ".iso"
            out = os.path.join(outdir, name + ext)
            if os.path.exists(out) and not force:
                raise ChdError(f"{os.path.basename(out)} sudah ada (pakai overwrite)")
            os.makedirs(outdir, exist_ok=True)
            tmp = out + ".part"
            try:
                with open(tmp, "wb") as fh:
                    if kind == "cd":
                        st = [0, max(1, tracks[0].frames)]
                        _dump_track(chd, tracks[0], fh, st, progress, cancel)
                    else:
                        left = chd.logical
                        for i in range(chd.hunkcount):
                            _check_cancel(cancel)
                            h = chd.read_hunk(i)
                            fh.write(h[:min(len(h), left)])
                            left -= min(len(h), left)
                            if i % 64 == 0:
                                progress(i / chd.hunkcount, f"hunk {i:,}/{chd.hunkcount:,}")
                os.replace(tmp, out)
            except BaseException:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
            return [out]

        if kind == "gd":
            outdir = os.path.join(outdir, name)
        os.makedirs(outdir, exist_ok=True)
        mixed = len({t.datasize for t in tracks}) > 1 and len(tracks) > 1
        if kind == "gd":
            split = True
        split = split or mixed
        total = sum(t.frames for t in tracks) or 1
        state = [0, total]
        placed, lines = [], []
        try:
            if kind == "gd":
                for t in tracks:
                    fn = f"track{t.num:02d}.{'raw' if t.ttype == 'AUDIO' else 'bin'}"
                    placed.append((t, fn, 0))
            elif split:
                for t in tracks:
                    placed.append((t, f"{name} (Track {t.num:02d}).bin", 0))
            else:
                pos = 0
                for t in tracks:
                    placed.append((t, f"{name}.bin", pos))
                    pos += t.frames
            check = [os.path.join(outdir, fn) for fn in dict.fromkeys(p[1] for p in placed)]
            gdi_path = os.path.join(outdir, name + ".gdi")
            cue_path = os.path.join(outdir, name + ".cue")
            check.append(gdi_path if kind == "gd" else cue_path)
            for p in check:
                if os.path.exists(p) and not force:
                    raise ChdError(f"{os.path.basename(p)} sudah ada (pakai overwrite)")
            handles = {}
            for t, fn, pos in placed:
                p = os.path.join(outdir, fn)
                if p not in handles:
                    handles[p] = open(p, "wb")
                    created.append(p)
                _dump_track(chd, t, handles[p], state, progress, cancel)
            for h in handles.values():
                h.close()
            if kind == "gd":
                lba, rows = 0, []
                for t, fn, _ in placed:
                    if t.lba is not None:
                        lba = t.lba
                    elif t.num == 3 and lba < GD_HD_START_LBA:
                        lba = GD_HD_START_LBA
                    rows.append(f"{t.num} {lba} {0 if t.ttype == 'AUDIO' else 4} {t.datasize} {fn} 0")
                    lba += t.frames
                with open(gdi_path, "w", newline="\n") as fh:
                    fh.write(f"{len(tracks)}\n" + "\n".join(rows) + "\n")
                created.append(gdi_path)
            else:
                last = None
                for t, fn, pos in placed:
                    if fn != last:
                        lines.append(f'FILE "{fn}" BINARY')
                        last = fn
                    lines.append(f"  TRACK {t.num:02d} {CUE_TYPE[t.ttype]}")
                    v = t.pgtype.startswith("V")
                    if t.pregap and not v:
                        lines.append(f"    PREGAP {_msf(t.pregap)}")
                    if t.pregap and v:
                        lines.append(f"    INDEX 00 {_msf(pos)}")
                        lines.append(f"    INDEX 01 {_msf(pos + t.pregap)}")
                    else:
                        lines.append(f"    INDEX 01 {_msf(pos)}")
                    if t.postgap:
                        lines.append(f"    POSTGAP {_msf(t.postgap)}")
                with open(cue_path, "w", newline="\n") as fh:
                    fh.write("\n".join(lines) + "\n")
                created.append(cue_path)
        except BaseException:
            for h in locals().get("handles", {}).values():
                h.close()
            for p in created:
                if os.path.exists(p):
                    os.remove(p)
            raise
    return created


def verify(chd_path, progress=None, cancel=None):
    """Dekompres semua hunk, cek CRC16 tiap hunk + SHA1 data mentah. -> (ok, pesan)"""
    progress = progress or _noop
    with ChdReader(chd_path) as chd:
        sha, left = hashlib.sha1(), chd.logical
        for i in range(chd.hunkcount):
            _check_cancel(cancel)
            try:
                h = chd.read_hunk(i)
            except ChdError as ex:
                return False, str(ex)
            take = min(len(h), left)
            sha.update(h[:take])
            left -= take
            if i % 64 == 0:
                progress(i / chd.hunkcount, f"verify {i:,}/{chd.hunkcount:,}")
        if sha.digest() != chd.rawsha1:
            return False, "SHA1 data mentah TIDAK cocok dengan header"
    return True, "OK - semua hunk valid, SHA1 cocok"


def info(chd_path):
    with ChdReader(chd_path) as chd:
        return chd.info()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cli_progress():
    last = [-1]

    def cb(frac, msg):
        pct = int(frac * 100)
        if pct != last[0]:
            last[0] = pct
            sys.stdout.write(f"\r  {pct:3d}%  {msg:<40}")
            sys.stdout.flush()
    return cb


def main(argv=None):
    ap = argparse.ArgumentParser(prog="chdforge", description="CHD v5 converter (pure Python)")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("convert", help="CUE/GDI/ISO -> CHD")
    c.add_argument("files", nargs="+")
    c.add_argument("-o", "--outdir")
    c.add_argument("--mode", choices=["auto", "dvd", "cd"], default="auto", help="untuk ISO")
    c.add_argument("--codec", choices=["lzma", "zlib"], default="lzma")
    c.add_argument("-j", "--jobs", type=int)
    c.add_argument("--force", action="store_true")
    c.add_argument("--verify", action="store_true")
    e = sub.add_parser("extract", help="CHD -> ISO/BIN+CUE/GDI")
    e.add_argument("files", nargs="+")
    e.add_argument("-o", "--outdir")
    e.add_argument("--split", action="store_true", help="satu BIN per track")
    e.add_argument("--force", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("files", nargs="+")
    i = sub.add_parser("info")
    i.add_argument("files", nargs="+")
    sub.add_parser("gui")
    a = ap.parse_args(argv)
    if a.cmd in (None, "gui"):
        return run_gui()
    rc = 0
    for f in a.files:
        print(f"\n== {f}")
        try:
            if a.cmd == "convert":
                od = a.outdir or os.path.dirname(os.path.abspath(f))
                os.makedirs(od, exist_ok=True)
                out = os.path.join(od, os.path.splitext(os.path.basename(f))[0] + ".chd")
                if os.path.exists(out) and not a.force:
                    raise ChdError(f"{out} sudah ada (pakai --force)")
                convert(f, out, a.mode, a.codec, a.jobs, _cli_progress())
                print(f"\n  -> {out}  ({human(os.path.getsize(out))})")
                if a.verify:
                    ok, msg = verify(out, _cli_progress())
                    print(f"\n  verify: {msg}")
                    rc |= 0 if ok else 1
            elif a.cmd == "extract":
                for p in extract(f, a.outdir, a.split, _cli_progress(), None, a.force):
                    print(f"\n  -> {p}")
            elif a.cmd == "verify":
                ok, msg = verify(f, _cli_progress())
                print(f"\n  {msg}")
                rc |= 0 if ok else 1
            elif a.cmd == "info":
                print(info(f))
        except (ChdError, OSError) as ex:
            print(f"\n  ERROR: {ex}")
            rc = 1
    return rc


# --------------------------------------------------------------------------
# GUI (Tkinter)
# --------------------------------------------------------------------------
def run_gui():
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"CHDForge {__version__}")
    root.geometry("860x680")
    q: "queue.Queue" = queue.Queue()
    cancel = threading.Event()
    state = {"running": False}
    items = {}  # iid -> path

    mode = tk.StringVar(value="convert")
    iso_mode = tk.StringVar(value="DVD (PS2 / PSP / DVD)")
    codec = tk.StringVar(value="LZMA (kecil)")
    threads = tk.IntVar(value=max(1, (os.cpu_count() or 2)))
    outdir = tk.StringVar()
    overwrite = tk.BooleanVar(value=False)
    verify_after = tk.BooleanVar(value=True)
    split = tk.BooleanVar(value=False)

    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    for text, val in (("Convert -> CHD", "convert"), ("Extract CHD -> ISO/BIN/GDI", "extract"),
                      ("Verify CHD", "verify"), ("Info", "info")):
        ttk.Radiobutton(top, text=text, value=val, variable=mode).pack(side="left", padx=6)

    mid = ttk.Frame(root, padding=(8, 0))
    mid.pack(fill="both", expand=True)
    tree = ttk.Treeview(mid, columns=("file", "status"), show="headings", height=8)
    tree.heading("file", text="File")
    tree.heading("status", text="Status")
    tree.column("file", width=560)
    tree.column("status", width=220)
    sb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")

    btns = ttk.Frame(root, padding=8)
    btns.pack(fill="x")

    def exts():
        return {"convert": (".cue", ".gdi", ".iso", ".img"), }.get(mode.get(), (".chd",))

    def add_path(p):
        if p in items.values():
            return
        iid = tree.insert("", "end", values=(p, "Queued"))
        items[iid] = p

    def add_files():
        e = exts()
        paths = filedialog.askopenfilenames(
            filetypes=[("Supported", " ".join("*" + x for x in e)), ("All", "*.*")])
        for p in paths:
            add_path(p)

    def add_folder():
        d = filedialog.askdirectory()
        if not d:
            return
        e = exts()
        for r, _, fs in os.walk(d):
            for fn in sorted(fs):
                if fn.lower().endswith(e):
                    add_path(os.path.join(r, fn))

    def remove_sel():
        for iid in tree.selection():
            items.pop(iid, None)
            tree.delete(iid)

    def clear():
        for iid in list(items):
            tree.delete(iid)
        items.clear()

    for t, cmd in (("Add files", add_files), ("Add folder", add_folder),
                   ("Remove", remove_sel), ("Clear", clear)):
        ttk.Button(btns, text=t, command=cmd).pack(side="left", padx=3)

    opt = ttk.LabelFrame(root, text="Options", padding=8)
    opt.pack(fill="x", padx=8)
    ttk.Label(opt, text="ISO type:").grid(row=0, column=0, sticky="w")
    ttk.Combobox(opt, textvariable=iso_mode, state="readonly", width=24,
                 values=["DVD (PS2 / PSP / DVD)", "CD (data MODE1)"]).grid(row=0, column=1, padx=4)
    ttk.Label(opt, text="Compression:").grid(row=0, column=2, sticky="w", padx=(12, 0))
    ttk.Combobox(opt, textvariable=codec, state="readonly", width=16,
                 values=["LZMA (kecil)", "ZLIB (cepat)"]).grid(row=0, column=3, padx=4)
    ttk.Label(opt, text="Threads:").grid(row=0, column=4, sticky="w", padx=(12, 0))
    ttk.Spinbox(opt, from_=1, to=64, width=4, textvariable=threads).grid(row=0, column=5, padx=4)
    ttk.Label(opt, text="Output folder:").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Entry(opt, textvariable=outdir, width=60).grid(row=1, column=1, columnspan=4, sticky="we", padx=4)
    ttk.Button(opt, text="Browse", command=lambda: outdir.set(filedialog.askdirectory() or outdir.get())
               ).grid(row=1, column=5)
    ttk.Checkbutton(opt, text="Overwrite", variable=overwrite).grid(row=2, column=0, sticky="w")
    ttk.Checkbutton(opt, text="Verify after convert", variable=verify_after).grid(row=2, column=1, sticky="w")
    ttk.Checkbutton(opt, text="Split BIN per track (extract)", variable=split).grid(row=2, column=2, columnspan=2, sticky="w")
    ttk.Label(opt, text="(kosongkan output folder = di folder yang sama dengan sumber)",
              foreground="gray").grid(row=3, column=0, columnspan=6, sticky="w")

    pf = ttk.Frame(root, padding=8)
    pf.pack(fill="x")
    lbl = ttk.Label(pf, text="Idle")
    lbl.pack(anchor="w")
    bar = ttk.Progressbar(pf, maximum=1000)
    bar.pack(fill="x", pady=2)
    obar = ttk.Progressbar(pf, maximum=1000)
    obar.pack(fill="x", pady=2)
    log = tk.Text(root, height=9, state="disabled", wrap="word")
    log.pack(fill="both", expand=False, padx=8, pady=(0, 4))

    def logln(s):
        log.configure(state="normal")
        log.insert("end", s + "\n")
        log.see("end")
        log.configure(state="disabled")

    def worker(jobs, o):
        n = len(jobs)
        for k, (iid, path) in enumerate(jobs):
            if cancel.is_set():
                q.put(("status", iid, "Cancelled"))
                continue
            q.put(("status", iid, "Running..."))
            q.put(("label", f"[{k + 1}/{n}] {os.path.basename(path)}"))

            def prog(frac, msg, k=k):
                if cancel.is_set():
                    raise Cancelled()
                q.put(("prog", frac, (k + frac) / n))

            try:
                if o["mode"] == "convert":
                    od = o["outdir"] or os.path.dirname(os.path.abspath(path))
                    os.makedirs(od, exist_ok=True)
                    out = os.path.join(od, os.path.splitext(os.path.basename(path))[0] + ".chd")
                    if os.path.exists(out) and not o["force"]:
                        raise ChdError("output sudah ada (centang Overwrite)")
                    convert(path, out, o["iso"], o["codec"], o["threads"], prog, cancel)
                    msg = f"OK  {human(os.path.getsize(path))} -> {human(os.path.getsize(out))}"
                    if o["verify"]:
                        ok, vm = verify(out, prog, cancel)
                        if not ok:
                            raise ChdError("verify gagal: " + vm)
                        msg += "  (verified)"
                    q.put(("status", iid, msg))
                elif o["mode"] == "extract":
                    files = extract(path, o["outdir"] or None, o["split"], prog, cancel, o["force"])
                    q.put(("log", "  -> " + ", ".join(os.path.basename(x) for x in files)))
                    q.put(("status", iid, "OK"))
                elif o["mode"] == "verify":
                    ok, vm = verify(path, prog, cancel)
                    q.put(("status", iid, vm if ok else "FAILED: " + vm))
                else:
                    q.put(("log", info(path)))
                    q.put(("status", iid, "OK"))
            except Cancelled:
                q.put(("status", iid, "Cancelled"))
            except Exception as ex:   # noqa
                q.put(("status", iid, f"ERROR: {ex}"))
                q.put(("log", f"ERROR {os.path.basename(path)}: {ex}"))
        q.put(("done",))

    def poll():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "status":
                    tree.set(m[1], "status", m[2])
                elif m[0] == "label":
                    lbl.configure(text=m[1])
                    logln(m[1])
                elif m[0] == "prog":
                    bar["value"] = m[1] * 1000
                    obar["value"] = m[2] * 1000
                elif m[0] == "log":
                    logln(m[1])
                elif m[0] == "done":
                    state["running"] = False
                    start_btn.configure(state="normal")
                    cancel_btn.configure(state="disabled")
                    lbl.configure(text="Done")
        except queue.Empty:
            pass
        root.after(80, poll)

    def start():
        if state["running"] or not items:
            return
        cancel.clear()
        state["running"] = True
        start_btn.configure(state="disabled")
        cancel_btn.configure(state="normal")
        for iid in items:
            tree.set(iid, "status", "Queued")
        o = {"mode": mode.get(), "iso": "cd" if iso_mode.get().startswith("CD") else "dvd",
             "codec": "zlib" if codec.get().startswith("ZLIB") else "lzma",
             "threads": max(1, threads.get()), "outdir": outdir.get().strip(),
             "force": overwrite.get(), "verify": verify_after.get(), "split": split.get()}
        threading.Thread(target=worker, args=(list(items.items()), o), daemon=True).start()

    def do_cancel():
        cancel.set()

    ctl = ttk.Frame(root, padding=8)
    ctl.pack(fill="x")
    start_btn = ttk.Button(ctl, text="Start", command=start)
    start_btn.pack(side="left")
    cancel_btn = ttk.Button(ctl, text="Cancel", command=do_cancel, state="disabled")
    cancel_btn.pack(side="left", padx=6)
    logln(f"CHDForge {__version__} - pure Python CHD v5. Tambah file lalu tekan Start.")
    poll()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
