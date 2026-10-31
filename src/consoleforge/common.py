"""Helper bersama untuk modul ConsoleForge."""
import collections
import itertools
import os
import sys
from concurrent.futures import ThreadPoolExecutor


class ForgeError(Exception):
    pass


class Cancelled(Exception):
    pass


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


def noop(frac, msg=""):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


_END = object()


def ordered_map(fn, items, workers=None, ahead=None):
    """map paralel berurutan dengan lookahead terbatas (hemat memori)."""
    workers = workers or os.cpu_count() or 2
    ahead = ahead or workers * 4
    with ThreadPoolExecutor(workers) as ex:
        pend, it = collections.deque(), iter(items)
        for x in itertools.islice(it, ahead):
            pend.append(ex.submit(fn, x))
        while pend:
            r = pend.popleft().result()
            nxt = next(it, _END)
            if nxt is not _END:
                pend.append(ex.submit(fn, nxt))
            yield r


def iter_blocks(path, bs):
    """Baca file per blok bs byte; blok terakhir di-pad nol."""
    with open(path, "rb") as f:
        while True:
            b = f.read(bs)
            if not b:
                return
            if len(b) < bs:
                b += bytes(bs - len(b))
            yield b


def reader_to_file(reader, dst, progress=None, cancel=None, force=False):
    """Tulis seluruh isi reader.blocks() ke file dst (atomik via .part)."""
    progress = progress or noop
    if os.path.exists(dst) and not force:
        raise ForgeError(f"{os.path.basename(dst)} sudah ada (pakai overwrite)")
    tmp = dst + ".part"
    done = 0
    try:
        with open(tmp, "wb") as fo:
            for i, b in enumerate(reader.blocks()):
                check_cancel(cancel)
                fo.write(b)
                done += len(b)
                if i % 128 == 0:
                    progress(min(1.0, done / max(1, reader.size)), human(done))
        if done != reader.size:
            raise ForgeError("ukuran hasil tidak sama dengan ukuran di header")
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return dst


def compare_with_file(reader, src, progress=None, cancel=None):
    """Bandingkan isi reader dengan file sumber. -> (ok, pesan)"""
    progress = progress or noop
    if os.path.getsize(src) != reader.size:
        return False, f"ukuran beda: sumber {os.path.getsize(src):,} vs hasil {reader.size:,}"
    off = 0
    with open(src, "rb") as f:
        for i, b in enumerate(reader.blocks()):
            check_cancel(cancel)
            s = f.read(len(b))
            if s != b:
                return False, f"data berbeda di sekitar offset 0x{off:X}"
            off += len(b)
            if i % 128 == 0:
                progress(off / max(1, reader.size), "membandingkan")
    return True, "identik dengan sumber"


# ---- lokasi file (script biasa maupun PyInstaller "frozen") ----
def resource_dir():
    """Folder aset bawaan (ui.html). PyInstaller mengekstrak ke sys._MEIPASS."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def app_dir():
    """Folder program (tempat exe / skrip); dipakai untuk mencari wii_keys.txt."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def user_data_dir():
    """Folder pengaturan & log per-user (bisa di-override env CONSOLEFORGE_HOME)."""
    d = os.environ.get("CONSOLEFORGE_HOME")
    if not d:
        base = os.environ.get("APPDATA") if os.name == "nt" else os.path.join(os.path.expanduser("~"), ".config")
        d = os.path.join(base or os.path.expanduser("~"), "ConsoleForge" if os.name == "nt" else "consoleforge")
    os.makedirs(d, exist_ok=True)
    return d
