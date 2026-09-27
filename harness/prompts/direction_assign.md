# 方向归类（Direction Assignment）

你的任务：把一篇论文归入**研究方向树**，让知识库随每篇新论文有机生长。

## 判定规则

- 路径是"从粗到细"的层级，每层 1~4 个英文词（保留英文原词，不要译成中文），深度 2~5 层。
  例：`LLM / RL / Agentic RL / Credit Assignment`
- **优先复用已有路径**：只有现有路径确实无法表达该论文所处子问题时，才新建分支。
- 新建分支"就粗不就细"：不确定时挂到较浅的节点；不要为单篇论文造一个过细的叶子。
- 允许**多归属**（1~3 条）：论文若同时涉及两个独立子问题（如 credit assignment 与 entropy collapse），
  分别给路径并各自说明理由；不要为凑数给出不相关归属。
- 第一层尽量用领域级词：LLM / Agent / RL / Multimodal / Retrieval / Training / Inference / Evaluation 等。
- 判定要看"解决了什么"与"未来展望"两行：**未来展望只用来判断该工作属于哪个活跃子问题**
  （帮你选更准的路径），**不能因为展望里提到某个方向就给它增加一条归属**；
  每条归属都必须对应论文里**实际做的工作**。
- 不要只挂到顶层大词（如只写 `LLM`）就算完成：至少要落到能区分同类工作的层级。

## 输出格式（只输出 JSON，不要解释文字、不要代码块围栏）

```
{
  "memberships": [
    {"path": ["LLM", "RL", "Agentic RL", "Credit Assignment"],
     "role": "primary",
     "reason": "<为什么属于这条路径：引用论文里的具体问题/机制，一句话>",
     "confidence": 0.85}
  ],
  "new_nodes": [
    {"path": ["LLM", "RL", "Agentic RL"],
     "description": "<一句话说明该方向在研究什么>"}
  ]
}
```

- `role` ∈ {primary, secondary}：primary 至多 1 条，其余为 secondary。
- `confidence`：0~1 之间的小数。
- `new_nodes` 只列**本次新建**的节点（含中间层），已存在的路径不要重复列出；没有新建就给空数组。
