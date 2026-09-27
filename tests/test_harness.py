"""Agent Harness 单元测试：文档结构、预算渲染、工具、工具循环、技能/对话装配。

全部离线：不触网、不依赖真实 LLM（LLM 调用一律 monkeypatch）。
"""

import types

import pytest

from harness import agent, chat, llm, runner, skills, tokens, tools
from harness.document import PaperDocument
from harness.paper import PaperInput

_PAPER_TEXT = """Preprint. Under review.
[Page 1]
Attention Is All You Need
Alice Smith, Bob Lee
Abstract
The dominant sequence transduction models are based on complex recurrent networks.
We propose the Transformer, based solely on attention mechanisms.
1 Introduction
Recurrent models preclude parallelization within training examples.
arXiv:1706.03762v1  [cs.CL]  12 Jun 2017
[Page 2]
Preprint. Under review.
2 Related Work
Prior work used convolutional neural networks and recurrent models.
3 Model
3.1 Encoder and Decoder Stacks
We stack 6 identical layers with d_model=512.
Attention is defined as softmax(Q K^T / sqrt(d_k)) V.
[Page 3]
Preprint. Under review.
4 Experiments
4.1 Machine Translation
WMT 2014 EN-DE: BLEU 28.4, 2.0 BLEU above the previous best.
4.2 Ablation
Single-head attention drops 0.9 BLEU on EN-DE.
5 Conclusion
The Transformer relies entirely on attention.
References
[1] Vaswani et al. Attention is all you need. NeurIPS 2017.
"""


def _document() -> PaperDocument:
    return PaperDocument(_PAPER_TEXT)


def _paper() -> PaperInput:
    return PaperInput(arxiv_id="1706.03762", title="Attention Is All You Need",
                      authors=["Alice Smith", "Bob Lee"], abstract="We propose the Transformer.",
                      categories=["cs.CL"], published="2017-06-12T00:00:00", full_text=_PAPER_TEXT)


# ---------------------------------------------------------------- tokens
def test_estimate_tokens_by_char_class():
    assert tokens.estimate_tokens("") == 0
    assert tokens.estimate_tokens("a" * 400) == 100      # ASCII: 4 字符/token
    assert tokens.estimate_tokens("中文") == 2             # CJK: 1 字符/token


def test_estimate_messages_counts_tool_calls():
    plain = tokens.estimate_messages([{"role": "user", "content": "hi"}])
    with_tc = tokens.estimate_messages([{
        "role": "assistant", "content": "",
        "tool_calls": [{"function": {"name": "read_section", "arguments": '{"section":"method"}'}}],
    }])
    assert with_tc > plain


# ---------------------------------------------------------------- document
def test_noise_lines_removed_and_text_kept():
    doc = _document()
    assert doc.dropped_lines                                   # 页眉/arXiv 水印被记录
    joined = "\n".join(b.text for b in doc.blocks)
    assert "Preprint" not in joined
    assert "1706.03762v1" not in joined
    assert "6 identical layers" in joined


def test_sections_detected_and_nested_heading_stays_in_section():
    doc = _document()
    keys = {item["key"] for item in doc.outline()}
    assert {"abstract", "introduction", "related_work", "method",
            "experiments", "conclusion", "references"} <= keys
    method = doc.match_section("method")
    assert method is not None and "6 identical layers" in method.text
    assert "d_model=512" in doc.section_text("method")
    # 无规范名的小标题不另开章节，只作为结构标记留在所属章节内
    assert "other" not in keys


def test_render_drops_references_and_keeps_page_anchors():
    rendered = _document().render(tokens_budget=10000)
    assert "Vaswani" not in rendered.text                       # 参考文献默认丢弃
    assert "28.4" in rendered.text
    assert "[p.3]" in rendered.text
    assert rendered.tokens > 0 and not rendered.truncated


def test_render_truncates_when_budget_too_small():
    rendered = _document().render(tokens_budget=30)
    assert rendered.truncated
    assert rendered.note                                        # 明确告知模型省略了什么
    assert rendered.tokens <= 40


def test_render_compact_sections_keep_head_and_tail():
    rendered = _document().render(tokens_budget=5000,
                                  sections=("abstract", "conclusion"), per_section_paras=2)
    assert "The Transformer relies entirely on attention." in rendered.text
    assert "Introduction" not in rendered.text.split("## ")[0]


def test_search_returns_page_anchored_snippets():
    out = _document().search("BLEU", k=3)
    assert "28.4" in out and "[p.3]" in out
    assert "未在正文中检索到" in _document().search("zzz-not-a-term")


def test_section_text_unknown_lists_available():
    out = _document().section_text("methodology-of-nothing")
    assert "未找到章节" in out and "method" in out


def test_read_page_and_stats():
    doc = _document()
    assert "Related Work" in doc.read_page(2)
    stats = doc.stats()
    assert stats["raw_chars"] > stats["clean_chars"] > 0
    assert stats["compression"] > 0


# ---------------------------------------------------------------- tools
def _tool_list(names):
    paper = _paper()
    ctx = tools.ToolContext(paper=paper, document=paper.document())
    return tools.build_tools(ctx, names)


def test_tool_registry_and_schema():
    assert {"get_outline", "read_section", "search_text", "read_page"} <= set(tools.tool_names())
    built = _tool_list(list(tools.tool_names()))
    assert len(built) == 4
    schema = built[0].schema()
    assert schema["type"] == "function" and schema["function"]["name"]


def test_tools_are_hidden_without_body():
    ctx = tools.ToolContext(paper=PaperInput(), document=PaperDocument(""))
    assert tools.build_tools(ctx, ["read_section"]) == []


def test_tool_run_handles_bad_arguments():
    tool = _tool_list(["read_section"])[0]
    bad_json, ok = tool.run("{not-json")
    assert not ok and "JSON" in bad_json
    # 参数名不对时不报错，而是把“缺少必要参数 + 可用取值”回给模型，让它自己纠正
    wrong_arg, _ = tool.run('{"unknown": 1}')
    assert "缺少 section" in wrong_arg and "abstract" in wrong_arg
    good, ok3 = tool.run('{"section": "conclusion"}')
    assert ok3 and "entirely on attention" in good


# ---------------------------------------------------------------- agent（工具循环）
def _stream_script(script):
    """把"每轮事件列表"变成可 monkeypatch 的 fake llm.stream_events。

    script: List[List[dict]]，第 n 次调用产出 script[n] 里的事件；同时记录每次调用的
    messages 与 tools 参数，便于断言回灌协议与"轮数用尽后不再下发工具"。
    """
    calls = []

    def fake_stream(settings, messages, tools=None, tool_choice=None, include_usage=None,
                    should_stop=None):
        index = len(calls)
        calls.append({"messages": list(messages), "tools": tools})
        for event in script[min(index, len(script) - 1)]:
            yield event

    fake_stream.calls = calls
    return fake_stream


def test_run_agent_executes_tool_then_answers(monkeypatch):
    fake = _stream_script([
        [{"type": "tool_call", "index": 0, "id": "c1", "name": "read_section",
          "arguments": '{"section": "method"}'}],
        [{"type": "delta", "content": "答", "reasoning_content": "想"},
         {"type": "delta", "content": "案", "reasoning_content": "好了"},
         {"type": "usage", "usage": {"prompt_tokens": 1, "total_tokens": 11}}],
    ])
    monkeypatch.setattr(llm, "stream_events", fake)
    run = agent.run_agent(llm.LLMSettings(model="m"), [{"role": "user", "content": "q"}],
                          _tool_list(["read_section"]), max_rounds=2)
    assert run.text == "答案" and run.rounds == 2
    assert run.reasoning_content == "想好了"              # 同一轮内的推理增量合并保留
    assert run.calls and run.calls[0].name == "read_section" and run.calls[0].ok
    assert run.usage["total_tokens"] == 11            # usage 来自流式最终 chunk
    # 第二轮必须能拿到工具结果（回灌协议正确）
    assert "6 identical layers" in fake.calls[1]["messages"][-1]["content"]
    assert fake.calls[1]["messages"][-1]["role"] == "tool"


def test_run_agent_forces_answer_after_max_rounds(monkeypatch):
    fake = _stream_script([[{"type": "tool_call", "index": 0, "id": "c", "name": "get_outline",
                             "arguments": "{}"}]])
    monkeypatch.setattr(llm, "stream_events", fake)
    run = agent.run_agent(llm.LLMSettings(model="m"), [{"role": "user", "content": "q"}],
                          _tool_list(["get_outline"]), max_rounds=1)
    assert fake.calls[0]["tools"]                 # 第一轮带工具
    assert fake.calls[1]["tools"] is None         # 轮数用尽后不再下发工具
    assert run.rounds == 2


def test_run_agent_reports_stream_error(monkeypatch):
    def broken(settings, messages, tools=None, tool_choice=None, include_usage=None,
               should_stop=None):
        raise RuntimeError("boom")
        yield  # pragma: no cover  让它成为生成器

    monkeypatch.setattr(llm, "stream_events", broken)
    run = agent.run_agent(llm.LLMSettings(model="m"), [{"role": "user", "content": "q"}])
    assert run.error and run.text == ""


def test_iter_agent_events_carries_content_and_reasoning(monkeypatch):
    fake = _stream_script([
        [{"type": "delta", "content": "", "reasoning_content": "先想"},
         {"type": "tool_call", "index": 0, "id": "c1", "name": "search_text",
          "arguments": '{"query": "BLEU"}'}],
        [{"type": "delta", "content": "28.4", "reasoning_content": ""},
         {"type": "delta", "content": " BLEU", "reasoning_content": ""},
         {"type": "usage", "usage": {"total_tokens": 7}}],
    ])
    monkeypatch.setattr(llm, "stream_events", fake)
    events = list(agent.iter_agent_events(llm.LLMSettings(model="m"),
                                         [{"role": "user", "content": "q"}],
                                         _tool_list(["search_text"]), max_rounds=2))
    kinds = [e["type"] for e in events]
    assert kinds == ["delta", "tool", "delta", "delta", "final"]
    # 每个 delta 事件同时携带 content 与 reasoning_content 两个字段
    assert events[0]["content"] == "" and events[0]["reasoning_content"] == "先想"
    assert events[1]["name"] == "search_text" and events[1]["ok"]
    final = events[-1]
    assert final["full_content"] == "28.4 BLEU"
    assert final["full_reasoning_content"] == "先想"
    assert final["usage"]["total_tokens"] == 7


def test_llm_parses_reasoning_field_aliases():
    class _Msg:
        def __init__(self, **kw):
            self.model_extra = kw

    assert llm._parse_reasoning(_Msg(reasoning_content="a")) == "a"
    assert llm._parse_reasoning(_Msg(reasoning="b")) == "b"
    assert llm._parse_reasoning(_Msg()) == ""


def test_stream_params_include_usage_option():
    settings = llm.LLMSettings(model="m", reasoning_effort="max")
    params = llm.build_params(settings, [{"role": "user", "content": "q"}],
                              stream=True, include_usage=True)
    assert params["stream"] is True
    assert params["stream_options"] == {"include_usage": True}
    assert params["extra_body"] == {"reasoning_effort": "max"}
    assert "stream_options" not in llm.build_params(settings, [], stream=True)


# ---------------------------------------------------------------- skills / runner
def test_skills_registered_and_prompts_render():
    names = skills.skill_names()
    assert "deep_reading" in names and "quick_summary" in names
    for spec in skills.list_skills():
        text = skills.render_prompt_file(spec.prompt, output_language="中文")
        assert "{{" not in text, spec.name
        assert text.strip()


def test_deep_reading_context_is_richer_than_quick_summary():
    paper = _paper()
    deep_msgs, deep_info = runner.build_skill_messages(skills.get_skill("deep_reading"), paper)
    quick_msgs, quick_info = runner.build_skill_messages(skills.get_skill("quick_summary"), paper)
    assert "论文元信息" in deep_msgs[1]["content"]
    assert "[p." in deep_msgs[1]["content"]
    assert "entirely on attention" in quick_msgs[1]["content"]      # 速读包含结论
    assert "Related Work" not in quick_msgs[1]["content"]           # 但不读相关工作
    assert quick_info["context_tokens_est"] < deep_info["context_tokens_est"]
    assert quick_info["context_budget_tokens"] == 3000


def test_quick_summary_has_no_tools_and_deep_reading_has_them():
    assert skills.get_skill("quick_summary").tools == ()
    assert "read_section" in skills.get_skill("deep_reading").tools


def test_run_skill_records_usage_and_tool_calls(monkeypatch):
    fake = _stream_script([[
        {"type": "delta", "content": "精读结果", "reasoning_content": "先看摘要"},
        {"type": "usage", "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                    "total_tokens": 120}},
    ]])
    monkeypatch.setattr(llm, "stream_events", fake)
    result = runner.run_skill("deep_reading", _paper(), llm.LLMSettings(model="m"))
    assert result.skill == "deep_reading" and result.output == "精读结果" and not result.error
    assert result.reasoning_content == "先看摘要"
    stats = result.stats
    assert stats["usage"]["total_tokens"] == 120
    assert stats["prompt_tokens_est"] > 0 and stats["output_tokens_est"] > 0
    assert stats["reasoning_chars"] == len("先看摘要")
    assert stats["doc"]["clean_chars"] > 0 and "latency_ms" in stats


def test_iter_skill_events_passes_tool_and_final(monkeypatch):
    fake = _stream_script([
        [{"type": "delta", "content": "", "reasoning_content": "先查正文"},
         {"type": "tool_call", "index": 0, "id": "c1", "name": "search_text",
          "arguments": '{"query": "BLEU"}'}],
        [{"type": "delta", "content": "结论", "reasoning_content": ""},
         {"type": "usage", "usage": {"total_tokens": 9}}],
    ])
    monkeypatch.setattr(llm, "stream_events", fake)
    events = list(runner.iter_skill_events("deep_reading", _paper(), llm.LLMSettings(model="m")))
    kinds = [e["type"] for e in events]
    assert kinds == ["delta", "tool", "delta", "final"]
    assert events[0]["reasoning_content"] == "先查正文"
    assert events[1]["name"] == "search_text" and events[1]["ok"]
    assert events[-1]["output"] == "结论" and events[-1]["skill"] == "deep_reading"
    assert events[-1]["reasoning_content"] == "先查正文"
    assert events[-1]["stats"]["usage"]["total_tokens"] == 9


def test_run_skill_with_empty_body_still_answers(monkeypatch):
    fake = _stream_script([[{"type": "delta", "content": "无正文", "reasoning_content": ""}]])
    monkeypatch.setattr(llm, "stream_events", fake)
    result = runner.run_skill("quick_summary", PaperInput(title="空论文"),
                              llm.LLMSettings(model="m"))
    assert result.output == "无正文"
    assert result.stats["tools"] == []


def test_unknown_skill_raises():
    with pytest.raises(KeyError):
        skills.get_skill("no_such_skill")


# ---------------------------------------------------------------- chat（对话）
def test_build_chat_messages_trims_history_and_puts_question_last():
    history = [{"role": "user", "content": "q{}".format(i)} for i in range(10)]
    cfg = {"chat_history_messages": 4, "chat_context_tokens": 2000, "output_language": "中文"}
    messages, info = chat.build_chat_messages(_paper(), history, "新问题", cfg=cfg)
    assert messages[0]["role"] == "system" and "论文材料" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "新问题"}
    assert info["history_messages"] == 4 and info["history_dropped"] == 6


def test_chat_context_zero_switches_to_outline_mode():
    cfg = {"chat_context_tokens": 0, "chat_history_messages": 4, "output_language": "中文"}
    messages, _ = chat.build_chat_messages(_paper(), [], "q", cfg=cfg)
    system = messages[0]["content"]
    assert "章节大纲" in system and "正文未预置" in system
    assert "6 identical layers" not in system      # 大纲模式下不预置正文


def test_chat_tools_respect_switch_and_body():
    paper = _paper()
    assert len(chat.build_chat_tools(paper, {"chat_tools": True})) == 4
    assert chat.build_chat_tools(paper, {"chat_tools": False}) == []
    assert chat.build_chat_tools(PaperInput(), {"chat_tools": True}) == []


def test_reasoning_effort_only_sent_when_set():
    from harness import config as harness_config
    base = llm.LLMSettings(model="m")
    assert "extra_body" not in llm.build_params(base, [])
    with_effort = llm.LLMSettings(model="m", reasoning_effort="low")
    params = llm.build_params(with_effort, [])
    assert params["extra_body"] == {"reasoning_effort": "low"}
    # 采样参数未显式设置时不下发，交给服务端默认值
    assert "temperature" not in params and "max_tokens" not in params
    # 配置节的形状稳定（值可以为空：发布版默认留空，由用户在网页「设置」里填）
    assert {"base_url", "api_key", "model"} <= set(harness_config.get_llm_config())


def test_shipped_config_is_not_fake_configured():
    """模板里的 LLM 三件套必须留空。

    回归：模板曾写着 api.openai.com + gpt-4o-mini + 占位 key，
    首启时会被当成“已配置”（不显示提醒），用户点任何 AI 功能都撞 401。
    """
    import importlib.util
    import os

    from harness import config as harness_config

    path = os.path.join(harness_config.ROOT_DIR, "config.example.py")
    spec = importlib.util.spec_from_file_location("example_cfg_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in ("base_url", "api_key", "model"):
        assert not str(module.LLM.get(key) or "").strip(), key
    # 空配置必须判定为“未配置”（前端据此显示「⚠ 设置」并明确提示）
    assert llm.is_configured(llm.LLMSettings(base_url="", api_key="", model="")) is False
