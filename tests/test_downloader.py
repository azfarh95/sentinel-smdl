"""Regression tests for optional downloader authentication cookies."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from app.downloader import _resolve_cookies


class ResolveCookiesTests(TestCase):
    def test_returns_none_when_optional_cookie_file_cannot_be_accessed(self):
        """Public downloads must proceed without an optional unreadable cookie file."""
        with patch.object(Path, "exists", side_effect=PermissionError("access denied")):
            self.assertIsNone(_resolve_cookies("https://www.instagram.com/reel/example/"))

    def test_returns_none_when_cookie_file_cannot_be_opened_read_write(self):
        """yt-dlp needs a read-write cookie jar, not merely a readable file."""
        with TemporaryDirectory() as cookie_dir:
            cookie_path = Path(cookie_dir) / "instagram.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            with patch("app.downloader.COOKIES_DIR", cookie_dir):
                with patch.object(Path, "open", side_effect=PermissionError("access denied")):
                    self.assertIsNone(_resolve_cookies("https://www.instagram.com/reel/example/"))

    def test_returns_path_when_optional_cookie_file_is_readable(self):
        with TemporaryDirectory() as cookie_dir:
            cookie_path = Path(cookie_dir) / "instagram.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            with patch("app.downloader.COOKIES_DIR", cookie_dir):
                self.assertEqual(
                    _resolve_cookies("https://www.instagram.com/reel/example/"),
                    str(cookie_path),
                )
