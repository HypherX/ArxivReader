"""PDF 查找的命中区间，以及输入法回车不会被当成提交。"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PdfFindTests(unittest.TestCase):
    def test_match_ranges_and_ime_enter(self):
        subprocess.check_call(["node", "tests/pdf_find_ranges.test.js"], cwd=ROOT)
