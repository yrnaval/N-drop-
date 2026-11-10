"""N-Drop v2 - iPhone <-> Windows clipboard and file bridge.

Run:   python n_drop.py
Token: created automatically in config.json on first run (python n_drop.py --show-token to print it).

Endpoints (all except /ping need the header  Authorization: Bearer <token>):
  GET  /ping              health check
  POST /text              form field "text"                -> PC clipboard
  POST /beam              legacy alias of /text (also accepts ?key=<token>)
  POST /file              multipart field "file" (or raw body + X-Filename header)
                          -> saved to save_dir, and put on the PC clipboard
  POST /drop              ONE endpoint for everything: send form fields "text" AND "file"
                          (both = Clipboard); the server works out which one is real
  GET  /pull              what is on the PC clipboard right now (JSON)
  GET  /pull/file/<id>    download the file/image announced by /pull
"""
import argparse
import hmac
import ipaddress
import json
import logging
import mimetypes
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from collections import defaultdict
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import unquote

from flask import Flask, jsonify, request, send_file

VERSION = "2.0"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DEFAULTS = {
    "port": 8010,
    "save_dir": str(Path.home() / "Downloads" / "N-Drop"),
    "max_mb": 500,
    "notifications": True,
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
try:  # optional: pip install pillow-heif  -> iPhone HEIC photos paste as images
    import pillow_heif
    pillow_heif.register_heif_opener()
    IMAGE_EXT = IMAGE_EXT | {".heic", ".heif"}
except Exception:
    pass
PULL_TTL = 15 * 60  # seconds a pulled file stays downloadable

log = logging.getLogger("n_drop")


# ----------------------------------------------------------------- config
def load_config(path=CONFIG_PATH):
    """Return (config, first_run). Creates config.json with a random token if missing."""
    cfg = dict(DEFAULTS)
    first_run = False
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(32)
        first_run = True
    if first_run or not path.exists():
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg, first_run


# ---------------------------------------------------------------- helpers
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_name(name, fallback="file"):
    """Strip any path, forbidden characters and reserved Windows names from a filename."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if not name:
        name = fallback
    p = Path(name)
    stem, ext = p.stem[:80] or fallback, p.suffix[:10]
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    return stem + ext


def unique_path(folder, name):
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    candidate = folder / f"{stamp}_{name}"
    n = 1
    while candidate.exists():
        candidate = folder / f"{stamp}_{n}_{name}"
        n += 1
    return candidate


# -------------------------------------------------------------------- app
def create_app(cfg, clip, notify=lambda title, msg: None):
    """clip must provide set_text(str), set_image(PIL.Image), set_files([paths]), read()."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = int(cfg["max_mb"]) * 1024 * 1024

    token = cfg["token"]
    save_dir = Path(cfg["save_dir"]).expanduser()
    save_dir.mkdir(parents=True, exist_ok=True)
    outbox = save_dir / "_outbox"
    outbox.mkdir(exist_ok=True)

    fails = defaultdict(list)  # ip -> timestamps of recent failed logins
    pulls = {}                 # id -> {"path", "name", "expires"}
    pulls_lock = threading.Lock()

    # ---- auth
    def check_auth(allow_query_key):
        ip = request.remote_addr or "?"
        now = time.time()
        fails[ip] = [t for t in fails[ip] if now - t < 60]
        if len(fails[ip]) >= 10:
            return jsonify(ok=False, error="too many attempts, wait a minute"), 429
        supplied = ""
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()
        if not supplied:
            supplied = request.headers.get("X-Token", "")
        if not supplied and allow_query_key:
            supplied = request.args.get("key", "")
        if hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
            return None
        fails[ip].append(now)
        log.warning("rejected request from %s (bad token)", ip)
        return jsonify(ok=False, error="unauthorized"), 401

    def guarded(allow_query_key=False):
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                denied = check_auth(allow_query_key)
                if denied is not None:
                    return denied
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    @app.errorhandler(413)
    def too_big(_err):
        return jsonify(ok=False, error=f"too large (limit {cfg['max_mb']} MB)"), 413

    # ---- health
    @app.get("/ping")
    def ping():
        return jsonify(ok=True, app="N-Drop", version=VERSION)

    # ---- phone -> PC: text
    def store_text(text):
        try:
            clip.set_text(text)
        except Exception as exc:  # clipboard locked by another app, etc.
            log.exception("could not set clipboard text")
            return jsonify(ok=False, error=f"clipboard error: {exc}"), 500
        log.info("text received (%d chars)", len(text))
        notify("N-Drop", f"Text received ({len(text)} characters)")
        return jsonify(ok=True, kind="text", chars=len(text))

    def receive_text():
        text = request.form.get("text")
        if text is None and request.is_json:
            text = (request.get_json(silent=True) or {}).get("text")
        if text is None and request.mimetype == "text/plain":
            text = request.get_data(as_text=True)
        if not text:
            return jsonify(ok=False, error="no text received"), 400
        return store_text(text)

    @app.post("/text")
    @guarded()
    def text_route():
        return receive_text()

    @app.post("/beam")
    @guarded(allow_query_key=True)
    def beam_route():  # legacy endpoint so the old shortcut keeps working
        return receive_text()

    # ---- phone -> PC: files and images
    def finish_file(dest):
        size = dest.stat().st_size
        if size == 0:
            dest.unlink()
            return jsonify(ok=False, error="empty file"), 400

        kind = "file"
        try:
            if dest.suffix.lower() in IMAGE_EXT:
                try:
                    from PIL import Image
                    with Image.open(dest) as im:
                        im.load()
                        clip.set_image(im.copy())
                    kind = "image"
                except Exception:
                    log.info("not a readable image, treating as a file: %s", dest.name)
            if kind == "file":
                clip.set_files([str(dest)])
        except Exception as exc:
            log.exception("could not place file on clipboard")
            return jsonify(ok=True, kind=kind, name=dest.name, size=size,
                           warning=f"saved, but clipboard failed: {exc}")
        log.info("%s received: %s (%d bytes)", kind, dest.name, size)
        notify("N-Drop", f"{kind.capitalize()} received: {dest.name}")
        return jsonify(ok=True, kind=kind, name=dest.name, size=size)

    def store_incoming(incoming):
        original = incoming.filename or ""
        if not original:
            original = "upload" + (mimetypes.guess_extension(incoming.mimetype or "") or ".bin")
        dest = unique_path(save_dir, safe_name(original))
        incoming.save(dest)
        return finish_file(dest)

    @app.post("/file")
    @guarded()
    def file_route():
        incoming = next(iter(request.files.values()), None)
        if incoming is not None:
            return store_incoming(incoming)
        original = unquote(request.headers.get("X-Filename", ""))
        if not original:
            original = "upload" + (mimetypes.guess_extension(request.mimetype or "") or ".bin")
        body = request.get_data()
        if not body:
            return jsonify(ok=False, error="nothing received"), 400
        dest = unique_path(save_dir, safe_name(original))
        dest.write_bytes(body)
        return finish_file(dest)

    # ---- phone -> PC: one endpoint for text AND files
    def is_converted_text(incoming, text):
        """True when the uploaded 'file' is just the text field turned into a file by Shortcuts."""
        stream = incoming.stream
        try:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(0)
        except Exception:
            return False
        if size == 0:
            return True
        if size > 4 * 1024 * 1024:
            return False
        data = stream.read()
        stream.seek(0)
        try:
            decoded = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False

        def norm(s):
            return s.replace("\r\n", "\n").strip()

        return norm(decoded) == norm(text)

    @app.post("/drop")
    @guarded()
    def drop_route():
        text = request.form.get("text")
        incoming = next(iter(request.files.values()), None)
        if incoming is not None and not (text and is_converted_text(incoming, text)):
            return store_incoming(incoming)  # a real file or image
        if text:
            return store_text(text)
        return jsonify(ok=False, error="nothing received"), 400

    # ---- PC -> phone: pull
    def purge_pulls():
        now = time.time()
        with pulls_lock:
            for fid in [k for k, v in pulls.items() if v["expires"] < now]:
                entry = pulls.pop(fid)
                p = Path(entry["path"])
                if p.parent == outbox:
                    p.unlink(missing_ok=True)
        for old in outbox.iterdir():  # leftovers from earlier runs
            if old.is_file() and now - old.stat().st_mtime > 3600:
                old.unlink(missing_ok=True)

    def register(path, name):
        fid = uuid.uuid4().hex
        with pulls_lock:
            pulls[fid] = {"path": str(path), "name": name, "expires": time.time() + PULL_TTL}
        return fid

    @app.get("/pull")
    @guarded()
    def pull_route():
        purge_pulls()
        try:
            item = clip.read()
        except Exception as exc:
            log.exception("could not read clipboard")
            return jsonify(ok=False, error=f"clipboard error: {exc}"), 500
        if not item:
            return jsonify(ok=True, type="empty")

        kind, value = item
        if kind == "text":
            log.info("pull: text (%d chars)", len(value))
            return jsonify(ok=True, type="text", text=value)

        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        if kind == "image":
            path = outbox / f"{uuid.uuid4().hex}.png"
            value.save(path, "PNG")
            name = f"clipboard_{stamp}.png"
            result_type = "image"
        else:  # files
            paths = [Path(p) for p in value if Path(p).exists()]
            if not paths:
                return jsonify(ok=True, type="empty")
            if len(paths) == 1 and paths[0].is_file():
                path, name = paths[0], paths[0].name
            else:
                path = outbox / f"{uuid.uuid4().hex}.zip"
                with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in paths:
                        if p.is_dir():
                            for sub in p.rglob("*"):
                                if sub.is_file():
                                    zf.write(sub, Path(p.name) / sub.relative_to(p))
                        else:
                            zf.write(p, p.name)
                name = f"N-Drop_{stamp}.zip"
            result_type = "file"

        if path.stat().st_size > app.config["MAX_CONTENT_LENGTH"]:
            return jsonify(ok=False, error=f"too large to pull (limit {cfg['max_mb']} MB)"), 413
        log.info("pull: %s (%s)", result_type, name)
        return jsonify(ok=True, type=result_type, id=register(path, name), name=name)

    @app.get("/pull/file/<fid>")
    @guarded()
    def pull_file_route(fid):
        with pulls_lock:
            entry = pulls.get(fid)
        if not entry or entry["expires"] < time.time() or not Path(entry["path"]).exists():
            return jsonify(ok=False, error="expired or unknown id, pull again"), 404
        return send_file(entry["path"], as_attachment=True, download_name=entry["name"])

    return app


# ------------------------------------------------------------ run helpers
def make_notifier(enabled):
    if not enabled:
        return lambda title, msg: None
    try:
        from winotify import Notification
    except Exception:
        log.info("winotify not installed - no desktop notifications")
        return lambda title, msg: None

    def notify(title, msg):
        def show():
            try:
                Notification(app_id="N-Drop", title=title, msg=msg, duration="short").show()
            except Exception:
                log.debug("notification failed", exc_info=True)
        threading.Thread(target=show, daemon=True).start()

    return notify


def tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=4)
        line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
        if line:
            return line
    except Exception:
        pass
    try:  # fallback: look for an address in Tailscale's 100.64.0.0/10 range
        net = ipaddress.ip_network("100.64.0.0/10")
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if ipaddress.ip_address(info[4][0]) in net:
                return info[4][0]
    except Exception:
        pass
    return None


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger("n_drop")
    root.setLevel(logging.INFO)
    for handler in (RotatingFileHandler(BASE_DIR / "n_drop.log", maxBytes=1_000_000, backupCount=3,
                                        encoding="utf-8"),
                    logging.StreamHandler()):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def main():
    parser = argparse.ArgumentParser(description="N-Drop v2 server")
    parser.add_argument("--show-token", action="store_true", help="print the token and exit")
    args = parser.parse_args()

    cfg, first_run = load_config()
    if args.show_token:
        print(cfg["token"])
        return
    setup_logging()

    import win_clipboard  # Windows only
    from waitress import serve

    app = create_app(cfg, win_clipboard, make_notifier(cfg.get("notifications", True)))
    ip = tailscale_ip()
    print(f"\nN-Drop v{VERSION} running on port {cfg['port']}")
    print(f"  Tailscale address : http://{ip}:{cfg['port']}" if ip else
          "  Tailscale address : not found - is Tailscale running?")
    print(f"  Files saved to    : {cfg['save_dir']}")
    if first_run:
        print(f"\n  Your token (also saved in config.json - keep it secret):\n  {cfg['token']}\n")
    else:
        print("  Token             : in config.json (python n_drop.py --show-token)")
    print("  Press Ctrl+C to stop.\n")
    serve(app, host="0.0.0.0", port=int(cfg["port"]), threads=4,
          max_request_body_size=int(cfg["max_mb"]) * 1024 * 1024 + 1_048_576)


if __name__ == "__main__":
    main()
