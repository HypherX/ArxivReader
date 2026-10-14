"""PDF 全文翻译设置的持久化与服务定义。

沿用 app.settings_store 的语义：
  - 字段缺省 / None = 不修改；
  - 空串 = 清除该设置（api_key 除外：空 = 不修改，防止误清空）；
  - openaicompatible / openai 服务的 model、base_url、api_key 留空时
    回退到主 LLM 设置（与应用内其它 AI 能力共用一套端点）。

所有键以 ``pdftr.`` 前缀存进 Setting 表，与主 LLM 设置互不干扰。
"""

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ..models import Setting

# ---------------------------------------------------------------- 服务目录
# 每个服务对应 pdf2zh_next 的一个翻译引擎；detail 字段名与
# pdf2zh_next config.toml 的 <service>_detail 段一一对应（见 config.toml.example）。
SERVICES: Dict[str, Dict[str, Any]] = {
    "openaicompatible": {
        "label": "OpenAI 兼容（默认跟随主模型设置）",
        "engine_type": "OpenAICompatible",
        "model_field": "openai_compatible_model",
        "base_url_field": "openai_compatible_base_url",
        "api_key_field": "openai_compatible_api_key",
        "default_model": "gpt-4o-mini",
        "needs_key": True, "needs_url": True, "fallback_main": True,
    },
    "openai": {
        "label": "OpenAI",
        "engine_type": "OpenAI",
        "model_field": "openai_model",
        "base_url_field": "openai_base_url",
        "api_key_field": "openai_api_key",
        "default_model": "gpt-4o-mini",
        "needs_key": True, "needs_url": False, "fallback_main": True,
    },
    "deepseek": {
        "label": "DeepSeek",
        "engine_type": "DeepSeek",
        "model_field": "deepseek_model",
        "api_key_field": "deepseek_api_key",
        "default_model": "deepseek-chat",
        "needs_key": True, "needs_url": False,
    },
    "siliconflow": {
        "label": "SiliconFlow（硅基流动）",
        "engine_type": "SiliconFlow",
        "model_field": "siliconflow_model",
        "base_url_field": "siliconflow_base_url",
        "api_key_field": "siliconflow_api_key",
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
        "default_base_url": "https://api.siliconflow.cn/v1",
        "needs_key": True, "needs_url": False,
    },
    "siliconflowfree": {
        "label": "SiliconFlow Free（免费）",
        "engine_type": "SiliconFlowFree",
        "needs_key": False, "needs_url": False,
    },
    "zhipu": {
        "label": "智谱 GLM",
        "engine_type": "Zhipu",
        "model_field": "zhipu_model",
        "api_key_field": "zhipu_api_key",
        "default_model": "glm-4-flash",
        "needs_key": True, "needs_url": False,
    },
    "gemini": {
        "label": "Google Gemini",
        "engine_type": "Gemini",
        "model_field": "gemini_model",
        "api_key_field": "gemini_api_key",
        "default_model": "gemini-1.5-flash",
        "needs_key": True, "needs_url": False,
    },
    "ollama": {
        "label": "Ollama（本地）",
        "engine_type": "Ollama",
        "model_field": "ollama_model",
        "base_url_field": "ollama_host",
        "default_model": "gemma2",
        "default_base_url": "http://localhost:11434",
        "needs_key": False, "needs_url": True,
    },
    "bing": {
        "label": "Bing 翻译（免费，无需 Key）",
        "engine_type": "Bing",
        "needs_key": False, "needs_url": False,
    },
    "google": {
        "label": "Google 翻译（免费，无需 Key）",
        "engine_type": "Google",
        "needs_key": False, "needs_url": False,
    },
}

DEFAULT_SERVICE = "openaicompatible"

# pdf2zh_next 支持的语言代码（lang_in / lang_out 下拉共用）
LANGUAGES = [
    ("zh-CN", "简体中文"), ("zh-TW", "繁體中文"), ("en", "English"),
    ("ja", "日本語"), ("ko", "한국어"), ("fr", "Français"),
    ("de", "Deutsch"), ("ru", "Русский"), ("es", "Español"),
]

OUTPUT_MODES = ("dual", "mono", "both")   # dual=对照版, mono=纯译文, both=两份都出
DUAL_MODES = ("LR", "TB")                 # LR=同页左右对照, TB=原文/译文交替页

_KEYS = (
    "service", "model", "base_url", "api_key",
    "lang_in", "lang_out", "qps", "output_mode", "dual_mode",
    "dual_translate_first", "translate_table_text", "ocr_workaround",
    "enhance_compatibility", "use_mirror",
)
_PREFIX = "pdftr."
_BOOL_KEYS = ("dual_translate_first", "translate_table_text", "ocr_workaround",
              "enhance_compatibility", "use_mirror")


def _key(name: str) -> str:
    return _PREFIX + name


def _read_all(db: Session) -> Dict[str, str]:
    rows = db.query(Setting).filter(Setting.key.like(_PREFIX + "%")).all()
    return {row.key[len(_PREFIX):]: row.value for row in rows}


def get_translation_settings(db: Session) -> Dict[str, Any]:
    """读取全文翻译设置（api_key 原样返回，仅限后端内部使用）。"""
    raw = _read_all(db)
    service = raw.get("service") or DEFAULT_SERVICE
    if service not in SERVICES:
        service = DEFAULT_SERVICE
    try:
        qps = int(raw.get("qps") or 4)
    except (TypeError, ValueError):
        qps = 4
    qps = max(1, min(100, qps))
    output_mode = raw.get("output_mode") or "dual"
    if output_mode not in OUTPUT_MODES:
        output_mode = "dual"
    dual_mode = (raw.get("dual_mode") or "LR").upper()
    if dual_mode not in DUAL_MODES:
        dual_mode = "LR"
    lang_in = raw.get("lang_in") or "en"
    lang_out = raw.get("lang_out") or ("zh-CN" if _app_language(db) == "zh" else "en")
    return {
        "service": service,
        "model": (raw.get("model") or "").strip(),
        "base_url": (raw.get("base_url") or "").strip(),
        "api_key": raw.get("api_key") or "",
        "lang_in": lang_in,
        "lang_out": lang_out,
        "qps": qps,
        "output_mode": output_mode,
        "dual_mode": dual_mode,
        **{name: (raw.get(name) or "").strip().lower() in ("1", "true", "yes", "on")
           for name in _BOOL_KEYS},
    }


def _app_language(db: Session) -> str:
    from ..settings_store import get_output_language
    return get_output_language(db)


def update_translation_settings(db: Session, patch: Dict[str, Any]) -> None:
    """写入翻译设置；None 跳过，api_key 空串跳过（= 不修改），其余空串 = 清除。"""
    for key, value in patch.items():
        if key not in _KEYS or value is None:
            continue
        if key == "service" and str(value) not in SERVICES:
            continue
        if key in _BOOL_KEYS:
            text = "1" if value is True or str(value).strip().lower() in ("1", "true", "yes", "on") else ""
        else:
            text = str(value).strip()
        if key == "api_key" and not text:
            continue
        if key in ("output_mode",) and text and text not in OUTPUT_MODES:
            continue
        if key in ("dual_mode",) and text and text.upper() not in DUAL_MODES:
            continue
        row = db.get(Setting, _key(key))
        if row is None:
            db.add(Setting(key=_key(key), value=text))
        else:
            row.value = text
    db.commit()


def public_view(settings: Dict[str, Any]) -> Dict[str, Any]:
    """给前端的视图：不回传完整 api_key，只给是否已设置与脱敏预览。"""
    from ..settings_store import mask_api_key
    view = {k: v for k, v in settings.items() if k != "api_key"}
    view["has_api_key"] = bool(settings.get("api_key"))
    view["api_key_preview"] = mask_api_key(settings.get("api_key") or "")
    view["services"] = [
        {"id": sid, "label": svc["label"], "needs_key": svc.get("needs_key", False),
         "needs_url": svc.get("needs_url", False),
         "default_model": svc.get("default_model", ""),
         "default_base_url": svc.get("default_base_url", "")}
        for sid, svc in SERVICES.items()
    ]
    view["languages"] = [{"code": code, "label": label} for code, label in LANGUAGES]
    return view


def resolve_engine_credentials(db: Session, settings: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """把翻译设置解析成写入 config.toml 的引擎凭据。

    openaicompatible / openai 的留空字段回退到主 LLM 设置；
    免费服务（bing/google/siliconflowfree）不需要凭据。
    """
    settings = settings or get_translation_settings(db)
    service = SERVICES[settings["service"]]
    model = settings.get("model") or ""
    base_url = settings.get("base_url") or ""
    api_key = settings.get("api_key") or ""
    if service.get("fallback_main"):
        from ..settings_store import get_llm_settings
        main = get_llm_settings(db)
        model = model or main.model
        base_url = base_url or main.base_url
        api_key = api_key or main.api_key
    if not model:
        model = service.get("default_model", "")
    if not base_url:
        base_url = service.get("default_base_url", "")
    return {"model": model, "base_url": base_url, "api_key": api_key}
