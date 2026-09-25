"""arxiv_service 单元测试：链接规范化与版本剥离（纯函数，无网络）。"""

import pytest

from app import arxiv_service as ax


@pytest.mark.parametrize("raw,expected", [
    ("2401.12345", "2401.12345"),
    ("2401.12345v2", "2401.12345v2"),
    ("http://arxiv.org/abs/2401.12345", "2401.12345"),
    ("https://arxiv.org/abs/2401.12345v1", "2401.12345v1"),
    ("https://arxiv.org/pdf/2401.12345", "2401.12345"),
    ("https://arxiv.org/pdf/2401.12345v3.pdf", "2401.12345v3"),
    ("arxiv.org/abs/2401.12345", "2401.12345"),
    ("https://arxiv.org/abs/2401.12345?context=cs", "2401.12345"),
    ("  2401.12345  ", "2401.12345"),
    ("math/0211159", "math/0211159"),
    ("http://arxiv.org/abs/math/0211159", "math/0211159"),
    ("cs.CL/0112017", "cs.CL/0112017"),
])
def test_normalize(raw, expected):
    assert ax.normalize_arxiv_id(raw) == expected


def test_normalize_invalid():
    with pytest.raises(ValueError):
        ax.normalize_arxiv_id("not-an-arxiv-link")
    with pytest.raises(ValueError):
        ax.normalize_arxiv_id("")
    with pytest.raises(ValueError):
        ax.normalize_arxiv_id("   ")


@pytest.mark.parametrize("aid,expected", [
    ("2401.12345v2", "2401.12345"),
    ("2401.12345", "2401.12345"),
    ("math/0211159v1", "math/0211159"),
])
def test_strip_version(aid, expected):
    assert ax.strip_version(aid) == expected
