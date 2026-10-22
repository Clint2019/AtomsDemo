"""Download names remain useful and cannot add HTTP response headers."""
import unittest
from urllib.parse import unquote

from atoms_demo.api.server import attachment_disposition


class AttachmentDispositionTests(unittest.TestCase):
    def test_unicode_name_has_utf8_value_and_extension_fallback(self):
        header = attachment_disposition("报告.txt")
        self.assertIn('filename="download.txt"', header)
        self.assertEqual(unquote(header.split("filename*=UTF-8''", 1)[1]), "报告.txt")

    def test_hostile_name_cannot_inject_header_or_path(self):
        header = attachment_disposition('a/../b\\c\r\nX-Injected: yes.txt')
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertNotIn("/", unquote(header.split("filename*=UTF-8''", 1)[1]))
        self.assertNotIn("\\", unquote(header.split("filename*=UTF-8''", 1)[1]))
        self.assertNotIn(":", unquote(header.split("filename*=UTF-8''", 1)[1]))

    def test_empty_and_long_names_are_bounded(self):
        self.assertIn('filename="download"', attachment_disposition(""))
        self.assertLess(len(attachment_disposition("a" * 1000)), 500)


if __name__ == "__main__":
    unittest.main()
