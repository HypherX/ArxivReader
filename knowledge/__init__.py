"""知识网络：把「读一篇论文」变成「知识库有机生长」的自动化 pipeline。

分三块：

1) 单篇阅读 pipeline（把一篇已入库论文吃进知识库）
   llm_steps.py   结构化 LLM 调用（方向归类 / 关系抽取 / 阶段综述），带 JSON 自修复重试
   store.py       存储层：方向树 / 归属 / 产物 / 关系边 / 综述 / 缓冲区 / 检索 run + 可视化序列化
   graphdoc.py    节点图谱 Markdown（每个节点一份，含 Mermaid 图 + 论文表 + 关系表 + 综述）
   pipeline.py    编排：四步（summary→tree→deep→graph），幂等可重跑、逐步可观测
   review.py      文件夹手动综述：全子树快照、分批证据、覆盖校验、取消与续跑

2) 智能关联检索（把当前论文的 Related Work / 参考文献变成候选论文）
   related.py       编排：collect→extract→search→judge + 候选加入文献库
                    （只下载 PDF 放进「全部论文」，不自动跑阅读步骤）

对外（其他层只应通过这里调用）：
  STEPS / STEP_TITLES / iter_pipeline_events / run_pipeline        单篇 pipeline
  RELATED_STAGES / iter_related_events / run_related_pipeline     智能关联检索 pipeline
  start_related_background / start_related_promote_background
  promote_related_candidate                                       提升入库
  store / graphdoc                                                存储与图谱文档

依赖方向：knowledge 使用 app 的 ORM/设置/arxiv 服务及 harness 的 LLM/技能；
app 路由调用 knowledge 编排，harness 不依赖 ORM。
"""

from . import arxiv_search, graphdoc, related, store  # noqa: F401
from .pipeline import (  # noqa: F401
    STEP_TITLES,
    STEPS,
    PipelineResult,
    iter_pipeline_events,
    run_pipeline,
)
from .related import (  # noqa: F401
    STAGE_TITLES as RELATED_STAGE_TITLES,
    STAGES as RELATED_STAGES,
    iter_related_events,
    promote_related_candidate,
    run_related_pipeline,
    start_related_background,
    start_related_promote_background,
)

__all__ = [
    "STEP_TITLES",
    "STEPS",
    "RELATED_STAGE_TITLES",
    "RELATED_STAGES",
    "PipelineResult",
    "arxiv_search",
    "graphdoc",
    "iter_pipeline_events",
    "iter_related_events",
    "iter_related_events",
    "promote_related_candidate",
    "related",
    "run_pipeline",
    "run_related_pipeline",
    "start_related_background",
    "start_related_promote_background",
    "store",
]
