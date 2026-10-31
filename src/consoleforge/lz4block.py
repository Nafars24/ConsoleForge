"""LZ4 block format (tanpa frame). Pakai modul `lz4` bila ada, kalau tidak: fallback murni Python."""
import struct

from common import ForgeError

try:
    import lz4.block as _l
    HAVE_LZ4 = True
except Exception:   # noqa
    _l = None
    HAVE_LZ4 = False


def _ext(out, v):
    while v >= 255:
        out.append(255)
        v -= 255
    out.append(v)


def _emit(out, src, a, b, offset, mlen):
    ll = b - a
    ml = mlen - 4 if mlen else 0
    out.append(((15 if ll >= 15 else ll) << 4) | (15 if ml >= 15 else ml))
    if ll >= 15:
        _ext(out, ll - 15)
    out += src[a:b]
    if mlen:
        out += struct.pack("<H", offset)
        if ml >= 15:
            _ext(out, ml - 15)


def _py_compress(src):
    n, out = len(src), bytearray()
    if n < 13:                       # terlalu kecil untuk match: literal saja
        _emit(out, src, 0, n, 0, 0)
        return bytes(out)
    ht, anchor, ip = {}, 0, 0
    last_start, match_limit = n - 12, n - 5   # aturan spesifikasi LZ4
    while ip <= last_start:
        key = src[ip:ip + 4]
        ref = ht.get(key)
        ht[key] = ip
        if ref is not None and ip - ref <= 65535:
            ml = 4
            while ip + ml < match_limit and src[ref + ml] == src[ip + ml]:
                ml += 1
            _emit(out, src, anchor, ip, ip - ref, ml)
            ip += ml
            anchor = ip
        else:
            ip += 1
    _emit(out, src, anchor, n, 0, 0)
    return bytes(out)


def _py_decompress(src, size):
    out, i, n = bytearray(), 0, len(src)
    try:
        while len(out) < size:
            tok = src[i]
            i += 1
            ll = tok >> 4
            if ll == 15:
                while True:
                    b = src[i]
                    i += 1
                    ll += b
                    if b != 255:
                        break
            out += src[i:i + ll]
            i += ll
            if len(out) >= size:
                break
            off = src[i] | (src[i + 1] << 8)
            i += 2
            if off == 0 or off > len(out):
                raise ForgeError("LZ4: offset tidak valid")
            ml = tok & 15
            if ml == 15:
                while True:
                    b = src[i]
                    i += 1
                    ml += b
                    if b != 255:
                        break
            ml += 4
            st = len(out) - off
            if off >= ml:
                out += out[st:st + ml]
            else:
                out += (bytes(out[st:]) * (ml // off + 1))[:ml]
    except IndexError:
        raise ForgeError("LZ4: data terpotong")
    if len(out) != size:
        raise ForgeError("LZ4: ukuran hasil tidak cocok")
    return bytes(out)


def scan_end(src, size):
    """Panjang byte aktual blok LZ4 (abaikan padding align) dengan menelusuri sequence."""
    produced, i = 0, 0
    try:
        while produced < size:
            tok = src[i]
            i += 1
            ll = tok >> 4
            if ll == 15:
                while True:
                    b = src[i]
                    i += 1
                    ll += b
                    if b != 255:
                        break
            i += ll
            produced += ll
            if produced >= size:
                return i
            i += 2
            ml = tok & 15
            if ml == 15:
                while True:
                    b = src[i]
                    i += 1
                    ml += b
                    if b != 255:
                        break
            produced += ml + 4
    except IndexError:
        raise ForgeError("LZ4: data terpotong")
    return i


def compress(data):
    if HAVE_LZ4:
        return _l.compress(data, mode="high_compression", compression=12, store_size=False)
    return _py_compress(data)


def decompress(data, size):
    if HAVE_LZ4:
        end = scan_end(data, size)
        try:
            return _l.decompress(data[:end], uncompressed_size=size)
        except Exception as ex:   # noqa
            raise ForgeError(f"LZ4: {ex}")
    return _py_decompress(data, size)
