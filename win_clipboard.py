"""Windows clipboard helper for N-Drop (needs pywin32 and Pillow).

The server must run in your normal logged-in Windows session (a console window or a
"run at logon" task), not as a background service, or it cannot touch your clipboard.
"""
import io
import subprocess
import threading
import time

import win32clipboard as wcb
import win32con
from PIL import ImageGrab

_lock = threading.RLock()
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def _open(retries=20, delay=0.05):
    """Another app can briefly hold the clipboard, so retry for about a second."""
    for _ in range(retries):
        try:
            wcb.OpenClipboard()
            return
        except Exception:
            time.sleep(delay)
    raise RuntimeError("clipboard is busy, try again")


def set_text(text):
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with _lock:
        _open()
        try:
            wcb.EmptyClipboard()
            wcb.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            wcb.CloseClipboard()


def set_image(img):
    """Put a PIL image on the clipboard (as DIB for old apps, plus PNG to keep transparency)."""
    bmp = io.BytesIO()
    img.convert("RGB").save(bmp, "BMP")
    dib = bmp.getvalue()[14:]  # drop the 14-byte BMP file header
    png = io.BytesIO()
    img.save(png, "PNG")
    png_format = wcb.RegisterClipboardFormat("PNG")
    with _lock:
        _open()
        try:
            wcb.EmptyClipboard()
            wcb.SetClipboardData(win32con.CF_DIB, dib)
            wcb.SetClipboardData(png_format, png.getvalue())
        finally:
            wcb.CloseClipboard()


def set_files(paths):
    """Put real files on the clipboard so Ctrl+V in Explorer pastes them."""
    quoted = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Set-Clipboard -LiteralPath {quoted}"],
        check=True, timeout=20, creationflags=_NO_WINDOW,
    )


def read():
    """Return ("files", [paths]) | ("text", str) | ("image", PIL.Image) | None."""
    with _lock:
        _open()
        try:
            if wcb.IsClipboardFormatAvailable(win32con.CF_HDROP):
                return ("files", list(wcb.GetClipboardData(win32con.CF_HDROP)))
            if wcb.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                text = wcb.GetClipboardData(win32con.CF_UNICODETEXT)
                if text:
                    return ("text", text)
        finally:
            wcb.CloseClipboard()
        grabbed = ImageGrab.grabclipboard()
        if grabbed is None:
            return None
        if isinstance(grabbed, list):
            return ("files", [str(p) for p in grabbed])
        return ("image", grabbed)
