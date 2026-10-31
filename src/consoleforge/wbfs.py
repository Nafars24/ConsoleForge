"""WBFS (Wii) - wadah blok 2 MiB dengan tabel wlba. Mode:
   full : semua blok disimpan (isi identik, hanya ganti wadah)
   game : buang update partition + area di luar partisi (dari tabel partisi Wii, tanpa kunci/AES)
   scrub: seperti game + buang cluster tak terpakai di dalam partisi (baca FST; butuh common key dari user)
Mendukung file terpisah (.wbfs, .wbf1, ...) untuk FAT32."""
import os
import struct

from common import ForgeError, check_cancel, human, noop

SL_SIZE, DL_SIZE = 4_699_979_776, 8_511_160_320
WII_SEC = 0x8000
HD_S, BS_S = 9, 21                           # sektor 512 B, blok wbfs 2 MiB
TABLE_N = -(-(143432 * 2 * WII_SEC) // (1 << BS_S))     # entri wlba per disc (dual layer)
PRIV_OFF, PRIV_TAG = 0x100000, b"FORGESZ\0"  # catatan privat: ukuran ISO asli (diabaikan tool lain)
SPLIT_4G = 0xFFFF8000                        # 4 GiB - 32 KiB (batas FAT32)


def is_wii(head):
    return len(head) >= 0x1C and struct.unpack(">I", head[0x18:0x1C])[0] == 0x5D1C9EA3


def _be32(b, o):
    return struct.unpack(">I", b[o:o + 4])[0]


def wii_partitions(f):
    """[(offset, type), ...] dari tabel partisi di 0x40000 (tidak terenkripsi)."""
    f.seek(0x40000)
    t = f.read(32)
    parts = []
    for g in range(4):
        cnt, off = _be32(t, g * 8), _be32(t, g * 8 + 4) << 2
        if not cnt:
            continue
        if cnt > 16:
            raise ForgeError("tabel partisi tidak masuk akal - pakai mode full")
        f.seek(off)
        e = f.read(cnt * 8)
        if len(e) < cnt * 8:
            raise ForgeError("tabel partisi terpotong - pakai mode full")
        for i in range(cnt):
            parts.append((_be32(e, i * 8) << 2, _be32(e, i * 8 + 4)))
    return parts


def used_ranges(f, size):
    ranges = [(0, 0x50000)]
    for po, pt in wii_partitions(f):
        if pt == 1:                                  # update partition: dibuang
            continue
        f.seek(po + 0x2B8)
        h = f.read(8)
        if len(h) < 8:
            raise ForgeError("header partisi terpotong - pakai mode full")
        do, ds = _be32(h, 0) << 2, _be32(h, 4) << 2
        end = po + do + ds
        if po < 0x50000 or ds == 0 or end > size + WII_SEC:
            raise ForgeError(f"partisi di 0x{po:X} tidak valid - pakai mode full")
        ranges.append((po, min(end, size)))
    return ranges


class SplitWriter:
    def __init__(self, base, split=0):
        self.base, self.split, self.pos, self.files = base, split or (1 << 62), 0, {}

    @staticmethod
    def part_name(base, i):
        if i == 0:
            return base
        root, ext = os.path.splitext(base)
        return root + (ext[:-1] + str(i) if ext.lower() == ".wbfs" else ext + str(i))

    def _f(self, i):
        if i not in self.files:
            self.files[i] = open(self.part_name(self.base, i), "w+b")
        return self.files[i]

    def write(self, data):
        while data:
            i, o = divmod(self.pos, self.split)
            chunk = data[:self.split - o]
            f = self._f(i)
            f.seek(o)
            f.write(chunk)
            self.pos += len(chunk)
            data = data[len(chunk):]

    def write_at(self, off, data):
        keep, self.pos = self.pos, off
        self.write(data)
        self.pos = keep

    def close(self):
        for f in self.files.values():
            f.close()

    def remove_all(self):
        self.close()
        for i in self.files:
            try:
                os.remove(self.part_name(self.base, i))
            except OSError:
                pass


class SplitReader:
    def __init__(self, base):
        self.files, self.sizes, i = [], [], 0
        while True:
            p = SplitWriter.part_name(base, i)
            if not os.path.exists(p):
                break
            self.files.append(open(p, "rb"))
            self.sizes.append(os.path.getsize(p))
            i += 1
        if not self.files:
            raise ForgeError(f"file tidak ditemukan: {base}")
        self.total = sum(self.sizes)

    def read_at(self, off, n):
        out = bytearray()
        i = 0
        while n > 0 and i < len(self.files):
            if off >= self.sizes[i]:
                off -= self.sizes[i]
                i += 1
                continue
            self.files[i].seek(off)
            chunk = self.files[i].read(min(n, self.sizes[i] - off))
            out += chunk
            n -= len(chunk)
            off, i = 0, i + 1
        return bytes(out)

    def close(self):
        for f in self.files:
            f.close()


def _build_sector0(header, table, n_wbfs_sec, size):
    bs, hd = 1 << BS_S, 1 << HD_S
    s0 = bytearray(bs)
    s0[0:12] = struct.pack(">4sIBB2x", b"WBFS", (n_wbfs_sec * bs) >> HD_S, HD_S, BS_S)
    s0[12] = 1                                            # disc_table[0] = ada disc
    s0[hd:hd + 0x100] = header
    s0[hd + 0x100:hd + 0x100 + TABLE_N * 2] = struct.pack(f">{TABLE_N}H", *table)
    nb = (n_wbfs_sec + 7) // 8
    s0[bs - nb:bs] = bytes(nb)                            # bitmap blok bebas: semua terpakai
    s0[PRIV_OFF:PRIV_OFF + 16] = PRIV_TAG + struct.pack(">Q", size)
    return bytes(s0)


def compress(src, dst, mode="game", split=0, progress=None, cancel=None, keys=None, log=None, **_):
    progress = progress or noop
    if mode not in ("game", "full", "scrub"):
        raise ForgeError("mode WBFS: game, full, atau scrub")
    if mode == "scrub" and not keys:
        raise ForgeError("mode scrub butuh Wii common key (--common-key / env WII_COMMON_KEY)")
    size, bs = os.path.getsize(src), 1 << BS_S
    with open(src, "rb") as fi:
        header = fi.read(0x100)
        if not is_wii(header):
            raise ForgeError("bukan image Wii (magic 0x5D1C9EA3 tidak ada di 0x18)")
        if size > DL_SIZE + bs or size < 0x50000:
            raise ForgeError("ukuran ISO Wii tidak valid")
        nblk = -(-size // bs)
        if nblk > TABLE_N:
            raise ForgeError("image terlalu besar untuk WBFS")
        secs = None                                       # peta sektor 0x8000 terpakai (hanya mode scrub)
        if mode == "full":
            keep = set(range(nblk))
        elif mode == "scrub":
            import wii
            secs = wii.build_usage(fi, size, keys, log)
            per = bs // WII_SEC
            keep = {i // per for i, u in enumerate(secs) if u}
        else:
            keep = set()
            for a, b in used_ranges(fi, size):
                keep.update(range(a // bs, -(-b // bs)))
            keep = {k for k in keep if k < nblk}
        order = sorted(keep)
        table = [0] * TABLE_N
        out = SplitWriter(dst, split)
        try:
            out.write(bytes(bs))                          # sektor 0 (diisi di akhir)
            for n, b in enumerate(order, 1):
                check_cancel(cancel)
                fi.seek(b * bs)
                data = fi.read(bs)
                data += bytes(bs - len(data))
                if secs is not None:                      # nol-kan sektor yang tidak dipakai
                    ba, per = bytearray(data), bs // WII_SEC
                    for j in range(per):
                        si = b * per + j
                        if si >= len(secs) or not secs[si]:
                            ba[j * WII_SEC:(j + 1) * WII_SEC] = bytes(WII_SEC)
                    data = bytes(ba)
                out.write(data)
                table[b] = n
                if n % 16 == 0:
                    progress(n / len(order), f"blok {n:,}/{len(order):,}")
            out.write_at(0, _build_sector0(header, table, len(order) + 1, size))
            out.close()
        except BaseException:
            out.remove_all()
            raise
    return dst


class WbfsReader:
    def __init__(self, path):
        self.path = path
        self.f = SplitReader(path)
        try:
            h = self.f.read_at(0, 512)
            magic, self.n_hd, hd_s, self.bs_s = struct.unpack(">4sIBB", h[:10])
            if magic != b"WBFS":
                raise ForgeError("bukan file WBFS")
            if not 9 <= hd_s <= 12 or not 15 <= self.bs_s <= 28:
                raise ForgeError("header WBFS tidak valid")
            self.hd, self.block_size = 1 << hd_s, 1 << self.bs_s
            if h[12] == 0:
                raise ForgeError("WBFS ini kosong (tidak ada disc)")
            if any(h[13:512]):
                raise ForgeError("WBFS berisi >1 disc - belum didukung")
            n = min(-(-(143432 * 2 * WII_SEC) // self.block_size),
                    (self.block_size - self.hd - 0x100) // 2)
            di = self.f.read_at(self.hd, 0x100 + n * 2)
            self.header = di[:0x100]
            self.table = struct.unpack(f">{n}H", di[0x100:0x100 + n * 2])
            priv = self.f.read_at(PRIV_OFF, 16)
            if priv[:8] == PRIV_TAG:
                self.size = struct.unpack(">Q", priv[8:])[0]
            else:                                         # file dari tool lain: tebak ukuran standar
                used = [i for i, e in enumerate(self.table) if e]
                if not used:
                    raise ForgeError("WBFS tidak berisi data")
                end = (used[-1] + 1) * self.block_size
                self.size = SL_SIZE if end <= SL_SIZE else DL_SIZE if end <= DL_SIZE else end
            self.n = -(-self.size // self.block_size)
            if self.n > len(self.table):
                raise ForgeError("ukuran disc melebihi tabel wlba")
        except Exception:
            self.f.close()
            raise

    def close(self):
        self.f.close()

    def blocks(self):
        bs = self.block_size
        for i in range(self.n):
            e = self.table[i]
            if e == 0:
                data = bytes(bs)
            else:
                data = self.f.read_at(e << self.bs_s, bs)
                if len(data) != bs:
                    raise ForgeError(f"blok {i}: file terpotong")
            need = bs if i < self.n - 1 else self.size - (self.n - 1) * bs
            yield data[:need]

    def compare_stored(self, src, progress=None, cancel=None, used=None):
        """Cek isi WBFS terhadap sumber. Tanpa `used`: tiap sektor 32 KiB harus identik ATAU nol
        (nol = dibuang/scrub). Dengan `used` (peta sektor terpakai): sektor terpakai WAJIB identik."""
        progress = progress or noop
        if os.path.getsize(src) != self.size:
            return False, "ukuran ISO berbeda dengan ukuran di WBFS"
        zero, stored, bad = bytes(WII_SEC), 0, None
        with open(src, "rb") as f:
            for i, b in enumerate(self.blocks()):
                check_cancel(cancel)
                if self.table[i]:
                    stored += 1
                    f.seek(i * self.block_size)
                    s = f.read(len(b))
                    for o in range(0, len(b), WII_SEC):
                        a, c = b[o:o + WII_SEC], s[o:o + WII_SEC]
                        si = (i * self.block_size + o) // WII_SEC
                        must = used is not None and si < len(used) and used[si]
                        if a != c and (must or a != bytes(len(a))):
                            return False, f"sektor 0x{si * WII_SEC:X} berbeda dari sumber"
                elif used is not None:
                    f.seek(i * self.block_size)
                    per = self.block_size // WII_SEC
                    if any(used[i * per:(i + 1) * per]):
                        return False, f"blok {i} berisi sektor terpakai tetapi tidak tersimpan"
                if i % 64 == 0:
                    progress(i / self.n, "membandingkan")
        return True, f"{stored}/{self.n} blok tersimpan; sektor terpakai identik dengan sumber"

    def info(self):
        stored = sum(1 for e in self.table[:self.n] if e)
        fsz = self.f.total
        title = self.header[0x20:0x60].split(b"\0")[0].decode("ascii", "replace")
        gid = self.header[:6].decode("ascii", "replace")
        return "\n".join([
            f"File        : {self.path}  ({len(self.f.files)} bagian)",
            f"Format      : WBFS, sektor HD {self.hd} B, blok {human(self.block_size)}",
            f"Game ID     : {gid}   Judul: {title}",
            f"Ukuran disc : {self.size:,} bytes ({human(self.size)})",
            f"Blok        : {stored:,}/{self.n:,} tersimpan",
            f"Ukuran file : {fsz:,} bytes ({100 * fsz / max(1, self.size):.1f}%)",
        ])
