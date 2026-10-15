import unittest

from harness.paper import PaperInput, readable_text


class ReadableTextTests(unittest.TestCase):
    def test_strips_pdf_control_characters_but_keeps_newlines(self):
        cleaned = readable_text("log\x00p\x01(y)\nnext")
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("\x01", cleaned)
        self.assertIn("\n", cleaned)
        self.assertTrue(cleaned.startswith("log p"))

    def test_paper_input_cleans_stored_text(self):
        paper = PaperInput.from_any({"full_text": "page\x00tail", "title": "T"})
        self.assertNotIn("\x00", paper.full_text)
        self.assertIn("tail", paper.full_text)


if __name__ == "__main__":
    unittest.main()
