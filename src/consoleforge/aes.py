"""AES-128 murni Python. Dekripsi CBC pakai T-table (cukup cepat untuk header/FST);
enkripsi byte-wise hanya untuk tes. Otomatis pakai pycryptodome bila terpasang."""
import struct

try:
    from Crypto.Cipher import AES as _CRYPTO
except Exception:   # noqa
    _CRYPTO = None


def _rotl8(x, n):
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _gmul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = ((a << 1) ^ (0x11B if a & 0x80 else 0)) & 0xFFFF
        b >>= 1
    return r & 0xFF


def _tables():
    sbox, p, q = [0] * 256, 1, 1
    while True:
        p = (p ^ (p << 1) ^ (0x11B if p & 0x80 else 0)) & 0xFF
        q = (q ^ (q << 1)) & 0xFF
        q = (q ^ (q << 2)) & 0xFF
        q = (q ^ (q << 4)) & 0xFF
        if q & 0x80:
            q ^= 0x09
        sbox[p] = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    td0 = [(_gmul(inv[x], 14) << 24) | (_gmul(inv[x], 9) << 16) | (_gmul(inv[x], 13) << 8) | _gmul(inv[x], 11)
           for x in range(256)]
    ror = lambda w, n: ((w >> n) | (w << (32 - n))) & 0xFFFFFFFF
    return sbox, inv, td0, [ror(w, 8) for w in td0], [ror(w, 16) for w in td0], [ror(w, 24) for w in td0]


SBOX, INV, TD0, TD1, TD2, TD3 = _tables()


def _expand(key):
    w = list(struct.unpack(">4I", key))
    rcon = 1
    for i in range(4, 44):
        t = w[i - 1]
        if i % 4 == 0:
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
            t = (SBOX[t >> 24] << 24) | (SBOX[(t >> 16) & 255] << 16) | (SBOX[(t >> 8) & 255] << 8) | SBOX[t & 255]
            t ^= rcon << 24
            rcon = _gmul(rcon, 2)
        w.append(w[i - 4] ^ t)
    return w


class AES128:
    def __init__(self, key):
        if len(key) != 16:
            raise ValueError("kunci AES-128 harus 16 byte")
        self.key = bytes(key)
        self.rk = _expand(self.key)
        rk = self.rk
        dk = list(rk[40:44])                           # equivalent inverse cipher
        for r in range(1, 10):
            for c in range(4):
                w = rk[40 - 4 * r + c]
                dk.append(TD0[SBOX[w >> 24]] ^ TD1[SBOX[(w >> 16) & 255]] ^ TD2[SBOX[(w >> 8) & 255]] ^ TD3[SBOX[w & 255]])
        dk += rk[0:4]
        self.dk = dk

    def decrypt_cbc(self, iv, data):
        if len(data) % 16 or len(iv) != 16:
            raise ValueError("panjang data/iv tidak valid")
        if _CRYPTO is not None:
            return _CRYPTO.new(self.key, _CRYPTO.MODE_CBC, iv).decrypt(data)
        n = len(data) // 4
        words = struct.unpack(f">{n}I", data)
        p0, p1, p2, p3 = struct.unpack(">4I", iv)
        dk, inv = self.dk, INV
        td0, td1, td2, td3 = TD0, TD1, TD2, TD3
        out = []
        for i in range(0, n, 4):
            c0, c1, c2, c3 = words[i:i + 4]
            s0, s1, s2, s3 = c0 ^ dk[0], c1 ^ dk[1], c2 ^ dk[2], c3 ^ dk[3]
            k = 4
            for _ in range(9):
                t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 255] ^ td2[(s2 >> 8) & 255] ^ td3[s1 & 255] ^ dk[k]
                t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 255] ^ td2[(s3 >> 8) & 255] ^ td3[s2 & 255] ^ dk[k + 1]
                t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 255] ^ td2[(s0 >> 8) & 255] ^ td3[s3 & 255] ^ dk[k + 2]
                t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 255] ^ td2[(s1 >> 8) & 255] ^ td3[s0 & 255] ^ dk[k + 3]
                s0, s1, s2, s3 = t0, t1, t2, t3
                k += 4
            o0 = ((inv[s0 >> 24] << 24) | (inv[(s3 >> 16) & 255] << 16) | (inv[(s2 >> 8) & 255] << 8) | inv[s1 & 255]) ^ dk[40]
            o1 = ((inv[s1 >> 24] << 24) | (inv[(s0 >> 16) & 255] << 16) | (inv[(s3 >> 8) & 255] << 8) | inv[s2 & 255]) ^ dk[41]
            o2 = ((inv[s2 >> 24] << 24) | (inv[(s1 >> 16) & 255] << 16) | (inv[(s0 >> 8) & 255] << 8) | inv[s3 & 255]) ^ dk[42]
            o3 = ((inv[s3 >> 24] << 24) | (inv[(s2 >> 16) & 255] << 16) | (inv[(s1 >> 8) & 255] << 8) | inv[s0 & 255]) ^ dk[43]
            out += (o0 ^ p0, o1 ^ p1, o2 ^ p2, o3 ^ p3)
            p0, p1, p2, p3 = c0, c1, c2, c3
        return struct.pack(f">{n}I", *out)

    # ---- hanya untuk tes: enkripsi byte-wise ----
    def encrypt_block(self, blk):
        rk = struct.pack(">44I", *self.rk)
        s = [blk[i] ^ rk[i] for i in range(16)]
        for r in range(1, 11):
            s = [SBOX[b] for b in s]
            s = [s[((c + rr) % 4) * 4 + rr] for c in range(4) for rr in range(4)]
            if r < 10:
                m = []
                for c in range(4):
                    a = s[c * 4:c * 4 + 4]
                    m += [_gmul(a[0], 2) ^ _gmul(a[1], 3) ^ a[2] ^ a[3], a[0] ^ _gmul(a[1], 2) ^ _gmul(a[2], 3) ^ a[3],
                          a[0] ^ a[1] ^ _gmul(a[2], 2) ^ _gmul(a[3], 3), _gmul(a[0], 3) ^ a[1] ^ a[2] ^ _gmul(a[3], 2)]
                s = m
            s = [s[i] ^ rk[r * 16 + i] for i in range(16)]
        return bytes(s)

    def encrypt_cbc(self, iv, data):
        out, prev = bytearray(), iv
        for i in range(0, len(data), 16):
            prev = self.encrypt_block(bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
            out += prev
        return bytes(out)


def cbc_decrypt(key, iv, data):
    return AES128(key).decrypt_cbc(iv, data)
