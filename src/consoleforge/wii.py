"""Analisis partisi Wii untuk scrub WBFS: dekripsi (kunci dari user), cek hash H0, baca FST,
lalu tentukan sektor 32 KiB mana yang benar-benar dipakai game.
Kunci TIDAK disimpan di kode; diberikan saat runtime."""
import hashlib
import os
import re
import struct

from aes import AES128
from common import ForgeError

SEC, HASH, DATA = 0x8000, 0x400, 0x7C00
KEY_NAMES = {0: "common", 1: "korean", 2: "vwii"}


def be32(b, o):
    return struct.unpack(">I", b[o:o + 4])[0]


def parse_key(text):
    t = re.sub(r"[^0-9a-fA-F]", "", text or "")
    if len(t) != 32:
        raise ForgeError("kunci harus 32 karakter hex (16 byte)")
    return bytes.fromhex(t)


def load_keys(common=None, korean=None, vwii=None, search_dirs=()):
    """Cari kunci: argumen > env (WII_COMMON_KEY/_KOREAN/_VWII) > file wii_keys.txt (common=HEX)."""
    keys = {}
    for idx, val, env in ((0, common, "WII_COMMON_KEY"), (1, korean, "WII_KOREAN_KEY"), (2, vwii, "WII_VWII_KEY")):
        val = val or os.environ.get(env)
        if val:
            keys[idx] = parse_key(val)
    for d in search_dirs:
        p = os.path.join(d, "wii_keys.txt")
        if os.path.isfile(p):
            for line in open(p, encoding="utf-8", errors="ignore"):
                m = re.match(r"\s*(common|korean|vwii)\s*=\s*([0-9a-fA-F\s]+)", line)
                if m:
                    idx = {"common": 0, "korean": 1, "vwii": 2}[m.group(1)]
                    keys.setdefault(idx, parse_key(m.group(2)))
    return keys


class WiiPartition:
    def __init__(self, f, po, keys, disc_flags):
        self.f, self.po = f, po
        f.seek(po)
        t = f.read(0x2C0)
        if len(t) < 0x2C0:
            raise ForgeError("header partisi terpotong")
        self.data_off, self.data_size = be32(t, 0x2B8) << 2, be32(t, 0x2BC) << 2
        self.ncl = self.data_size // SEC
        self.encrypted = not disc_flags[1]
        self.check_hash = not disc_flags[0]
        self.start = po + self.data_off
        if self.start % SEC or self.ncl == 0:
            raise ForgeError("partisi tidak sejajar 0x8000 / kosong")
        self._cache = (-1, None)
        if self.encrypted:
            ki = t[0x1F1]
            if ki not in keys:
                raise ForgeError(f"disc memakai kunci '{KEY_NAMES.get(ki, ki)}' (index {ki}) yang tidak diberikan")
            self.aes = AES128(AES128(keys[ki]).decrypt_cbc(t[0x1DC:0x1E4] + bytes(8), t[0x1BF:0x1CF]))

    def cluster(self, k):
        if self._cache[0] == k:
            return self._cache[1]
        if not 0 <= k < self.ncl:
            raise ForgeError(f"cluster {k} di luar partisi")
        self.f.seek(self.start + k * SEC)
        raw = self.f.read(SEC)
        if len(raw) != SEC:
            raise ForgeError("cluster terpotong")
        if self.encrypted:
            hashes = self.aes.decrypt_cbc(bytes(16), raw[:HASH])
            data = self.aes.decrypt_cbc(raw[0x3D0:0x3E0], raw[HASH:])
        else:
            hashes, data = raw[:HASH], raw[HASH:]
        if self.check_hash:
            for i in range(31):         # H0: SHA1 tiap 0x400 byte data. Gagal = kunci salah / data rusak.
                if hashlib.sha1(data[i * HASH:(i + 1) * HASH]).digest() != hashes[i * 20:(i + 1) * 20]:
                    raise ForgeError(f"hash H0 cluster {k} tidak cocok (kunci salah atau data korup)")
        self._cache = (k, data)
        return data

    def read(self, off, n):
        out = bytearray()
        while n > 0:
            k, o = divmod(off, DATA)
            chunk = self.cluster(k)[o:o + n]
            out += chunk
            off += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def used_clusters(self):
        """Himpunan indeks cluster yang dipakai (boot, apploader, DOL, FST, semua file)."""
        b = self.read(0, 0x2460)
        dol, fst, fsz = be32(b, 0x420) << 2, be32(b, 0x424) << 2, be32(b, 0x428) << 2
        limit = self.ncl * DATA
        rng = [(0, 0x2440), (0x2440, 0x2440 + 0x20 + be32(b, 0x2454) + be32(b, 0x2458))]
        dh = self.read(dol, 0x100)
        dend = max([0x100] + [be32(dh, i * 4) + be32(dh, 0x90 + i * 4) for i in range(18) if be32(dh, 0x90 + i * 4)])
        rng.append((dol, dol + dend))
        if fsz < 12 or fsz > 64 << 20:
            raise ForgeError("ukuran FST tidak masuk akal")
        fs = self.read(fst, fsz)
        n = be32(fs, 8)
        if n < 1 or n * 12 > fsz:
            raise ForgeError("FST tidak valid")
        rng.append((fst, fst + fsz))
        for i in range(1, n):
            e = fs[i * 12:i * 12 + 12]
            if e[0] == 0 and be32(e, 8):
                rng.append((be32(e, 4) << 2, (be32(e, 4) << 2) + be32(e, 8)))
        used = set()
        for a, z in rng:
            if a < 0 or z > limit or z < a:
                raise ForgeError(f"entri FST di luar partisi (0x{a:X}-0x{z:X})")
            if z > a:
                used.update(range(a // DATA, (z - 1) // DATA + 1))
        return used


def build_usage(f, size, keys, log=None):
    """bytearray per sektor 0x8000 disc: 1 = dipakai. Partisi update dibuang; partisi yang tidak bisa
    dianalisis disimpan utuh (aman)."""
    from wbfs import wii_partitions          # impor lokal: hindari impor melingkar
    log = log or (lambda m: None)
    f.seek(0)
    head = f.read(0x100)
    flags = (head[0x60], head[0x61])
    used = bytearray(-(-size // SEC))

    def mark(a, z):
        for s in range(a // SEC, min(len(used), -(-z // SEC))):
            used[s] = 1
    mark(0, 0x50000)
    for po, pt in wii_partitions(f):
        if pt == 1:
            continue
        f.seek(po + 0x2B8)
        h = f.read(8)
        do, ds = be32(h, 0) << 2, be32(h, 4) << 2
        if po % SEC or do % SEC:
            raise ForgeError(f"partisi di 0x{po:X} tidak sejajar - pakai mode game/full")
        mark(po, po + do)
        try:
            part = WiiPartition(f, po, keys, flags)
            clusters = part.used_clusters()
        except ForgeError as ex:
            log(f"PERINGATAN partisi 0x{po:X} tipe {pt}: {ex} -> disimpan utuh (tanpa scrub)")
            mark(po + do, po + do + ds)
            continue
        for k in clusters:
            mark(po + do + k * SEC, po + do + (k + 1) * SEC)
        log(f"partisi 0x{po:X} tipe {pt}: {len(clusters)}/{part.ncl} cluster dipakai "
            f"({100 * len(clusters) // max(1, part.ncl)}%)")
    return used
