#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ConsoleForge - AIO converter image disc konsol (pure Python).

  Target : CHD (via chdforge) | CSO | ZSO | GCZ | WBFS
  Sumber : ISO / CUE / GDI, atau container lain (CSO/ZSO/GCZ/WBFS/CHD) -> otomatis lewat ISO sementara

  python consoleforge.py                         # GUI
  python consoleforge.py convert game.iso --to cso -o out/ --verify
  python consoleforge.py extract game.cso
  python consoleforge.py detect  *.iso
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
import tempfile
import threading

import chdforge
import cso
import gcz
import wbfs
import wii
from common import (Cancelled, ForgeError, app_dir, compare_with_file, human, noop, reader_to_file)


def noop_log(m):
    pass

from version import __version__

FORMATS = {
    "chd": ".chd", "cso": ".cso", "zso": ".zso", "gcz": ".gcz", "wbfs": ".wbfs",
}
CONTAINERS = {".cso": "cso", ".zso": "zso", ".gcz": "gcz", ".wbfs": "wbfs", ".chd": "chd"}
RAW_INPUTS = (".iso", ".img", ".cue", ".gdi")
ERRORS = (ForgeError, chdforge.ChdError, OSError)

# format yang masuk akal per platform (hanya untuk saran di GUI/CLI)
SUGGEST = {
    "Wii": ["wbfs", "gcz"], "GameCube": ["gcz"], "PSP": ["cso", "zso"],
    "PS2": ["cso", "zso", "chd"], "PS1": ["chd"],
}


# --------------------------------------------------------------------------
# Deteksi platform
# --------------------------------------------------------------------------
def _iso9660_files(f):
    """Daftar nama (huruf besar) di root ISO9660 + extent/size, atau None."""
    f.seek(16 * 2048)
    pvd = f.read(2048)
    if len(pvd) < 2048 or pvd[1:6] != b"CD001":
        return None
    lba, size = struct.unpack("<I", pvd[158:162])[0], struct.unpack("<I", pvd[166:170])[0]
    if size > 1 << 20:
        return None
    f.seek(lba * 2048)
    data = f.read(size)
    out, i = {}, 0
    while i < len(data):
        n = data[i]
        if n == 0:                                  # record tidak melewati batas sektor
            i = (i // 2048 + 1) * 2048
            continue
        nl = data[i + 32]
        name = data[i + 33:i + 33 + nl].decode("ascii", "replace").upper().split(";")[0]
        out[name] = (struct.unpack("<I", data[i + 2:i + 6])[0],
                     struct.unpack("<I", data[i + 10:i + 14])[0], bool(data[i + 25] & 2))
        i += n
    return out


def detect(path):
    """-> (platform, game_id). platform: Wii/GameCube/PSP/PS2/PS1/Unknown"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".cso", ".zso", ".gcz", ".wbfs", ".chd"):
            return {"cso": ("PSP/PS2", ""), "zso": ("PS2", ""), "gcz": ("GameCube/Wii", ""),
                    "wbfs": ("Wii", ""), "chd": ("CHD", "")}[CONTAINERS[ext]]
        with open(path, "rb") as f:
            head = f.read(0x20)
            if wbfs.is_wii(head):
                return "Wii", head[:6].decode("ascii", "replace")
            if len(head) >= 0x20 and struct.unpack(">I", head[0x1C:0x20])[0] == 0xC2339F3D:
                return "GameCube", head[:6].decode("ascii", "replace")
            files = _iso9660_files(f)
            if files is None:
                return "Unknown", ""
            if "PSP_GAME" in files:
                return "PSP", ""
            if "SYSTEM.CNF" in files:
                lba, size, _ = files["SYSTEM.CNF"]
                f.seek(lba * 2048)
                txt = f.read(min(size, 2048)).decode("ascii", "replace")
                import re
                m = re.search(r"BOOT2\s*=\s*cdrom0:\\?([\w.]+)", txt, re.I)
                if m:
                    return "PS2", m.group(1)
                m = re.search(r"BOOT\s*=\s*cdrom:\\?([\w.]+)", txt, re.I)
                if m:
                    return "PS1", m.group(1)
    except OSError:
        pass
    return "Unknown", ""


# --------------------------------------------------------------------------
# Reader / extract / verify / info
# --------------------------------------------------------------------------
def open_reader(path):
    ext = os.path.splitext(path)[1].lower()
    kind = CONTAINERS.get(ext)
    if kind in ("cso", "zso"):
        return cso.CsoReader(path)
    if kind == "gcz":
        return gcz.GczReader(path)
    if kind == "wbfs":
        return wbfs.WbfsReader(path)
    raise ForgeError(f"'{ext}' bukan container yang bisa dibuka langsung (cso/zso/gcz/wbfs)")


def extract(src, outdir=None, force=False, split=False, progress=None, cancel=None):
    """Container -> ISO (atau BIN/CUE/GDI untuk CHD). Mengembalikan daftar file."""
    progress = progress or noop
    ext = os.path.splitext(src)[1].lower()
    outdir = outdir or os.path.dirname(os.path.abspath(src))
    os.makedirs(outdir, exist_ok=True)
    if CONTAINERS.get(ext) == "chd":
        return chdforge.extract(src, outdir, split, progress, cancel, force)
    name = os.path.splitext(os.path.basename(src))[0]
    r = open_reader(src)
    try:
        return [reader_to_file(r, os.path.join(outdir, name + ".iso"), progress, cancel, force)]
    finally:
        r.close()


def verify(path, against=None, progress=None, cancel=None):
    """-> (ok, pesan). `against`: file ISO sumber untuk dibandingkan."""
    progress = progress or noop
    ext = os.path.splitext(path)[1].lower()
    if CONTAINERS.get(ext) == "chd":
        return chdforge.verify(path, progress, cancel)
    r = open_reader(path)
    try:
        if against:
            if isinstance(r, wbfs.WbfsReader):
                return r.compare_stored(against, progress, cancel)
            return compare_with_file(r, against, progress, cancel)
        n = 0
        for b in r.blocks():                 # dekode semua blok (GCZ: sekaligus cek Adler32)
            n += len(b)
            if cancel is not None and cancel.is_set():
                raise Cancelled()
        return True, f"OK - seluruh blok terbaca ({human(n)})"
    except ERRORS as ex:
        return False, str(ex)
    finally:
        r.close()


def info(path):
    ext = os.path.splitext(path)[1].lower()
    if CONTAINERS.get(ext) == "chd":
        return chdforge.info(path)
    r = open_reader(path)
    try:
        return r.info()
    finally:
        r.close()


# --------------------------------------------------------------------------
# Convert
# --------------------------------------------------------------------------
def find_keys(opts=None):
    """Kunci Wii: opts['wii_keys'] > env > wii_keys.txt (folder skrip / folder kerja)."""
    opts = opts or {}
    if opts.get("wii_keys"):
        return opts["wii_keys"]
    return wii.load_keys(search_dirs=[os.getcwd(), app_dir()])


def _verify_wbfs(out, src, mode, keys, log, progress, cancel):
    r = wbfs.WbfsReader(out)
    try:
        if mode == "scrub":
            with open(src, "rb") as f:
                used = wii.build_usage(f, os.path.getsize(src), keys, log)
            return r.compare_stored(src, progress, cancel, used=used)
        if mode == "full":
            return compare_with_file(r, src, progress, cancel)
        return r.compare_stored(src, progress, cancel)
    finally:
        r.close()


def _scaled(progress, lo, hi):
    return lambda f, m="": progress(lo + (hi - lo) * f, m)


def _remove_output(out, fmt):
    paths = [wbfs.SplitWriter.part_name(out, i) for i in range(8)] if fmt == "wbfs" else [out]
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


def convert(src, fmt, outdir=None, force=False, opts=None, progress=None, cancel=None):
    """src (ISO/CUE/GDI atau container) -> fmt (chd/cso/zso/gcz/wbfs). Mengembalikan path hasil."""
    opts, progress = opts or {}, progress or noop
    if fmt not in FORMATS:
        raise ForgeError(f"format tujuan tidak dikenal: {fmt}")
    ext = os.path.splitext(src)[1].lower()
    name = os.path.splitext(os.path.basename(src))[0]
    outdir = outdir or os.path.dirname(os.path.abspath(src))
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, name + FORMATS[fmt])
    if os.path.exists(out) and not force:
        raise ForgeError(f"{os.path.basename(out)} sudah ada (pakai overwrite)")
    if ext not in RAW_INPUTS and ext not in CONTAINERS:
        raise ForgeError(f"ekstensi '{ext}' tidak didukung")
    if ext in (".cue", ".gdi") and fmt != "chd":
        raise ForgeError(f"{ext} (CD multi-track/GD-ROM) hanya bisa ke CHD")
    if CONTAINERS.get(ext) == fmt:
        raise ForgeError("file sudah berformat itu")

    tmpdir, work = None, src
    phase = (0.0, 1.0)
    try:
        if ext in CONTAINERS:                                  # transcode via ISO sementara
            tmpdir = tempfile.mkdtemp(prefix="cforge_", dir=outdir)
            files = extract(src, tmpdir, True, _scaled(progress, 0.0, 0.45), cancel)
            isos = [f for f in files if f.lower().endswith(".iso")]
            if len(isos) != 1:
                raise ForgeError("sumber ini bukan image data tunggal (CD multi-track?) - ekstrak ke BIN/CUE lalu konversi ke CHD")
            work, phase = isos[0], (0.45, 1.0)
        p = _scaled(progress, *phase)
        workers = opts.get("workers")
        if fmt == "chd":
            chdforge.convert(work, out, opts.get("iso_mode", "auto"), opts.get("codec", "lzma"),
                             workers, p, cancel)
        elif fmt in ("cso", "zso"):
            cso.compress(work, out, fmt=fmt, level=opts.get("level", 9), workers=workers,
                         progress=p, cancel=cancel)
        elif fmt == "gcz":
            gcz.compress(work, out, level=opts.get("level", 9), workers=workers, progress=p, cancel=cancel)
        else:
            keys = find_keys(opts)
            wmode = opts.get("wbfs_mode") or ("scrub" if keys else "game")
            if wmode == "scrub" and not keys:
                raise ForgeError("mode scrub butuh Wii common key (--common-key, env WII_COMMON_KEY, "
                                 "atau file wii_keys.txt)")
            log = opts.get("log") or noop
            wbfs.compress(work, out, mode=wmode, split=wbfs.SPLIT_4G if opts.get("split") else 0,
                          progress=p, cancel=cancel, keys=keys, log=log)
        if opts.get("verify"):
            if fmt == "wbfs":
                ok, msg = _verify_wbfs(out, work, wmode, keys, log, _scaled(progress, 0.95, 1.0), cancel)
            else:
                ok, msg = verify(out, None if fmt == "chd" else work, _scaled(progress, 0.95, 1.0), cancel)
            if not ok:
                _remove_output(out, fmt)
                raise ForgeError("verifikasi gagal, hasil dihapus: " + msg)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _cli_progress():
    last = [-1]

    def cb(frac, msg=""):
        pct = int(frac * 100)
        if pct != last[0]:
            last[0] = pct
            sys.stdout.write(f"\r  {pct:3d}%  {str(msg):<36}")
            sys.stdout.flush()
    return cb


def main(argv=None):
    for name in ("stdout", "stderr"):                  # exe --windowed: stdout/stderr bernilai None
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    ap = argparse.ArgumentParser(prog="consoleforge", description="AIO converter image disc konsol")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("convert")
    c.add_argument("files", nargs="+")
    c.add_argument("--to", required=True, choices=list(FORMATS))
    c.add_argument("-o", "--outdir")
    c.add_argument("--level", type=int, default=9, help="level zlib 1-9 (cso/gcz)")
    c.add_argument("--wbfs-mode", choices=["scrub", "game", "full"], default=None,
                   help="default: scrub bila ada kunci, selain itu game")
    c.add_argument("--common-key", help="Wii common key (32 hex). Alternatif: env WII_COMMON_KEY / wii_keys.txt")
    c.add_argument("--korean-key", help="kunci Korea (index 1), bila perlu")
    c.add_argument("--vwii-key", help="kunci vWii (index 2), bila perlu")
    c.add_argument("--split", action="store_true", help="WBFS: pecah 4GB (FAT32)")
    c.add_argument("--iso-mode", choices=["auto", "dvd", "cd"], default="auto", help="CHD dari ISO")
    c.add_argument("--codec", choices=["lzma", "zlib"], default="lzma", help="CHD")
    c.add_argument("-j", "--jobs", type=int)
    c.add_argument("--force", action="store_true")
    c.add_argument("--verify", action="store_true")
    e = sub.add_parser("extract")
    e.add_argument("files", nargs="+")
    e.add_argument("-o", "--outdir")
    e.add_argument("--split-tracks", action="store_true")
    e.add_argument("--force", action="store_true")
    for n in ("verify", "info", "detect"):
        sub.add_parser(n).add_argument("files", nargs="+")
    sub.add_parser("gui", help="GUI web (default)")
    sub.add_parser("gui-tk", help="GUI Tkinter (klasik)")
    a = ap.parse_args(argv)
    if a.cmd == "gui-tk":
        return run_gui()
    if a.cmd in (None, "gui"):
        import gui_web
        return gui_web.serve()
    rc = 0
    for f in a.files:
        print(f"\n== {f}")
        try:
            if a.cmd == "detect":
                plat, gid = detect(f)
                print(f"  platform: {plat} {gid}   saran format: {', '.join(SUGGEST.get(plat, list(FORMATS)))}")
            elif a.cmd == "convert":
                keys = wii.load_keys(a.common_key, a.korean_key, a.vwii_key,
                                     [os.getcwd(), app_dir()])
                opts = {"level": a.level, "wbfs_mode": a.wbfs_mode, "split": a.split, "iso_mode": a.iso_mode,
                        "codec": a.codec, "workers": a.jobs, "verify": a.verify, "wii_keys": keys,
                        "log": lambda m: print(f"\n  ! {m}")}
                out = convert(f, a.to, a.outdir, a.force, opts, _cli_progress())
                print(f"\n  -> {out}  ({human(os.path.getsize(out))})")
            elif a.cmd == "extract":
                for p in extract(f, a.outdir, a.force, a.split_tracks, _cli_progress()):
                    print(f"\n  -> {p}")
            elif a.cmd == "verify":
                ok, msg = verify(f, None, _cli_progress())
                print(f"\n  {msg}")
                rc |= 0 if ok else 1
            elif a.cmd == "info":
                print(info(f))
        except ERRORS as ex:
            print(f"\n  ERROR: {ex}")
            rc = 1
    return rc


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def run_gui():
    import queue
    import tkinter as tk
    from tkinter import filedialog, ttk

    root = tk.Tk()
    root.title(f"ConsoleForge {__version__}")
    root.geometry("900x720")
    q: "queue.Queue" = queue.Queue()
    cancel = threading.Event()
    state = {"running": False}
    items = {}

    mode = tk.StringVar(value="convert")
    target = tk.StringVar(value="cso")
    iso_mode = tk.StringVar(value="auto")
    level = tk.IntVar(value=9)
    wbfs_mode = tk.StringVar(value="auto")
    wii_key = tk.StringVar()
    split = tk.BooleanVar(value=False)
    threads = tk.IntVar(value=os.cpu_count() or 2)
    outdir = tk.StringVar()
    overwrite = tk.BooleanVar(value=False)
    verify_after = tk.BooleanVar(value=True)

    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    for text, val in (("Convert", "convert"), ("Extract -> ISO", "extract"), ("Verify", "verify"),
                      ("Info", "info"), ("Detect", "detect")):
        ttk.Radiobutton(top, text=text, value=val, variable=mode).pack(side="left", padx=6)

    mid = ttk.Frame(root, padding=(8, 0))
    mid.pack(fill="both", expand=True)
    tree = ttk.Treeview(mid, columns=("file", "plat", "status"), show="headings", height=9)
    for c, t, w in (("file", "File", 470), ("plat", "Platform", 130), ("status", "Status", 230)):
        tree.heading(c, text=t)
        tree.column(c, width=w)
    sb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")

    def exts():
        if mode.get() == "convert":
            return RAW_INPUTS + tuple(CONTAINERS)
        return tuple(CONTAINERS)

    def add_path(p):
        if p in items.values():
            return
        plat, gid = detect(p)
        iid = tree.insert("", "end", values=(p, f"{plat} {gid}".strip(), "Queued"))
        items[iid] = p

    def add_files():
        for p in filedialog.askopenfilenames(
                filetypes=[("Supported", " ".join("*" + x for x in exts())), ("All", "*.*")]):
            add_path(p)

    def add_folder():
        d = filedialog.askdirectory()
        if d:
            for r, _, fs in os.walk(d):
                for fn in sorted(fs):
                    if fn.lower().endswith(exts()):
                        add_path(os.path.join(r, fn))

    def remove_sel():
        for iid in tree.selection():
            items.pop(iid, None)
            tree.delete(iid)

    def clear():
        for iid in list(items):
            tree.delete(iid)
        items.clear()

    btns = ttk.Frame(root, padding=8)
    btns.pack(fill="x")
    for t, cmd in (("Add files", add_files), ("Add folder", add_folder), ("Remove", remove_sel), ("Clear", clear)):
        ttk.Button(btns, text=t, command=cmd).pack(side="left", padx=3)

    opt = ttk.LabelFrame(root, text="Options", padding=8)
    opt.pack(fill="x", padx=8)
    ttk.Label(opt, text="Target:").grid(row=0, column=0, sticky="w")
    ttk.Combobox(opt, textvariable=target, state="readonly", width=8, values=list(FORMATS)).grid(row=0, column=1, padx=4)
    ttk.Label(opt, text="zlib level:").grid(row=0, column=2, sticky="e")
    ttk.Spinbox(opt, from_=1, to=9, width=3, textvariable=level).grid(row=0, column=3, padx=4)
    ttk.Label(opt, text="Threads:").grid(row=0, column=4, sticky="e")
    ttk.Spinbox(opt, from_=1, to=64, width=4, textvariable=threads).grid(row=0, column=5, padx=4)
    ttk.Label(opt, text="WBFS:").grid(row=1, column=0, sticky="w", pady=4)
    ttk.Combobox(opt, textvariable=wbfs_mode, state="readonly", width=8, values=["auto", "scrub", "game", "full"]).grid(row=1, column=1)
    ttk.Checkbutton(opt, text="Split 4GB (FAT32)", variable=split).grid(row=1, column=2, columnspan=2, sticky="w")
    ttk.Label(opt, text="CHD ISO:").grid(row=1, column=4, sticky="e")
    ttk.Combobox(opt, textvariable=iso_mode, state="readonly", width=6, values=["auto", "dvd", "cd"]).grid(row=1, column=5)
    ttk.Label(opt, text="Output folder:").grid(row=2, column=0, sticky="w")
    ttk.Entry(opt, textvariable=outdir, width=64).grid(row=2, column=1, columnspan=4, sticky="we", padx=4)
    ttk.Button(opt, text="Browse", command=lambda: outdir.set(filedialog.askdirectory() or outdir.get())).grid(row=2, column=5)
    ttk.Checkbutton(opt, text="Overwrite", variable=overwrite).grid(row=3, column=0, sticky="w")
    ttk.Checkbutton(opt, text="Verify after convert", variable=verify_after).grid(row=3, column=1, columnspan=2, sticky="w")
    ttk.Label(opt, text="Wii common key:").grid(row=4, column=0, sticky="w", pady=(4, 0))
    ttk.Entry(opt, textvariable=wii_key, width=40, show="*").grid(row=4, column=1, columnspan=4, sticky="we", padx=4)
    ttk.Label(opt, text="(untuk WBFS scrub; hanya di memori, tidak disimpan)", foreground="gray").grid(row=5, column=0, columnspan=6, sticky="w")
    note = "ZSO: " + ("pakai modul lz4 (cepat)" if __import__("lz4block").HAVE_LZ4 else "tanpa modul lz4 -> lambat; pip install lz4")
    ttk.Label(opt, text=note, foreground="gray").grid(row=6, column=0, columnspan=6, sticky="w")

    pf = ttk.Frame(root, padding=8)
    pf.pack(fill="x")
    lbl = ttk.Label(pf, text="Idle")
    lbl.pack(anchor="w")
    bar = ttk.Progressbar(pf, maximum=1000)
    bar.pack(fill="x", pady=2)
    obar = ttk.Progressbar(pf, maximum=1000)
    obar.pack(fill="x", pady=2)
    log = tk.Text(root, height=9, state="disabled", wrap="word")
    log.pack(fill="both", padx=8, pady=(0, 4))

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

            def prog(frac, msg="", k=k):
                if cancel.is_set():
                    raise Cancelled()
                q.put(("prog", frac, (k + frac) / n))
            try:
                od = o["outdir"] or None
                if o["mode"] == "convert":
                    out = convert(path, o["target"], od, o["force"], o["opts"], prog, cancel)
                    q.put(("status", iid, f"OK  {human(os.path.getsize(path))} -> {human(os.path.getsize(out))}"))
                elif o["mode"] == "extract":
                    files = extract(path, od, o["force"], False, prog, cancel)
                    q.put(("log", "  -> " + ", ".join(os.path.basename(x) for x in files)))
                    q.put(("status", iid, "OK"))
                elif o["mode"] == "verify":
                    ok, msg = verify(path, None, prog, cancel)
                    q.put(("status", iid, msg if ok else "FAILED: " + msg))
                elif o["mode"] == "info":
                    q.put(("log", info(path)))
                    q.put(("status", iid, "OK"))
                else:
                    plat, gid = detect(path)
                    q.put(("log", f"{os.path.basename(path)}: {plat} {gid} | saran: {', '.join(SUGGEST.get(plat, list(FORMATS)))}"))
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
                    bar["value"], obar["value"] = m[1] * 1000, m[2] * 1000
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
        try:
            keys = wii.load_keys(wii_key.get().strip() or None, None, None,
                                 [os.getcwd(), app_dir()])
        except ForgeError as ex:
            logln(f"Key tidak valid: {ex}")
            state["running"] = False
            start_btn.configure(state="normal")
            cancel_btn.configure(state="disabled")
            return
        wm = wbfs_mode.get()
        o = {"mode": mode.get(), "target": target.get(), "outdir": outdir.get().strip(), "force": overwrite.get(),
             "opts": {"level": level.get(), "wbfs_mode": None if wm == "auto" else wm, "split": split.get(),
                      "iso_mode": iso_mode.get(), "workers": max(1, threads.get()), "verify": verify_after.get(),
                      "wii_keys": keys, "log": lambda m: q.put(("log", "! " + m))}}
        threading.Thread(target=worker, args=(list(items.items()), o), daemon=True).start()

    ctl = ttk.Frame(root, padding=8)
    ctl.pack(fill="x")
    start_btn = ttk.Button(ctl, text="Start", command=start)
    start_btn.pack(side="left")
    cancel_btn = ttk.Button(ctl, text="Cancel", command=cancel.set, state="disabled")
    cancel_btn.pack(side="left", padx=6)
    logln(f"ConsoleForge {__version__}. Tambah file, pilih mode/target, lalu Start.")
    poll()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
