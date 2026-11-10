"""Run with:  python -m unittest test_n_drop -v
Tests the HTTP logic with a fake clipboard, so it works on any OS (no Windows needed)."""
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import n_drop

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeClipboard:
    def __init__(self):
        self.text = self.image = self.files = None
        self.to_read = None

    def set_text(self, text):
        self.text = text

    def set_image(self, img):
        self.image = img

    def set_files(self, paths):
        self.files = list(paths)

    def read(self):
        return self.to_read


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clip = FakeClipboard()
        cfg = {"token": TOKEN, "save_dir": self.tmp.name, "max_mb": 1, "port": 0}
        self.app = n_drop.create_app(cfg, self.clip)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # --- auth
    def test_ping_needs_no_token(self):
        self.assertTrue(self.client.get("/ping").get_json()["ok"])

    def test_text_rejected_without_token(self):
        r = self.client.post("/text", data={"text": "hi"})
        self.assertEqual(r.status_code, 401)
        self.assertIsNone(self.clip.text)

    def test_wrong_token_rejected(self):
        r = self.client.post("/text", data={"text": "hi"}, headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)

    def test_rate_limit_after_ten_failures(self):
        for _ in range(10):
            self.client.post("/text", data={"text": "x"})
        r = self.client.post("/text", data={"text": "x"}, headers=AUTH)
        self.assertEqual(r.status_code, 429)

    # --- text
    def test_text_with_unicode(self):
        r = self.client.post("/text", data={"text": "नमस्ते 😀\nline2"}, headers=AUTH)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.clip.text, "नमस्ते 😀\nline2")

    def test_empty_text_is_error(self):
        r = self.client.post("/text", data={"text": ""}, headers=AUTH)
        self.assertEqual(r.status_code, 400)

    def test_legacy_beam_with_query_key(self):
        r = self.client.post(f"/beam?key={TOKEN}", data={"text": "old shortcut"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.clip.text, "old shortcut")

    def test_legacy_beam_old_key_rejected(self):
        self.assertEqual(self.client.post("/beam?key=1234", data={"text": "x"}).status_code, 401)

    def test_query_key_not_accepted_on_new_routes(self):
        self.assertEqual(self.client.post(f"/text?key={TOKEN}", data={"text": "x"}).status_code, 401)

    # --- files
    def test_pdf_saved_and_put_on_clipboard(self):
        data = {"file": (io.BytesIO(b"%PDF-1.4 fake"), "report.pdf")}
        r = self.client.post("/file", data=data, headers=AUTH, content_type="multipart/form-data")
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["kind"], "file")
        saved = Path(self.tmp.name) / body["name"]
        self.assertEqual(saved.read_bytes(), b"%PDF-1.4 fake")
        self.assertEqual(self.clip.files, [str(saved)])

    def test_image_goes_to_clipboard_as_image(self):
        buf = io.BytesIO()
        Image.new("RGB", (4, 3), "red").save(buf, "PNG")
        buf.seek(0)
        r = self.client.post("/file", data={"file": (buf, "photo.png")}, headers=AUTH,
                             content_type="multipart/form-data")
        self.assertEqual(r.get_json()["kind"], "image")
        self.assertEqual(self.clip.image.size, (4, 3))

    def test_fake_image_extension_falls_back_to_file(self):
        r = self.client.post("/file", data={"file": (io.BytesIO(b"not an image"), "x.png")}, headers=AUTH,
                             content_type="multipart/form-data")
        self.assertEqual(r.get_json()["kind"], "file")

    def test_path_traversal_name_is_contained(self):
        r = self.client.post("/file", data={"file": (io.BytesIO(b"data"), "../../evil.txt")}, headers=AUTH,
                             content_type="multipart/form-data")
        saved = Path(self.tmp.name) / r.get_json()["name"]
        self.assertTrue(saved.exists())
        self.assertEqual(saved.parent.resolve(), Path(self.tmp.name).resolve())

    def test_raw_body_upload_with_filename_header(self):
        headers = dict(AUTH, **{"X-Filename": "my%20notes.txt"})
        r = self.client.post("/file", data=b"hello", headers=headers, content_type="application/octet-stream")
        self.assertIn("my notes.txt", r.get_json()["name"])

    def test_same_name_twice_does_not_overwrite(self):
        names = set()
        for _ in range(2):
            r = self.client.post("/file", data={"file": (io.BytesIO(b"x"), "a.txt")}, headers=AUTH,
                                 content_type="multipart/form-data")
            names.add(r.get_json()["name"])
        self.assertEqual(len(names), 2)

    def test_too_large_rejected(self):
        big = io.BytesIO(b"0" * (2 * 1024 * 1024))
        r = self.client.post("/file", data={"file": (big, "big.bin")}, headers=AUTH,
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 413)

    def test_safe_name_rules(self):
        self.assertEqual(n_drop.safe_name("CON.txt"), "_CON.txt")
        self.assertEqual(n_drop.safe_name("a<b>c.pdf"), "a_b_c.pdf")
        self.assertEqual(n_drop.safe_name(""), "file")
        self.assertEqual(n_drop.safe_name("C:\\Users\\x\\doc.pdf"), "doc.pdf")

    # --- /drop: one endpoint for text and files
    def drop(self, text=None, file=None, auth=True):
        data = {}
        if text is not None:
            data["text"] = text
        if file is not None:
            data["file"] = file
        return self.client.post("/drop", data=data, headers=AUTH if auth else {},
                                content_type="multipart/form-data")

    def test_drop_requires_token(self):
        self.assertEqual(self.drop(text="x", auth=False).status_code, 401)

    def test_drop_text_only(self):
        body = self.drop(text="just text").get_json()
        self.assertEqual((body["ok"], body["kind"]), (True, "text"))
        self.assertEqual(self.clip.text, "just text")

    def test_drop_text_converted_to_file_is_treated_as_text(self):
        # Shortcuts turns the same text into a file too: both fields carry the same content
        body = self.drop(text="hello नमस्ते", file=(io.BytesIO("hello नमस्ते".encode()), "Text.txt")).get_json()
        self.assertEqual(body["kind"], "text")
        self.assertEqual(self.clip.text, "hello नमस्ते")
        self.assertIsNone(self.clip.files)
        self.assertEqual([p for p in Path(self.tmp.name).iterdir() if p.is_file()], [])

    def test_drop_converted_text_with_crlf_bom_and_trailing_newline(self):
        converted = b"\xef\xbb\xbfline1\r\nline2\r\n"
        body = self.drop(text="line1\nline2", file=(io.BytesIO(converted), "Text.txt")).get_json()
        self.assertEqual(body["kind"], "text")

    def test_drop_empty_file_part_with_text(self):
        body = self.drop(text="abc", file=(io.BytesIO(b""), "Text.txt")).get_json()
        self.assertEqual(body["kind"], "text")

    def test_drop_image_ignores_name_in_text_field(self):
        buf = io.BytesIO()
        Image.new("RGB", (6, 2), "green").save(buf, "PNG")
        buf.seek(0)
        body = self.drop(text="IMG_1234.PNG", file=(buf, "IMG_1234.PNG")).get_json()
        self.assertEqual(body["kind"], "image")
        self.assertEqual(self.clip.image.size, (6, 2))
        self.assertIsNone(self.clip.text)

    def test_drop_pdf(self):
        body = self.drop(text="report.pdf", file=(io.BytesIO(b"%PDF-1.4 x"), "report.pdf")).get_json()
        self.assertEqual(body["kind"], "file")
        self.assertIsNone(self.clip.text)

    def test_drop_real_txt_file_with_different_text_field(self):
        body = self.drop(text="notes.txt", file=(io.BytesIO(b"my actual notes"), "notes.txt")).get_json()
        self.assertEqual(body["kind"], "file")
        self.assertIsNone(self.clip.text)

    def test_drop_nothing(self):
        self.assertEqual(self.drop().status_code, 400)

    def test_drop_file_only(self):
        body = self.drop(file=(io.BytesIO(b"abc"), "a.bin")).get_json()
        self.assertEqual(body["kind"], "file")

    # --- pull
    def test_pull_empty(self):
        self.assertEqual(self.client.get("/pull", headers=AUTH).get_json()["type"], "empty")

    def test_pull_text(self):
        self.clip.to_read = ("text", "from the PC")
        body = self.client.get("/pull", headers=AUTH).get_json()
        self.assertEqual((body["type"], body["text"]), ("text", "from the PC"))

    def test_pull_requires_token(self):
        self.assertEqual(self.client.get("/pull").status_code, 401)

    def test_pull_single_file_roundtrip(self):
        src = Path(self.tmp.name) / "src.pdf"
        src.write_bytes(b"pdf-bytes")
        self.clip.to_read = ("files", [str(src)])
        info = self.client.get("/pull", headers=AUTH).get_json()
        self.assertEqual((info["type"], info["name"]), ("file", "src.pdf"))
        got = self.client.get(f"/pull/file/{info['id']}", headers=AUTH)
        self.assertEqual(got.data, b"pdf-bytes")

    def test_pull_multiple_files_zipped(self):
        paths = []
        for n in ("a.txt", "b.txt"):
            p = Path(self.tmp.name) / n
            p.write_text(n)
            paths.append(str(p))
        self.clip.to_read = ("files", paths)
        info = self.client.get("/pull", headers=AUTH).get_json()
        self.assertTrue(info["name"].endswith(".zip"))
        got = self.client.get(f"/pull/file/{info['id']}", headers=AUTH)
        self.assertTrue(got.data.startswith(b"PK"))

    def test_pull_image(self):
        self.clip.to_read = ("image", Image.new("RGB", (5, 5), "blue"))
        info = self.client.get("/pull", headers=AUTH).get_json()
        self.assertEqual(info["type"], "image")
        got = self.client.get(f"/pull/file/{info['id']}", headers=AUTH)
        self.assertTrue(got.data.startswith(b"\x89PNG"))

    def test_pull_unknown_id(self):
        self.assertEqual(self.client.get("/pull/file/doesnotexist", headers=AUTH).status_code, 404)


if __name__ == "__main__":
    unittest.main()
