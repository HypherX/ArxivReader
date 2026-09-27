"""ORM 数据模型。

业务表：folders / papers / filing_rules / chat_sessions / chat_messages / settings
authors、categories 以 JSON 文本存储，通过属性辅助方法读写。

知识网络（论文阅读 -> 方向树 / 局部图谱 / 阶段综述）：
  direction_nodes   方向树节点（物化路径 path，便于层序遍历与可视化）
  paper_directions  论文 ↔ 节点 多归属关系（一篇论文可属于多个子问题）
  paper_artifacts   论文产物（Step1 摘要 / Step3 深读笔记），pipeline 各步的落盘处
  paper_graph_edges 局部论文关系边（同一直方向节点内的引用/改进/对立…）
  node_syntheses    阶段性综述历史（每个节点多版）
  pipeline_runs     pipeline 编排账本（步骤状态/错误/耗时，支持断点与重跑）

所有图结构均可由上述表导出为「节点 + 边」JSON（D3/ECharts 可直接用）。
"""

import datetime
import json
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


class Folder(Base):
    """自定义分类文件夹，支持通过 parent_id 嵌套成树。"""

    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("folders.id"), nullable=True, index=True
    )
    # 指向方向树节点：不为空表示该文件夹是「由模型/流程维护」的方向文件夹，
    # 左侧文件夹树因此就是方向树的可视化体现（计数=该节点直接挂载的论文数）。
    direction_node_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("direction_nodes.id"), nullable=True, unique=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    # 自引用邻接表：parent 为多对一，remote_side 指向主键
    parent: Mapped[Optional["Folder"]] = relationship(
        "Folder", back_populates="children", remote_side=[id]
    )
    children: Mapped[List["Folder"]] = relationship(
        "Folder", back_populates="parent", order_by="Folder.name"
    )
    papers: Mapped[List["Paper"]] = relationship("Paper", back_populates="folder")


class Paper(Base):
    """一篇已入库的论文（当前来源为 ArXiv）。"""

    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arxiv_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    authors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    abstract: Mapped[str] = mapped_column(Text, nullable=False, default="")
    categories_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    published: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pdf_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    folder_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("folders.id"), nullable=True, index=True
    )
    # 抽取的论文全文缓存，供对话阅读直塞上下文；text_truncated 标记是否被截断
    full_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    added_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    folder: Mapped[Optional["Folder"]] = relationship("Folder", back_populates="papers")
    sessions: Mapped[List["ChatSession"]] = relationship(
        "ChatSession", back_populates="paper", cascade="all, delete-orphan"
    )
    # 知识网络关联：删论文时一并清理产物/归属/关系边/pipeline 账本
    artifacts: Mapped[List["PaperArtifact"]] = relationship(
        "PaperArtifact", back_populates="paper", cascade="all, delete-orphan"
    )
    directions: Mapped[List["PaperDirection"]] = relationship(
        "PaperDirection", back_populates="paper", cascade="all, delete-orphan"
    )
    edges_out: Mapped[List["PaperRelation"]] = relationship(
        "PaperRelation", foreign_keys="PaperRelation.src_paper_id",
        cascade="all, delete-orphan",
    )
    edges_in: Mapped[List["PaperRelation"]] = relationship(
        "PaperRelation", foreign_keys="PaperRelation.dst_paper_id",
        cascade="all, delete-orphan",
    )
    pipeline_runs: Mapped[List["PipelineRun"]] = relationship(
        "PipelineRun", back_populates="paper", cascade="all, delete-orphan"
    )

    @property
    def authors(self) -> List[str]:
        try:
            return json.loads(self.authors_json or "[]")
        except (ValueError, TypeError):
            return []

    @property
    def categories(self) -> List[str]:
        try:
            return json.loads(self.categories_json or "[]")
        except (ValueError, TypeError):
            return []

    @property
    def has_text(self) -> bool:
        return bool(self.full_text)

    @property
    def text_chars(self) -> int:
        """已缓存全文的字符数，供前端展示“入上下文”规模。"""
        return len(self.full_text or "")

    @property
    def folder_name(self) -> Optional[str]:
        return self.folder.name if self.folder else None

    @property
    def folder_is_direction(self) -> bool:
        """所在文件夹是否为方向树镜像（🧭）：前端据此区分“自建分类”与“模型长出的方向”。"""
        return bool(self.folder and self.folder.direction_node_id)


class FilingRule(Base):
    """自动归档规则：命中后把论文放入 folder_id。priority 越小越先匹配。"""

    __tablename__ = "filing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # category | keyword | author | title
    match_type: Mapped[str] = mapped_column(String(32), nullable=False)
    pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_id: Mapped[Optional[int]] = mapped_column(ForeignKey("folders.id"), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    folder: Mapped[Optional["Folder"]] = relationship("Folder")

    @property
    def folder_name(self) -> Optional[str]:
        return self.folder.name if self.folder else None


class ChatSession(Base):
    """针对某篇论文的一次对话会话。"""

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新对话")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    paper: Mapped["Paper"] = relationship("Paper", back_populates="sessions")
    messages: Mapped[List["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
    )


class ChatMessage(Base):
    """会话中的一条消息。role ∈ {system, user, assistant}。"""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 推理过程（推理模型才有）：前端折叠展示、复盘用；老数据为空串
    reasoning_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")


class Setting(Base):
    """运行时可编辑的键值设置（当前存 LLM 端点与采样参数）。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")


# ---------------------------------------------------------------- 知识网络
class PaperArtifact(Base):
    """论文的结构化产物：summary（Step1 摘要）/ deep_reading（Step3 深读笔记）。

    一步一产物，重跑时覆盖（updated_at 变化），因此"产物存在"即可作为幂等判据。
    """

    __tablename__ = "paper_artifacts"
    __table_args__ = (UniqueConstraint("paper_id", "kind", name="uq_paper_artifact"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)      # summary | deep_reading
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reasoning_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    reasoning_effort: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    stats_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow,
                                                         onupdate=_utcnow)

    paper: Mapped["Paper"] = relationship("Paper", back_populates="artifacts")

    @property
    def stats(self) -> dict:
        try:
            return json.loads(self.stats_json or "{}")
        except (ValueError, TypeError):
            return {}


class DirectionNode(Base):
    """方向树节点：一项细分研究方向（如 LLM -> RL -> Agentic RL -> Credit Assignment）。

    path 为物化路径（用 "/" 连接祖先名），唯一约束保证同一路径不重复建点；
    depth 便于层序遍历；synthesis_* 存"最新一版阶段综述"，历史版本在 NodeSynthesis。
    """

    __tablename__ = "direction_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("direction_nodes.id"),
                                                    nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True, index=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 直接挂在本节点下的论文数（子树总数在序列化时按树聚合）
    paper_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    synthesis_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    synthesis_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    synthesis_paper_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 图谱 Markdown 的落盘路径（每个节点一份，叶子节点必有）+ 最后刷新时间
    graph_md_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    graph_md_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow,
                                                         onupdate=_utcnow)

    parent: Mapped[Optional["DirectionNode"]] = relationship(
        "DirectionNode", back_populates="children", remote_side=[id]
    )
    children: Mapped[List["DirectionNode"]] = relationship(
        "DirectionNode", back_populates="parent"
    )
    directions: Mapped[List["PaperDirection"]] = relationship(
        "PaperDirection", back_populates="node", cascade="all, delete-orphan"
    )
    syntheses: Mapped[List["NodeSynthesis"]] = relationship(
        "NodeSynthesis", back_populates="node", cascade="all, delete-orphan"
    )

    @property
    def synthesis_last_updated(self) -> Optional[datetime.datetime]:
        """对外的取用别名（与设计稿字段名对齐）。"""
        return self.synthesis_at

    @property
    def is_leaf(self) -> bool:
        return not self.children


class PaperDirection(Base):
    """论文在方向树上的归属（多归属：一篇论文可同时属于多个子问题节点）。"""

    __tablename__ = "paper_directions"
    __table_args__ = (UniqueConstraint("paper_id", "node_id", name="uq_paper_direction"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id"), nullable=False, index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("direction_nodes.id"), nullable=False,
                                        index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="primary")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 关系抽取的幂等标记：抽取过（即使 0 条边）就不再重复调用 LLM
    relations_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)
    relations_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    paper: Mapped["Paper"] = relationship("Paper", back_populates="directions")
    node: Mapped["DirectionNode"] = relationship("DirectionNode", back_populates="directions")


class PaperRelation(Base):
    """局部论文关系边（表名按设计稿：``paper_relations``）：限定在某个方向节点内。

    字段对映设计稿：`relation` = relation_type，`rationale` = description，
    `src_paper_id/target_paper_id` = source_paper_id/target_paper_id。

    relation ∈ {cites, extends, improves, contradicts, complements, applies, baseline_of, uses}
    direction："->" 表示 src 指向 dst（如 src 改进了 dst），"<->" 表示互惠/互补。
    rationale 为人类可读的理由（图谱可视化时悬停展示 / 图谱 md 里逐条列出）。
    """

    __tablename__ = "paper_relations"
    __table_args__ = (
        UniqueConstraint("node_id", "src_paper_id", "dst_paper_id", "relation",
                         name="uq_paper_relation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("direction_nodes.id"), nullable=False,
                                        index=True)
    src_paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id"), nullable=False, index=True)
    dst_paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id"), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="->")
    strength: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    @property
    def source_paper_id(self) -> int:
        return self.src_paper_id

    @property
    def target_paper_id(self) -> int:
        return self.dst_paper_id

    @property
    def relation_type(self) -> str:
        return self.relation

    @property
    def description(self) -> str:
        return self.rationale


class NodeSynthesis(Base):
    """阶段性综述（每节点每满 N 篇生成一版），保留历史便于看领域演进。"""

    __tablename__ = "node_syntheses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("direction_nodes.id"), nullable=False,
                                        index=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    paper_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paper_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="papers+N")
    stats_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    node: Mapped["DirectionNode"] = relationship("DirectionNode", back_populates="syntheses")

    @property
    def paper_ids(self) -> List[int]:
        try:
            return [int(x) for x in json.loads(self.paper_ids_json or "[]")]
        except (ValueError, TypeError):
            return []


class PipelineRun(Base):
    """pipeline 编排账本：一次"读一篇论文"的执行状态（步骤/耗时/错误）。"""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    current_step: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    steps_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    paper: Mapped["Paper"] = relationship("Paper", back_populates="pipeline_runs")

    @property
    def steps(self) -> List[dict]:
        try:
            data = json.loads(self.steps_json or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []


# ---------------------------------------------------------------- 检索 Pipeline 与缓冲区
class BufferPaper(Base):
    """待读缓冲区：检索 Pipeline 筛查通过、但尚未纳入正式知识库的论文。

    与 papers 的区别：缓冲区论文**只有元信息 + 预筛/摘要结果，不下载全文**，也没做深读与图谱；
    用户点"加入详细阅读"后才迁移成 Paper（下载全文）并触发单篇 pipeline。

    status ∈ {pending, read, rejected}：pending=留在缓冲区；read=已迁入正式库；rejected=用户丢弃。
    """

    __tablename__ = "buffer_papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arxiv_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    abstract: Mapped[str] = mapped_column(Text, nullable=False, default="")
    authors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    categories_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    published: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 预筛结果
    match_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    suggested_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    matched_node_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("direction_nodes.id"), nullable=True, index=True
    )
    screen_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 快速总结验证（Step 3）
    summary_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary_stats_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    verify_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verified_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 提升入库（点"加入详细阅读"）的状态：""/queued/running/done/failed
    promote_state: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    promote_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    promoted_paper_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("papers.id"), nullable=True, index=True
    )
    # 来源：检索 run / 手动录入
    search_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("search_runs.id"),
                                                        nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="search")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow,
                                                         onupdate=_utcnow)

    @property
    def authors(self) -> List[str]:
        try:
            return json.loads(self.authors_json or "[]")
        except (ValueError, TypeError):
            return []

    @property
    def categories(self) -> List[str]:
        try:
            return json.loads(self.categories_json or "[]")
        except (ValueError, TypeError):
            return []

    @property
    def stats(self) -> dict:
        try:
            return json.loads(self.summary_stats_json or "{}")
        except (ValueError, TypeError):
            return {}


class SearchRun(Base):
    """一次 ArXiv 检索 Pipeline 的运行状态（前端轮询 /api/search/status 用）。

    status ∈ {running, done, cancelled, failed}；stage ∈ {search, screen, summarize, done}。
    全部进度/日志以 JSON 存盘，因此进程重启后仍可看到历史（已处理的缓冲区结果不丢）。
    """

    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    stage: Mapped[str] = mapped_column(String(16), nullable=False, default="search")
    request_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    log_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    counters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime, nullable=True)

    @property
    def request(self) -> dict:
        try:
            data = json.loads(self.request_json or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    @property
    def logs(self) -> List[dict]:
        try:
            data = json.loads(self.log_json or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    @property
    def counters(self) -> dict:
        try:
            data = json.loads(self.counters_json or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}
