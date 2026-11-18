import os
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from backend.downloader import build_format_selector, expand_subtitle_langs
from backend.dubbing import NEURAL_VOICES_BY_GENDER, parse_subtitle_cues
from backend.security import _waf_check_text, detect_platform, validate_url
from backend.universal import is_public_http_url
from backend.updater import parse_version


class CoreTests(unittest.TestCase):
    def test_ssrf_public_url_validation(self):
        self.assertTrue(is_public_http_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertTrue(is_public_http_url("https://vimeo.com/76979871"))
        self.assertFalse(is_public_http_url("http://127.0.0.1:8080/admin"))
        self.assertFalse(is_public_http_url("http://localhost/secret"))
        self.assertFalse(is_public_http_url("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(is_public_http_url("http://192.168.1.1/"))
        self.assertFalse(is_public_http_url("https://user:pass@youtube.com/watch?v=1"))

        with self.assertRaises(HTTPException):
            validate_url("http://127.0.0.1/test")

    def test_platform_detection(self):
        self.assertEqual(detect_platform("https://youtu.be/abc123"), "YouTube")
        self.assertEqual(detect_platform("https://www.instagram.com/reel/xyz/"), "Instagram")
        self.assertEqual(detect_platform("https://www.vimeo.com/123"), "vimeo.com")

    def test_waf_signatures(self):
        self.assertIsNotNone(_waf_check_text("<script>alert(1)</script>"))
        self.assertIsNotNone(_waf_check_text("../../etc/passwd"))
        self.assertIsNotNone(_waf_check_text("a UNION SELECT * FROM users"))
        self.assertIsNone(_waf_check_text("https://www.youtube.com/watch?v=TjWmVXJEKSM"))

    def test_subtitle_and_format_helpers(self):
        self.assertEqual(expand_subtitle_langs("tr"), ["tr"])
        self.assertEqual(expand_subtitle_langs("en"), ["en"])
        self.assertEqual(expand_subtitle_langs("zh"), ["zh-Hans"])
        self.assertEqual(expand_subtitle_langs("bad;rm"), [])
        self.assertEqual(build_format_selector("audio")["postprocessors"][0]["preferredcodec"], "mp3")
        self.assertEqual(build_format_selector("720")["merge_output_format"], "mp4")

    def test_dubbing_cue_merging_and_voices(self):
        for code in ("tr", "en", "de", "ru", "zh"):
            self.assertIn(code, NEURAL_VOICES_BY_GENDER)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.srt"
            p.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
                "2\n00:00:02,100 --> 00:00:03,500\nworld.\n",
                encoding="utf-8",
            )
            cues = parse_subtitle_cues(p)
            self.assertEqual(len(cues), 1)
            self.assertEqual(cues[0][2], "Hello world.")

    def test_version_parsing(self):
        self.assertGreater(parse_version("1.4.3"), parse_version("1.4.2"))

    def test_sqlite_persistence(self):
        import time
        from backend import storage

        storage.save_job("job_test_1", {"job_id": "job_test_1", "status": "completed", "percent": 100.0})
        loaded = storage.load_job("job_test_1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["status"], "completed")

        storage.save_ban("198.51.100.99", time.time() + 120)
        self.assertGreater(storage.get_ban_until("198.51.100.99"), time.time())
        storage.save_ban("198.51.100.99", time.time() - 5)
        self.assertEqual(storage.get_ban_until("198.51.100.99"), 0.0)


if __name__ == "__main__":
    unittest.main()
