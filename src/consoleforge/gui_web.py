"""GUI web lokal ConsoleForge: server stdlib di 127.0.0.1 + ui.html. Tanpa dependency."""
import json
import os
import secrets
import string
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import time
import traceback

import consoleforge as CF
import wii
from common import Cancelled, app_dir, resource_dir, user_data_dir
from version import __version__

HERE = resource_dir()
LOCK = threading.Lock()
ST = {"running": False, "jobs": {}, "log": [], "overall": 0.0}
BEAT = {"last": 0.0, "seen": False}
QUIT = threading.Event()
CANCEL = threading.Event()


def _log(msg, c=""):
    with LOCK:
        ST["log"].append({"m": msg, "c": c})


def _exts(mode):
    return (CF.RAW_INPUTS + tuple(CF.CONTAINERS)) if mode == "convert" else tuple(CF.CONTAINERS)


def _strip_secrets(d):
    """Buang field 'key' di semua level (defense in depth)."""
    if isinstance(d, dict):
        d.pop("key", None)
        for v in d.values():
            _strip_secrets(v)
    elif isinstance(d, list):
        for v in d:
            _strip_secrets(v)


def list_dir(path, mode):
    if not path:
        path = os.path.expanduser("~")
    path = os.path.abspath(path)
    if os.name == "nt" and path in ("", "\\"):
        return {"path": "", "parent": "", "entries": [
            {"name": d + ":\\", "path": d + ":\\", "dir": True} for d in string.ascii_uppercase
            if os.path.exists(d + ":\\")]}
    ex, out = _exts(mode), []
    for n in sorted(os.listdir(path), key=str.lower):
        if n.startswith("."):
            continue
        p = os.path.join(path, n)
        try:
            if os.path.isdir(p):
                out.append({"name": n, "path": p, "dir": True})
            elif n.lower().endswith(ex):
                plat = CF.detect(p)
                out.append({"name": n, "path": p, "dir": False, "size": os.path.getsize(p),
                            "plat": " ".join(x for x in plat if x).strip()})
        except OSError:
            pass
    out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else ("" if os.name == "nt" else path),
            "entries": out[:600]}


def run_jobs(req):
    try:
        _run_jobs(req)
    except Exception as ex:   # noqa  - worker tidak boleh membuat UI menggantung
        _log(f"Internal error: {ex}", "e")
    finally:
        with LOCK:
            ST["running"] = False


def _run_jobs(req):
    mode, target, files = req.get("mode", "convert"), req.get("target"), req.get("files", [])
    o = req.get("opts", {})
    try:
        keys = wii.load_keys(o.get("key") or None, None, None, [os.getcwd(), app_dir()])
    except Exception as ex:   # noqa
        _log(f"Invalid Wii key: {ex}", "e")
        keys = {}
    opts = {"level": int(o.get("level", 9)), "wbfs_mode": o.get("wbfs_mode"), "split": bool(o.get("split")),
            "iso_mode": o.get("iso_mode", "auto"), "verify": bool(o.get("verify")), "wii_keys": keys,
            "log": lambda m: _log(m, "w")}
    n, out, force = len(files), (req.get("out") or "").strip() or None, bool(req.get("force"))
    for k, path in enumerate(files):
        base = os.path.basename(path)
        if CANCEL.is_set():
            ST["jobs"][path] = {"state": "error", "frac": 0, "msg": "Cancelled"}
            continue

        def prog(frac, msg="", k=k, path=path):
            if CANCEL.is_set():
                raise Cancelled()
            with LOCK:
                ST["jobs"][path] = {"state": "running", "frac": frac, "msg": f"{int(frac * 100)}%"}
                ST["overall"] = (k + frac) / n
        _log(f"[{k + 1}/{n}] {base}")
        try:
            if mode == "convert":
                res = CF.convert(path, target, out, force, dict(opts), prog, CANCEL)
                msg = f"✓ {CF.human(os.path.getsize(path))} → {CF.human(os.path.getsize(res))}"
                _log("  → " + res)
            elif mode == "extract":
                files_out = CF.extract(path, out, force, False, prog, CANCEL)
                msg = "✓ " + ", ".join(os.path.basename(x) for x in files_out)
                _log("  → " + ", ".join(files_out))
            elif mode == "verify":
                ok, m = CF.verify(path, None, prog, CANCEL)
                msg = ("✓ " if ok else "✗ ") + m
                if not ok:
                    raise CF.ForgeError(m)
            else:
                _log(CF.info(path))
                msg = "✓ info"
            ST["jobs"][path] = {"state": "done", "frac": 1, "msg": msg}
        except Cancelled:
            ST["jobs"][path] = {"state": "error", "frac": 0, "msg": "Cancelled"}
        except Exception as ex:   # noqa
            ST["jobs"][path] = {"state": "error", "frac": 0, "msg": str(ex)[:90]}
            _log(f"ERROR {base}: {ex}", "e")
        with LOCK:
            ST["overall"] = (k + 1) / n


def make_handler(token, port):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method):
            u = urlparse(self.path)
            if self.headers.get("Host", "") not in hosts:
                return self._send(403, {"error": "bad host"})
            if u.path == "/" and method == "GET":
                html = open(os.path.join(HERE, "ui.html"), encoding="utf-8").read().replace("__TOKEN__", token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            q = parse_qs(u.query)
            if not u.path.startswith("/api/") or q.get("t", [""])[0] != token:
                return self._send(403, {"error": "forbidden"})
            body = {}
            if method == "POST":
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
            try:
                if u.path == "/api/ls":
                    return self._send(200, list_dir(q.get("path", [""])[0], q.get("mode", ["convert"])[0]))
                if u.path == "/api/state":
                    with LOCK:
                        return self._send(200, dict(ST, log=list(ST["log"]), jobs=dict(ST["jobs"])))
                if u.path == "/api/ping":
                    BEAT.update(last=time.time(), seen=True)
                    return self._send(200, {"version": __version__, "running": ST["running"]})
                if u.path == "/api/quit":
                    QUIT.set()
                    return self._send(200, {})
                if u.path == "/api/settings":
                    p = os.path.join(user_data_dir(), "settings.json")
                    if method == "POST":
                        _strip_secrets(body)                       # key TIDAK pernah disimpan ke disk
                        json.dump(body, open(p, "w", encoding="utf-8"))
                        return self._send(200, {})
                    try:
                        return self._send(200, json.load(open(p, encoding="utf-8")))
                    except (OSError, ValueError):
                        return self._send(200, {})
                if u.path == "/api/cancel":
                    CANCEL.set()
                    return self._send(200, {})
                if u.path == "/api/start":
                    with LOCK:
                        if ST["running"]:
                            return self._send(409, {"error": "already running"})
                        ST.update(running=True, jobs={}, log=[], overall=0.0)
                    CANCEL.clear()
                    threading.Thread(target=run_jobs, args=(body,), daemon=True).start()
                    return self._send(200, {})
            except OSError as ex:
                return self._send(400, {"error": str(ex)})
            self._send(404, {"error": "not found"})

        do_GET = lambda self: self._route("GET")
        do_POST = lambda self: self._route("POST")
    return H


def serve(port=0, open_browser=True, idle_timeout=180, native=True):
    """Jalankan UI. Berhenti saat: tombol Quit, jendela native ditutup, atau tab browser hilang
    (tidak ada ping selama idle_timeout detik dan tidak ada job berjalan)."""
    token = secrets.token_urlsafe(16)
    srv = ThreadingHTTPServer(("127.0.0.1", port), lambda *a: None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(token, port)
    url = f"http://127.0.0.1:{port}/"
    QUIT.clear()
    BEAT.update(last=time.time(), seen=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"ConsoleForge UI: {url}")

    def watchdog():
        while not QUIT.wait(1.0):
            if BEAT["seen"] and not ST["running"] and time.time() - BEAT["last"] > idle_timeout:
                QUIT.set()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        win = None
        if native and open_browser:
            try:
                import webview                                    # opsional: jendela native (pywebview)
                win = webview.create_window(f"ConsoleForge {__version__}", url, width=1200, height=780,
                                            min_size=(900, 560))
                win.events.closed += QUIT.set
                webview.start()                                   # blok sampai jendela ditutup
                QUIT.set()
            except ImportError:
                win = None
        if win is None:
            if open_browser:
                threading.Timer(0.4, lambda: webbrowser.open(url)).start()
            QUIT.wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
    return 0


def main():
    try:
        return serve(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    except Exception:   # noqa  - catat crash supaya bisa dilaporkan (exe tanpa konsol)
        p = os.path.join(user_data_dir(), "crash.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + __version__ + "\n" + traceback.format_exc() + "\n")
        raise


if __name__ == "__main__":
    sys.exit(main())
