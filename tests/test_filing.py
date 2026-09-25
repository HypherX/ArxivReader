"""filing 规则引擎单元测试：四类匹配、优先级、停用与兜底。"""

from app import filing


def _target(**kw):
    base = dict(title="", abstract="", authors=[], categories=[])
    base.update(kw)
    return filing.FilingTarget(**base)


def test_category_exact():
    rules = [filing.RuleSpec(match_type="category", pattern="cs.CL", folder_id=1)]
    assert filing.apply_rules(_target(categories=["cs.CL"]), rules) == 1


def test_category_prefix():
    rules = [filing.RuleSpec(match_type="category", pattern="cs", folder_id=2)]
    assert filing.apply_rules(_target(categories=["cs.LG", "math.OC"]), rules) == 2


def test_category_no_false_prefix():
    # "cs" 不应匹配 "cst"（无此分类，但确保前缀以 . 或全等界定）
    rules = [filing.RuleSpec(match_type="category", pattern="cs", folder_id=2)]
    assert filing.apply_rules(_target(categories=["cstd"]), rules) is None


def test_keyword_in_abstract():
    rules = [filing.RuleSpec(match_type="keyword", pattern="diffusion", folder_id=3)]
    assert filing.apply_rules(_target(abstract="A diffusion model study"), rules) == 3


def test_author_case_insensitive():
    rules = [filing.RuleSpec(match_type="author", pattern="bengio", folder_id=4)]
    assert filing.apply_rules(_target(authors=["Yoshua Bengio", "Other"]), rules) == 4


def test_title_match():
    rules = [filing.RuleSpec(match_type="title", pattern="attention", folder_id=5)]
    assert filing.apply_rules(_target(title="Attention Is All You Need"), rules) == 5


def test_priority_first_match():
    rules = [
        filing.RuleSpec(match_type="keyword", pattern="model", folder_id=10, priority=50),
        filing.RuleSpec(match_type="category", pattern="cs.CL", folder_id=11, priority=10),
    ]
    assert filing.apply_rules(_target(title="a model", categories=["cs.CL"]), rules) == 11


def test_disabled_skipped():
    rules = [filing.RuleSpec(match_type="keyword", pattern="x", folder_id=1, enabled=False)]
    assert filing.apply_rules(_target(title="x"), rules) is None


def test_no_match_returns_none():
    rules = [filing.RuleSpec(match_type="category", pattern="cs.CL", folder_id=1)]
    assert filing.apply_rules(_target(categories=["math.NA"]), rules) is None


def test_empty_pattern_no_match():
    rules = [filing.RuleSpec(match_type="keyword", pattern="   ", folder_id=1)]
    assert filing.apply_rules(_target(title="anything"), rules) is None
