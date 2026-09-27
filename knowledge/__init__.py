"""知识网络：把「读一篇论文」变成「知识库有机生长」的自动化 pipeline。

分两块：

1) 单篇阅读 pipeline（把一篇已入库论文吃进知识库）
   llm_steps.py   结构化 LLM 调用（方向归类 / 关系抽取 / 阶段综述），带 JSON 自修复重试
   store.py       存储层：方向树 / 归属 / 产物 / 关系边 / 综述 / 缓冲区 / 检索 run + 可视化序列化
   graphdoc.py    节点图谱 Markdown（每个节点一份，含 Mermaid 图 + 论文表 + 关系表 + 综述）
   pipeline.py    编排：五步（summary→tree→deep→graph→synthesis），幂等可重跑、逐步可观测

2) 检索 pipeline（把 arXiv 新论文筛进缓冲区）
   arxiv_search.py  批量检索（日期 + 分类 + 关键词，只取 title/abstract，不下载全文）
   discovery.py     编排：search→screen→summarize→buffer + 缓冲区提升入库（promote_buffer）

对外（其他层只应通过这里调用）：
  STEPS / STEP_TITLES / iter_pipeline_events / run_pipeline        单篇 pipeline
  SEARCH_STAGES / iter_search_events / run_search_pipeline        检索 pipeline
  start_search_background                                           异步后台执行
  promote_buffer                                                    缓冲区 -> 正式库
  store / graphdoc                                                  存储与图谱文档

依赖方向：knowledge -> app（ORM/设置/arxiv 服务）+ harness（LLM/技能），反向无引用。
"""

from . import arxiv_search, graphdoc, store  # noqa: F401
from .discovery import (  # noqa: F401
    STAGE_TITLES as SEARCH_STAGE_TITLES,
    STAGES as SEARCH_STAGES,
    iter_search_events,
    promote_buffer,
    run_search_pipeline,
    start_search_background,
)
from .pipeline import (  # noqa: F401
    STEP_TITLES,
    STEPS,
    PipelineResult,
    iter_pipeline_events,
    run_pipeline,
)

__all__ = [
    "STEP_TITLES",
    "STEPS",
    "SEARCH_STAGE_TITLES",
    "SEARCH_STAGES",
    "PipelineResult",
    "arxiv_search",
    "graphdoc",
    "iter_pipeline_events",
    "iter_search_events",
    "promote_buffer",
    "run_pipeline",
    "run_search_pipeline",
    "start_search_background",
    "store",
]
