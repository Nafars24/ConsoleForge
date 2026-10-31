"""Decoder FLAC minimal (hanya frame, tanpa header fLaC/STREAMINFO) untuk codec CHD 'cdfl' dan 'flac'.
Setiap frame diverifikasi dengan CRC-8 header dan CRC-16 frame; salah parse = error, bukan data rusak."""
import operator
import struct

from common import ForgeError


def _table(poly, bits):
    top, mask, t = 1 << (bits - 1), (1 << bits) - 1, []
    for i in range(256):
        c = i << (bits - 8)
        for _ in range(8):
            c = ((c << 1) ^ poly) if c & top else (c << 1)
        t.append(c & mask)
    return t


_T8, _T16 = _table(0x07, 8), _table(0x8005, 16)


def _crc8(data):
    c = 0
    for b in data:
        c = _T8[c ^ b]
    return c


def _crc16(data):
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ _T16[(c >> 8) ^ b]
    return c


class _Bits:
    __slots__ = ("b", "p")

    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def u(self, n):
        if n == 0:
            return 0
        p = self.p
        s, e = p >> 3, (p + n + 7) >> 3
        if e > len(self.b):
            raise ForgeError("FLAC: data terpotong")
        self.p = p + n
        return (int.from_bytes(self.b[s:e], "big") >> ((e - s) * 8 - (p & 7) - n)) & ((1 << n) - 1)

    def s(self, n):
        v = self.u(n)
        return v - (1 << n) if v >> (n - 1) else v

    def unary(self):
        count, b = 0, self.b
        while True:
            p = self.p
            s = p >> 3
            if s >= len(b):
                raise ForgeError("FLAC: data terpotong")
            byte = b[s] & (0xFF >> (p & 7))
            if byte:
                lz = 8 - byte.bit_length() - (p & 7)
                self.p = p + lz + 1
                return count + lz
            count += 8 - (p & 7)
            self.p = (s + 1) * 8


def _residual(br, n, order):
    method = br.u(2)
    if method > 1:
        raise ForgeError("FLAC: metode residual tidak valid")
    pbits = 4 if method == 0 else 5
    esc, po = (1 << pbits) - 1, br.u(4)
    parts = 1 << po
    if n % parts or (n >> po) < order:
        raise ForgeError("FLAC: partisi residual tidak valid")
    res = []
    for p in range(parts):
        cnt = (n >> po) - (order if p == 0 else 0)
        k = br.u(pbits)
        if k == esc:
            bits = br.u(5)
            res += [br.s(bits) for _ in range(cnt)] if bits else [0] * cnt
        else:
            for _ in range(cnt):
                v = (br.unary() << k) | br.u(k)
                res.append((v >> 1) ^ -(v & 1))
    return res


def _subframe(br, n, bps):
    if br.u(1):
        raise ForgeError("FLAC: padding subframe tidak nol")
    t, wasted = br.u(6), 0
    if br.u(1):
        wasted = br.unary() + 1
    b = bps - wasted
    if t == 0:
        out = [br.s(b)] * n
    elif t == 1:
        out = [br.s(b) for _ in range(n)]
    elif 8 <= t <= 12:
        order = t - 8
        out = [br.s(b) for _ in range(order)]
        res = _residual(br, n, order)
        if order == 0:
            out = res
        else:
            for r in res:
                a = out
                if order == 1:
                    p = a[-1]
                elif order == 2:
                    p = 2 * a[-1] - a[-2]
                elif order == 3:
                    p = 3 * a[-1] - 3 * a[-2] + a[-3]
                else:
                    p = 4 * a[-1] - 6 * a[-2] + 4 * a[-3] - a[-4]
                a.append(p + r)
    elif t >= 32:
        order = t - 31
        out = [br.s(b) for _ in range(order)]
        prec = br.u(4) + 1
        if prec == 16:
            raise ForgeError("FLAC: presisi LPC tidak valid")
        shift = br.s(5)
        rc = [br.s(prec) for _ in range(order)][::-1]
        res = _residual(br, n, order)
        mul = operator.mul
        for i, r in enumerate(res):
            out.append((sum(map(mul, rc, out[i:i + order])) >> shift) + r)
    else:
        raise ForgeError("FLAC: tipe subframe tidak valid")
    if wasted:
        out = [x << wasted for x in out]
    if len(out) != n:
        raise ForgeError("FLAC: jumlah sampel subframe salah")
    return out


_BS = {1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608}
_SS = {0: 16, 1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32}


def decode_frame(buf, pos):
    """-> (list_per_kanal, jumlah_sampel, posisi_byte_setelah_frame)"""
    br = _Bits(buf, pos * 8)
    if br.u(14) != 0x3FFE:
        raise ForgeError("FLAC: sync frame tidak ditemukan")
    br.u(2)
    bsc, src, ca, ssc = br.u(4), br.u(4), br.u(4), br.u(3)
    br.u(1)
    first = br.u(8)
    for _ in range(max(8 - ((~first) & 0xFF).bit_length() - 1, 0)):
        br.u(8)
    if bsc in _BS:
        bs = _BS[bsc]
    elif bsc == 6:
        bs = br.u(8) + 1
    elif bsc == 7:
        bs = br.u(16) + 1
    elif bsc >= 8:
        bs = 256 << (bsc - 8)
    else:
        raise ForgeError("FLAC: block size tidak valid")
    if src == 12:
        br.u(8)
    elif src in (13, 14):
        br.u(16)
    hend = br.p >> 3
    if br.u(8) != _crc8(buf[pos:hend]):
        raise ForgeError("FLAC: CRC-8 header salah")
    if ssc not in _SS or ca > 10:
        raise ForgeError("FLAC: parameter header tidak valid")
    bps = _SS[ssc]
    nch = ca + 1 if ca < 8 else 2
    ch = []
    for c in range(nch):
        side = (ca == 8 and c == 1) or (ca == 9 and c == 0) or (ca == 10 and c == 1)
        ch.append(_subframe(br, bs, bps + (1 if side else 0)))
    br.p = (br.p + 7) & ~7
    end = br.p >> 3
    if br.u(16) != _crc16(buf[pos:end]):
        raise ForgeError("FLAC: CRC-16 frame salah (data korup atau salah parse)")
    if ca == 8:
        ch[1] = [l - s for l, s in zip(ch[0], ch[1])]
    elif ca == 9:
        ch[0] = [s + r for s, r in zip(ch[0], ch[1])]
    elif ca == 10:
        m2 = [(m << 1) | (s & 1) for m, s in zip(ch[0], ch[1])]
        ch = [[(m + s) >> 1 for m, s in zip(m2, ch[1])], [(m - s) >> 1 for m, s in zip(m2, ch[1])]]
    return ch, bs, br.p >> 3


def decode_stream(data, nsamples, offset=0):
    """Decode frame berurutan sampai `nsamples` sampel/kanal. -> ([L, R], posisi_byte_akhir)"""
    chans, pos, got = [[], []], offset, 0
    while got < nsamples:
        ch, bs, pos = decode_frame(data, pos)
        if len(ch) != 2:
            raise ForgeError("FLAC: harus stereo")
        chans[0] += ch[0]
        chans[1] += ch[1]
        got += bs
    if got != nsamples:
        raise ForgeError("FLAC: jumlah sampel tidak sesuai ukuran hunk")
    return chans, pos


def pcm_bytes(chans, big_endian):
    n = len(chans[0])
    inter = [0] * (2 * n)
    inter[0::2], inter[1::2] = chans
    try:
        return struct.pack((">" if big_endian else "<") + f"{2 * n}h", *inter)
    except struct.error:
        raise ForgeError("FLAC: sampel di luar jangkauan 16-bit")
