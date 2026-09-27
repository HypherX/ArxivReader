/* ArxivReader 前端逻辑（原生 JS，无外部依赖）。
 * 结构：状态 -> API 封装 -> 渲染 -> 事件绑定。
 */
(function () {
  "use strict";

  // ---------------- 状态 ----------------
  var state = {
    folders: [],            // 树
    flatFolders: [],        // [{id,name,depth}]
    folderMap: {},          // id -> node
    selectedFolderId: null, // null = 全部
    papers: [],
    papersLoading: false,
    bufferReady: false,
    artifacts: {},          // 当前论文的阅读产物元信息：kind -> {chars, updated_at, …}
    currentPaper: null,
    search: "",
    sessions: [],
    currentSessionId: null,
    streaming: false,
  };

  // ---------------- 小工具 ----------------
  function $(id) { return document.getElementById(id); }
  function el(tag, props, children) {
    var n = document.createElement(tag);
    if (props) for (var k in props) {
      if (k === "class") n.className = props[k];
      else if (k === "text") n.textContent = props[k];
      else if (k.indexOf("on") === 0) n.addEventListener(k.slice(2).toLowerCase(), props[k]);
      else if (k === "style") n.setAttribute("style", props[k]);
      else n.setAttribute(k, props[k]);
    }
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  var toastTimer = null;
  var TOAST_ICON = { info: "ℹ", ok: "✓", err: "✕", warn: "!" };
  function toast(msg, type, ms) {
    var t = $("toast"), kind = type || "info";
    t.replaceChildren(
      el("span", { class: "ti", text: TOAST_ICON[kind] || TOAST_ICON.info }),
      el("span", { class: "tm", text: msg })
    );
    t.className = "toast show " + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = "toast hidden"; }, ms || 3600);
  }

  // 富空态 / 骨架屏：所有列表与预览区共用同一套"等待中 / 什么都没有"观感
  function emptyState(icon, title, hint) {
    return el("div", { class: "empty rich" }, [
      el("div", { class: "icon", text: icon }),
      el("div", { class: "t", text: title }),
      el("div", { class: "h", text: hint || "" }),
    ]);
  }
  function skeleton(kind, rows) {
    var box = el("div", { class: "skel " + (kind || "rows") });
    for (var i = 0; i < (rows || 3); i++) {
      box.appendChild(el("div", { class: "skel-row" }, [
        el("div", { class: "skel-line w70" }),
        el("div", { class: "skel-line w40" }),
      ]));
    }
    return box;
  }

  // ---------------- 通用弹窗（替代原生 prompt / confirm） ----------------
  // 原生弹窗在沙箱 iframe / 部分浏览器里会被禁用并直接抛错，且样式不可控；统一走自绘弹窗。
  var dlgResolve = null;

  function closeDialog(result) {
    $("dialog-modal").classList.add("hidden");
    var fn = dlgResolve;
    dlgResolve = null;
    if (fn) fn(result);
  }

  function openDialog(opts) {
    return new Promise(function (resolve) {
      dlgResolve = resolve;
      $("dlg-title").textContent = opts.title || "确认";
      var msg = $("dlg-message");
      msg.replaceChildren();
      (opts.lines || []).forEach(function (line) {
        msg.appendChild(el("p", { class: "dlg-line", text: line }));
      });
      var withInput = opts.input !== undefined && opts.input !== null;
      $("dlg-input-wrap").classList.toggle("hidden", !withInput);
      if (withInput) {
        $("dlg-input-label").textContent = opts.inputLabel || "名称";
        $("dlg-input").value = opts.input || "";
      }
      // 多个平级选项（如“增量运行 / 强制重跑”）时，用自绘按钮代替单一确定键
      var choices = opts.choices || [];
      var actions = $("dlg-actions");
      if (!actions) {
        actions = el("span", { id: "dlg-actions", class: "dlg-actions" });
        var foot = $("dialog-modal").querySelector(".modal-foot");
        foot.insertBefore(actions, $("dlg-cancel"));
      }
      actions.replaceChildren();
      choices.forEach(function (c) {
        actions.appendChild(el("button", {
          class: "primary" + (c.danger ? " danger" : ""),
          text: c.text,
          onclick: function () { closeDialog(c.value); },
        }));
      });
      $("dlg-ok").classList.toggle("hidden", choices.length > 0);
      $("dlg-ok").textContent = opts.okText || "确定";
      $("dlg-ok").classList.toggle("danger", !!opts.danger);
      $("dialog-modal").classList.remove("hidden");
      setTimeout(function () {
        if (withInput) { $("dlg-input").focus(); $("dlg-input").select(); }
        else if (choices.length) actions.firstChild.focus();
        else $("dlg-ok").focus();
      }, 30);
    });
  }

  /** 确认框：返回 Promise<bool> */
  function askConfirm(title, lines, opts) {
    var conf = opts || {};
    return openDialog({ title: title, lines: lines, okText: conf.okText,
      danger: conf.danger }).then(function (res) {
      return res === true;
    });
  }

  /** 输入框：返回 Promise<string|null>（取消为 null） */
  function askText(title, value, label, opts) {
    var conf = opts || {};
    return openDialog({ title: title, inputLabel: label || "名称", input: value || "",
      okText: conf.okText, lines: conf.lines }).then(function (res) {
      return typeof res === "string" && res ? res : null;
    });
  }

  /** 多选一：返回被选中项的 value（取消为 null） */
  function askChoice(title, lines, choices) {
    return openDialog({ title: title, lines: lines, choices: choices });
  }

  function currentDialogValue() {
    var hasInput = !$("dlg-input-wrap").classList.contains("hidden");
    return hasInput ? ($("dlg-input").value || "").trim() : true;
  }

  // ---------------- 布局持久化 ----------------
  var LS_LAYOUT = "arxivreader.layout.v1";
  function loadLayout() { try { return JSON.parse(localStorage.getItem(LS_LAYOUT)) || {}; } catch (e) { return {}; } }
  function saveLayout(patch) {
    try {
      var cur = loadLayout();
      for (var k in patch) { if (patch.hasOwnProperty(k)) cur[k] = patch[k]; }
      localStorage.setItem(LS_LAYOUT, JSON.stringify(cur));
    } catch (e) { /* localStorage 不可用时静默降级 */ }
  }

  // ---------------- Markdown 安全渲染 ----------------
  // 策略：先整体转义 HTML，再做 Markdown 结构化替换；代码块/行内码用占位符隔离，
  // 链接仅放行 http/https。用户与模型内容一律不可注入脚本。
  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ---------------- LaTeX 数学（轻量子集，离线无依赖） ----------------
  // 覆盖常见记号：accent(\hat \bar \vec \tilde \dot)、上下标、\frac、\sqrt、希腊字母、
  // 关系/运算符、\mathbb 等；未识别命令去反斜杠降级为普通文本。不做完整 LaTeX 排版，
  // 目标是让 \(\hat{y}\)、$x^2$、$$E=mc^2$$ 这类对话内公式可读。
  var MATH_GREEK = {
    alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ϵ", varepsilon: "ε",
    zeta: "ζ", eta: "η", theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ",
    lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", pi: "π", rho: "ρ", sigma: "σ",
    tau: "τ", upsilon: "υ", phi: "ϕ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω",
    Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ",
    Upsilon: "Υ", Phi: "Φ", Psi: "Ψ", Omega: "Ω"
  };
  var MATH_SYMS = {
    times: "×", cdot: "⋅", div: "÷", pm: "±", mp: "∓", ast: "∗", star: "⋆",
    circ: "∘", bullet: "∙", leq: "≤", le: "≤", geq: "≥", ge: "≥", neq: "≠",
    ne: "≠", approx: "≈", equiv: "≡", sim: "∼", simeq: "≃", cong: "≅",
    propto: "∝", ll: "≪", gg: "≫", sum: "∑", prod: "∏", int: "∫", iint: "∬",
    iiint: "∭", oint: "∮", infty: "∞", partial: "∂", nabla: "∇", forall: "∀",
    exists: "∃", in: "∈", notin: "∉", ni: "∋", subset: "⊂", supset: "⊃",
    subseteq: "⊆", supseteq: "⊇", cup: "∪", cap: "∩", emptyset: "∅",
    varnothing: "∅", setminus: "∖", to: "→", rightarrow: "→", leftarrow: "←",
    Rightarrow: "⇒", Leftarrow: "⇐", leftrightarrow: "↔", Leftrightarrow: "⇔",
    mapsto: "↦", implies: "⟹", iff: "⟺", ldots: "…", cdots: "⋯", vdots: "⋮",
    ddots: "⋱", dots: "…", angle: "∠", perp: "⊥", parallel: "∥",
    triangle: "△", hbar: "ℏ", ell: "ℓ", aleph: "ℵ", langle: "⟨", rangle: "⟩",
    lceil: "⌈", rceil: "⌉", lfloor: "⌊", rfloor: "⌋", quad: " ", qquad: "  ",
    deg: "°", prime: "′", dagger: "†"
  };
  var MATH_ACC = {
    hat: "\u0302", widehat: "\u0302", bar: "\u0304", overline: "\u0305",
    vec: "\u20D7", tilde: "\u0303", widetilde: "\u0303", dot: "\u0307",
    ddot: "\u0308", check: "\u030C", breve: "\u0306", acute: "\u0301", grave: "\u0300"
  };
  var MATH_BB = {
    A: "𝔸", B: "𝔹", C: "ℂ", D: "𝔻", E: "𝔼", F: "𝔽", G: "𝔾", H: "ℍ",
    I: "𝕀", J: "𝕁", K: "𝕂", L: "𝕃", M: "𝕄", N: "ℕ", O: "𝕆", P: "ℙ",
    Q: "ℚ", R: "ℝ", S: "𝕊", T: "𝕋", U: "𝕌", V: "𝕍", W: "𝕎", X: "𝕏",
    Y: "𝕐", Z: "ℤ"
  };
  function mathBlackboard(c) {
    return String(c).split("").map(function (ch) { return MATH_BB[ch] || ch; }).join("");
  }
  // 把一段 LaTeX 子集转为安全 HTML（输入已被 escapeHtml，此处仅追加 <sup>/<sub>/<b>）。
  function texToHtml(raw) {
    var s = String(raw);
    s = s.replace(/\\\{/g, "\u0003").replace(/\\\}/g, "\u0004");
    s = s.replace(/\\(?:left|right|displaystyle|textstyle|limits|big|Big|bigg|Bigg)/g, "");
    s = s.replace(/\\(?:quad|qquad)/g, " ").replace(/\\[,;:!]/g, " ").replace(/\\\s/g, " ").replace(/\\\\/g, " ");
    var guard = 0, before;
    do {
      before = s;
      s = s.replace(/\\(hat|widehat|bar|overline|vec|tilde|widetilde|dot|ddot|check|breve|acute|grave)\s*\{([^{}]*)\}/g,
        function (_, cmd, arg) { return arg + (MATH_ACC[cmd] || ""); });
      s = s.replace(/\\sqrt\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}/g, "√($1)");
      s = s.replace(/\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, "($1)/($2)");
      s = s.replace(/\\mathbb\s*\{([^{}]*)\}/g, function (_, c) { return mathBlackboard(c); });
      s = s.replace(/\\(?:mathcal|mathscr|mathrm|mathsf|mathtt|mathit|text|textrm|textit|operatorname)\s*\{([^{}]*)\}/g, "$1");
      s = s.replace(/\\(?:mathbf|textbf|bm|boldsymbol)\s*\{([^{}]*)\}/g, "<b>$1</b>");
      s = s.replace(/\^\s*\{([^{}]*)\}/g, "<sup>$1</sup>");
      s = s.replace(/_\s*\{([^{}]*)\}/g, "<sub>$1</sub>");
    } while (s !== before && ++guard < 30);
    s = s.replace(/\^\s*([A-Za-z0-9])/g, "<sup>$1</sup>");
    s = s.replace(/_\s*([A-Za-z0-9])/g, "<sub>$1</sub>");
    s = s.replace(/\\([A-Za-z]+)/g, function (_, name) {
      if (Object.prototype.hasOwnProperty.call(MATH_GREEK, name)) return MATH_GREEK[name];
      if (Object.prototype.hasOwnProperty.call(MATH_SYMS, name)) return MATH_SYMS[name];
      return name;
    });
    s = s.replace(/[{}]/g, "");
    s = s.replace(/\u0003/g, "{").replace(/\u0004/g, "}");
    s = s.replace(/\s+/g, " ");
    return s.trim();
  }
  // 从（已转义的）行内文本提取数学片段，渲染后放入 bag，原位替换成 \u0001N\u0001 占位，
  // 避免其中的 * _ ~ 等被后续 Markdown 行内规则破坏。$$/\[ 为块级，\(/$ 为行内。
  function extractMath(s, bag) {
    function push(tex, display) {
      var html = texToHtml(tex);
      bag.push(display ? '<span class="math math-block">' + html + "</span>"
        : '<span class="math">' + html + "</span>");
      return "\u0001" + (bag.length - 1) + "\u0001";
    }
    s = s.replace(/\$\$([\s\S]+?)\$\$/g, function (_, t) { return push(t, true); });
    s = s.replace(/\\\[([\s\S]+?)\\\]/g, function (_, t) { return push(t, true); });
    s = s.replace(/\\\(([\s\S]+?)\\\)/g, function (_, t) { return push(t, false); });
    s = s.replace(/\$([^$\n]+?)\$/g, function (_, t) {
      return /[\\^_{}]/.test(t) ? push(t, false) : "$" + t + "$";
    });
    return s;
  }

  function inlineMd(s) {
    var codes = [];
    s = s.replace(/`([^`]+)`/g, function (_, c) { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    var maths = [];
    s = extractMath(s, maths);
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (_, t, u) {
      return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + t + '</a>';
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(/\u0000(\d+)\u0000/g, function (_, i) { return "<code>" + codes[+i] + "</code>"; });
    s = s.replace(/\u0001(\d+)\u0001/g, function (_, i) { return maths[+i]; });
    return s;
  }
  function splitRow(line) {
    var t = line.replace(/^\s*\|/, "").replace(/\|\s*$/, "");
    return t.split("|").map(function (c) { return inlineMd(c.trim()); });
  }
  function blocksToHtml(chunk) {
    var lines = escapeHtml(chunk).split("\n");
    var html = [], listType = null, listBuf = [], tableBuf = [];
    function flushList() {
      if (listType) {
        html.push("<" + listType + ">" + listBuf.map(function (li) { return "<li>" + li + "</li>"; }).join("") + "</" + listType + ">");
        listBuf = []; listType = null;
      }
    }
    function flushTable() {
      if (!tableBuf.length) return;
      var rows = tableBuf.slice(); tableBuf = [];
      var isSep = /^\s*\|?[\s:|-]+\|?\s*$/.test(rows[1] || "") && /-/.test(rows[1] || "");
      var out = ["<table>"];
      var startIdx = 0;
      if (isSep) {
        out.push("<thead><tr>" + splitRow(rows[0]).map(function (c) { return "<th>" + c + "</th>"; }).join("") + "</tr></thead>");
        startIdx = 2;
      }
      out.push("<tbody>");
      for (var r = startIdx; r < rows.length; r++) {
        out.push("<tr>" + splitRow(rows[r]).map(function (c) { return "<td>" + c + "</td>"; }).join("") + "</tr>");
      }
      out.push("</tbody></table>");
      html.push(out.join(""));
    }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i], m;
      if (/^\s*\|.*\|\s*$/.test(line)) { flushList(); tableBuf.push(line); continue; }
      flushTable();
      if (/^\s*$/.test(line)) { flushList(); continue; }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flushList(); var lv = m[1].length; html.push("<h" + lv + ">" + inlineMd(m[2]) + "</h" + lv + ">"); continue; }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { flushList(); html.push("<hr>"); continue; }
      if ((m = line.match(/^\s*&gt;\s?(.*)$/))) { flushList(); html.push("<blockquote>" + inlineMd(m[1]) + "</blockquote>"); continue; }
      if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) { if (listType !== "ul") { flushList(); listType = "ul"; } listBuf.push(inlineMd(m[1])); continue; }
      if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) { if (listType !== "ol") { flushList(); listType = "ol"; } listBuf.push(inlineMd(m[1])); continue; }
      flushList();
      html.push("<p>" + inlineMd(line) + "</p>");
    }
    flushList(); flushTable();
    return html.join("");
  }
  function renderMarkdown(text) {
    if (!text) return "";
    var src = String(text).replace(/\r\n/g, "\n");
    var parts = src.split("```"), out = [];
    for (var pi = 0; pi < parts.length; pi++) {
      if (pi % 2 === 1) {
        var code = parts[pi].replace(/^[^\n]*\n/, "");
        out.push("<pre><code>" + escapeHtml(code.replace(/\n$/, "")) + "</code></pre>");
      } else if (parts[pi]) {
        out.push(blocksToHtml(parts[pi]));
      }
    }
    return out.join("");
  }
  function setMd(node, text) {
    var wrap = el("span", { class: "md" });
    wrap.innerHTML = renderMarkdown(text);
    node.replaceChildren(wrap);
  }

  // ---------------- 推理过程（reasoning_content）折叠块 ----------------
  // 推理内容是模型的思考过程，用纯文本展示（不做 Markdown 渲染），
  // 流式期间自动展开、结束后自动折叠，历史消息默认折叠可手动展开。
  function makeReasoningBlock(parent) {
    var label = el("span", { class: "reasoning-label", text: "推理过程" });
    var hint = el("span", { class: "reasoning-hint", text: "（点击展开/收起）" });
    var head = el("summary", { class: "reasoning-head" }, [label, hint]);
    var body = el("div", { class: "reasoning-body" });
    var wrap = el("details", { class: "reasoning" }, [head, body]);
    parent.insertBefore(wrap, parent.firstChild);
    return { wrap: wrap, body: body, label: label };
  }
  function renderReasoning(block, text, streaming) {
    block.body.textContent = text;
    block.label.textContent = "推理过程（" + text.length + " 字" + (streaming ? "，生成中…" : "") + "）";
  }

  // ---------------- API 封装 ----------------
  function req(method, url, body) {
    var opt = { method: method, headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      if (r.status === 204) return null;
      return r.text().then(function (txt) {
        var data = txt ? JSON.parse(txt) : null;
        if (!r.ok) { var d = (data && data.detail) ? data.detail : ("HTTP " + r.status); throw new Error(d); }
        return data;
      });
    });
  }
  var api = {
    get: function (u) { return req("GET", u); },
    post: function (u, b) { return req("POST", u, b || {}); },
    put: function (u, b) { return req("PUT", u, b || {}); },
    patch: function (u, b) { return req("PATCH", u, b || {}); },
    del: function (u) { return req("DELETE", u); },
  };

  // ---------------- 文件夹 ----------------
  function flatten(nodes, depth, out) {
    nodes.forEach(function (n) {
      out.push({ id: n.id, name: n.name, depth: depth });
      state.folderMap[n.id] = n;
      if (n.children && n.children.length) flatten(n.children, depth + 1, out);
    });
  }
  function loadFolders() {
    return api.get("/api/folders").then(function (tree) {
      state.folders = tree; state.flatFolders = []; state.folderMap = {};
      flatten(tree, 0, state.flatFolders);
      renderFolderTree(); renderFolderSelects();
      var total = state.papers.length;
      $("all-count").textContent = state.selectedFolderId === null ? total : countAll(tree);
    });
  }
  function countAll(nodes) {
    return nodes.reduce(function (s, n) { return s + (n.paper_count || 0) + countAll(n.children || []); }, 0);
  }
  function renderFolderTree() {
    var box = $("folder-tree"); box.replaceChildren();
    box.appendChild(buildTree(state.folders, 0));
  }
  function buildTree(nodes, depth) {
    var frag = document.createDocumentFragment();
    nodes.forEach(function (n) {
      var pad = 12 + depth * 14;
      var row = el("div", {
        class: "folder-row" + (state.selectedFolderId === n.id ? " selected" : "") + (n.direction_node_id ? " dir-node" : ""),
        style: "padding-left:" + pad + "px",
        "data-id": n.id,
      }, [
        el("span", { class: "fname", text: (n.direction_node_id ? "🧭 " : "") + n.name }),
        el("span", { class: "count", text: String(n.paper_count || 0) }),
        el("span", { class: "ops" }, [
          el("button", { title: "新建子文件夹", "data-act": "add", "data-id": n.id, text: "＋" }),
          el("button", { title: "重命名", "data-act": "rename", "data-id": n.id, text: "✎" }),
          el("button", { title: "删除", "data-act": "del", "data-id": n.id, text: "🗑" }),
        ]),
      ]);
      row.addEventListener("click", function (e) {
        var act = e.target.getAttribute && e.target.getAttribute("data-act");
        if (act) { e.stopPropagation(); folderOp(act, n); return; }
        selectFolder(n.id);
      });
      row.addEventListener("contextmenu", function (e) { e.preventDefault(); openCtxMenu(e, n); });
      frag.appendChild(row);
      if (n.children && n.children.length) frag.appendChild(buildTree(n.children, depth + 1));
    });
    return frag;
  }

  // 🧭 文件夹是方向树的镜像：改名要走方向节点（后代路径与图谱 md 才会一起平移），
  // 删除则是删方向节点（论文保留），普通文件夹则维持原来的"子项上移"语义。
  function addSubFolder(node) {
    askText("新建子文件夹", "", "文件夹名", { lines: ["将建在「" + node.name + "」下。"] })
      .then(function (name) {
        if (!name) return null;
        return api.post("/api/folders", { name: name, parent_id: node.id })
          .then(loadFolders).then(loadPapers).catch(function (e) { toast(e.message, "err"); });
      });
  }

  function renameFolder(node) {
    var dirNodeId = node.direction_node_id;
    askText(dirNodeId ? "重命名方向节点" : "重命名文件夹", node.name, "新名称",
      dirNodeId ? { lines: ["方向节点是知识网络的单一数据源：改名会同步镜像文件夹与图谱文档。"] } : null)
      .then(function (name) {
        if (!name || name === node.name) return null;
        var target = dirNodeId ? ("/api/knowledge/nodes/" + dirNodeId)
          : ("/api/folders/" + node.id);
        return api.patch(target, { name: name }).then(function () {
          toast("已重命名为「" + name + "」", "ok");
          return loadFolders();
        }).then(loadPapers).then(function () {
          if (dirNodeId && currentGraphNodeId === dirNodeId) loadNodeGraph(dirNodeId);
        }).catch(function (e) { toast(e.message, "err"); });
      });
  }

  function deleteFolder(node) {
    if (node.direction_node_id) {
      var nodeId = node.direction_node_id;
      askConfirm("删除方向节点", [
        "「" + node.name + "」是知识网络维护的方向文件夹。",
        "将删除该方向节点及其归属 / 关系边 / 阶段综述；论文与 PDF 保留，子方向上移一层。",
      ], { okText: "删除", danger: true }).then(function (ok) {
        if (!ok) return null;
        return api.del("/api/knowledge/nodes/" + nodeId).then(function () {
          toast("已删除方向节点", "ok");
          if (currentGraphNodeId === nodeId) restoreViewerAfterGraph();
          return loadFolders();
        }).then(loadPapers).catch(function (e) { toast(e.message, "err"); });
      });
      return;
    }
    askConfirm("删除文件夹", ["删除「" + node.name + "」？", "其中的子文件夹与论文将上移到父级，不会删除论文。"],
      { okText: "删除", danger: true }).then(function (ok) {
      if (!ok) return null;
      return api.del("/api/folders/" + node.id).then(function () {
        if (state.selectedFolderId === node.id) state.selectedFolderId = null;
        return loadFolders();
      }).then(loadPapers).catch(function (e) { toast(e.message, "err"); });
    });
  }

  function folderOp(act, node) {
    if (act === "add") addSubFolder(node);
    else if (act === "rename") renameFolder(node);
    else if (act === "del") deleteFolder(node);
  }

  // ---------------- 右键菜单（文件夹树） ----------------
  function closeCtxMenu() {
    var menu = $("ctx-menu");
    if (menu) menu.classList.add("hidden");
  }

  function openCtxMenu(e, node) {
    var menu = $("ctx-menu");
    if (!menu) return;
    var items = [
      { text: "＋   新建子文件夹", run: function () { addSubFolder(node); } },
      { text: "✎   重命名", run: function () { renameFolder(node); } },
    ];
    if (node.direction_node_id) {
      items.push({ text: "🧭   查看论文图谱", run: function () { selectFolder(node.id); } });
      items.push({ text: "⇪   重建知识库文件", run: function () { exportKnowledgeFiles(); } });
    }
    items.push({ sep: true });
    items.push({ text: "🗑   删除", danger: true, run: function () { deleteFolder(node); } });

    menu.replaceChildren();
    items.forEach(function (it) {
      if (it.sep) { menu.appendChild(el("div", { class: "ctx-sep" })); return; }
      menu.appendChild(el("div", {
        class: "ctx-item" + (it.danger ? " danger" : ""),
        text: it.text,
        onclick: function () { closeCtxMenu(); it.run(); },
      }));
    });
    menu.classList.remove("hidden");
    var w = menu.offsetWidth || 190, h = menu.offsetHeight || 160;
    menu.style.left = Math.max(8, Math.min(e.clientX, window.innerWidth - w - 10)) + "px";
    menu.style.top = Math.max(8, Math.min(e.clientY, window.innerHeight - h - 10)) + "px";
  }

  // 一键重建：把方向树同步成文件夹（🧭）并重写每个节点的 GRAPH.md
  function exportKnowledgeFiles() {
    toast("正在重建知识库文件…");
    api.post("/api/knowledge/export", {}).then(function (res) {
      toast("已重建 " + (res.count || 0) + " 份图谱 md"
        + (res.moved_papers ? "，归位 " + res.moved_papers + " 篇论文" : ""), "ok");
      if (currentGraphNodeId) loadNodeGraph(currentGraphNodeId);
      return loadFolders();
    }).catch(function (e) { toast(e.message, "err"); });
  }
  function renderFolderSelects() {
    [["target-folder", true], ["rule-folder", false], ["move-folder", false]].forEach(function (pair) {
      var sel = $(pair[0]); if (!sel) return;
      var prev = sel.value; sel.replaceChildren();
      if (pair[1]) sel.appendChild(el("option", { value: "", text: "自动归档（按规则）" }));
      state.flatFolders.forEach(function (f) {
        sel.appendChild(el("option", { value: String(f.id), text: "　".repeat(f.depth) + f.name }));
      });
      if (prev) sel.value = prev;
    });
  }
  function selectFolder(id) {
    state.selectedFolderId = id;
    $("folder-all").classList.toggle("selected", id === null);
    renderFolderTree(); loadPapers();
    // 方向文件夹（🧭）额外展示该节点的论文图谱（每个节点一份 GRAPH.md + 内置 SVG 图）
    var node = id !== null ? state.folderMap[id] : null;
    if (node && node.direction_node_id) showNodeGraph(node.direction_node_id);
    else hideNodeGraph();
  }

  // ---------------- ArXiv 检索页（检索 Pipeline + 待读缓冲区） ----------------
  var SEARCH_CATS = ["cs.CL", "cs.AI", "cs.LG", "cs.CV", "cs.NE", "stat.ML", "cs.RO", "cs.SE"];
  var SEARCH_STAGES = [
    { name: "search", title: "ArXiv 批量检索" },
    { name: "screen", title: "方向树预筛" },
    { name: "summarize", title: "快速总结验证" },
    { name: "buffer", title: "写入待读缓冲区" },
  ];
  var RUN_STATE = { running: "运行中", done: "已完成", failed: "失败",
    cancelled: "已中断", canceled: "已中断", stopped: "已中断" };
  var searchTimer = null;
  var searchRunId = null;
  var searchPolls = 0;
  var searchPolledRun = null;
  var SEARCH_POLL_LIMIT = 900;        // 2s 一次 × 900 ≈ 30 分钟：服务端异常时也不让界面无限轮询

  function initSearchCats() {
    var box = $("s-cats");
    if (!box || box.getAttribute("data-ready")) return;
    box.setAttribute("data-ready", "1");
    SEARCH_CATS.forEach(function (c) {
      var cb = el("input", { type: "checkbox", value: c });
      if (c === "cs.CL" || c === "cs.AI" || c === "cs.LG") cb.checked = true;
      box.appendChild(el("label", { class: "cat" }, [cb, el("span", { text: c })]));
    });
    box.appendChild(el("input", { id: "s-cats-custom", type: "text", placeholder: "自定义分类，逗号分隔" }));
  }

  var LS_SEARCH = "arxivreader.search.v1";

  function openSearch() {
    initSearchCats();
    restoreSearchForm();
    $("search-modal").classList.remove("hidden");
    loadBuffer();
    if (searchRunId) pollSearch(searchRunId, true);
  }

  // 检索表单持久化：能跨会话记住上次的检索条件（这类调研往往是连续几天的）
  function saveSearchForm(body) {
    try { localStorage.setItem(LS_SEARCH, JSON.stringify(body || collectSearchBody())); }
    catch (e) { /* localStorage 不可用时静默降级 */ }
  }

  function restoreSearchForm() {
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(LS_SEARCH)); } catch (e) { saved = null; }
    var today = new Date().toISOString().slice(0, 10);
    if (!saved) { $("s-date-to").value = today; return; }
    var cats = saved.categories || [];
    document.querySelectorAll("#s-cats input[type=checkbox]").forEach(function (cb) {
      cb.checked = cats.indexOf(cb.value) >= 0;
    });
    if ($("s-cats-custom"))
      $("s-cats-custom").value = cats.filter(function (c) { return SEARCH_CATS.indexOf(c) < 0; }).join(", ");
    $("s-keywords").value = (saved.keywords || []).join(", ");
    $("s-kw-mode").value = saved.keyword_mode || "and";
    $("s-date-from").value = saved.date_from || "";
    $("s-date-to").value = saved.date_to || today;
    if (saved.max_results) $("s-max").value = saved.max_results;
    if (saved.min_score !== null && saved.min_score !== undefined) $("s-min-score").value = saved.min_score;
    if (saved.max_summarize !== null && saved.max_summarize !== undefined) $("s-max-sum").value = saved.max_summarize;
  }

  function collectSearchBody() {
    var cats = [];
    document.querySelectorAll("#s-cats input[type=checkbox]:checked").forEach(function (cb) { cats.push(cb.value); });
    var custom = ($("s-cats-custom") && $("s-cats-custom").value || "").split(/[,，\s]+/).filter(Boolean);
    custom.forEach(function (c) { if (cats.indexOf(c) < 0) cats.push(c); });
    var keywords = $("s-keywords").value.split(/[,，\s]+/).filter(Boolean);
    return {
      categories: cats, keywords: keywords,
      date_from: $("s-date-from").value || null, date_to: $("s-date-to").value || null,
      max_results: Number($("s-max").value || 40), sort_by: "submitted",
      keyword_mode: $("s-kw-mode").value,
      min_score: $("s-min-score").value === "" ? null : Number($("s-min-score").value),
      max_summarize: $("s-max-sum").value === "" ? null : Number($("s-max-sum").value),
    };
  }

  function startSearch() {
    var body = collectSearchBody();
    if (!body.categories.length && !body.keywords.length && !body.date_from) {
      toast("至少填写一个检索条件（分类 / 关键词 / 日期）", "err"); return;
    }
    saveSearchForm(body);
    searchPolls = 0;
    searchPolledRun = null;
    $("s-run").disabled = true;
    $("s-cancel").classList.remove("hidden");
    $("s-progress").classList.remove("hidden");
    $("s-logs").replaceChildren(el("div", { class: "log info", text: "已提交，等待第一批结果…" }));
    api.post("/api/search/run", body).then(function (run) {
      searchRunId = run.id;
      renderSearchRun(run);
      pollSearch(run.id, false);
    }).catch(function (e) { toast(e.message, "err"); $("s-run").disabled = false; $("s-cancel").classList.add("hidden"); });
  }

  function stageDetail(logs, title) {
    for (var i = logs.length - 1; i >= 0; i--) {
      var m = logs[i].message || "";
      if (m.indexOf("[" + title + "] done") === 0) return m.replace("[" + title + "] done", "").trim();
    }
    return "";
  }

  function renderSearchRun(run) {
    var logs = run.logs || [];
    // 兼容历史数据：stage 已 done 但 status 未写回的 run 视为已结束（否则界面永远停在“运行中”）
    var effStatus = (run.status === "running" && run.stage === "done") ? "done" : run.status;
    var box = $("s-stages"); box.replaceChildren();
    SEARCH_STAGES.forEach(function (s) {
      var done = logs.some(function (l) { return (l.message || "").indexOf("[" + s.title + "] done") === 0; });
      var running = !done && s.name === (run.stage || "") && effStatus === "running";
      box.appendChild(el("div", { class: "stage " + (done ? "done" : (running ? "running" : "todo")) }, [
        el("span", { class: "dot", text: done ? "✓" : (running ? "◐" : "○") }),
        el("span", { class: "st", text: s.title }),
        el("span", { class: "sd", text: stageDetail(logs, s.title) }),
      ]));
    });
    var stateText = RUN_STATE[effStatus] || effStatus;
    box.appendChild(el("div", { class: "stage " + (effStatus === "done" ? "done" : (effStatus === "running" ? "running" : "failed")) }, [
      el("span", { class: "dot", text: effStatus === "done" ? "✓" : (effStatus === "running" ? "◐" : "■") }),
      el("span", { class: "st", text: "任务：" + stateText
        + (run.cancel_requested && effStatus === "running" ? "（已请求中断，当前批次结束后停止）" : "") }),
    ]));

    var c = run.counters || {};
    $("s-bar").style.width = (effStatus === "done" ? 100
      : Math.min(97, Math.round(100 * ((c.screened || 0) + (c.summarized || 0)) / Math.max(1, 2 * (c.found || 1))))) + "%";
    $("s-counters").textContent = "命中 " + (c.found || 0) + " · 去重跳过 " + (c.duplicated || 0)
      + " · 预筛过线 " + (c.passed || 0) + " · 已复核 " + (c.summarized || 0)
      + " · 入缓冲区 " + (c.buffered || 0) + " · 丢弃 " + (c.dropped || 0)
      + (run.error ? " · 错误：" + run.error : "");

    var html = logs.slice(-60).map(function (l) {
      return '<div class="log ' + escapeHtml(l.level || "info") + '"><span class="lt">'
        + escapeHtml(l.ts || "") + "</span> " + escapeHtml(l.message || "") + "</div>";
    }).join("");
    var logBox = $("s-logs");
    if (logBox.getAttribute("data-last") !== html) {
      logBox.innerHTML = html; logBox.scrollTop = logBox.scrollHeight; logBox.setAttribute("data-last", html);
    }
  }

  function pollSearch(runId, silent) {
    clearTimeout(searchTimer);
    if (searchPolledRun !== runId) { searchPolledRun = runId; searchPolls = 0; }
    api.get("/api/search/status?run_id=" + runId).then(function (run) {
      renderSearchRun(run);
      searchPolls += 1;
      // stage 已 done 的 run 就算 status 还写着 running 也不再轮询（兼容历史数据）
      var running = run.status === "running" && run.stage !== "done";
      if (running && searchPolls < SEARCH_POLL_LIMIT) {
        searchTimer = setTimeout(function () { pollSearch(runId, silent); }, 2000);
        return;
      }
      $("s-run").disabled = false;
      $("s-cancel").classList.add("hidden");
      if (running) {
        toast("检索已超过 30 分钟，界面停止轮询（任务仍在后台跑，可重新打开此页查看）", "warn", 8000);
        return;
      }
      loadBuffer();
      var effStatus = (run.status === "running" && run.stage === "done") ? "done" : run.status;
      if (!silent) {
        toast("检索" + (effStatus === "done" ? "完成" : (effStatus === "failed" ? "失败" : "已停止"))
          + "：命中 " + ((run.counters || {}).found || 0) + " · 入缓冲区 "
          + ((run.counters || {}).buffered || 0) + " 篇"
          + (run.error ? "（" + run.error + "）" : ""),
          effStatus === "done" ? "ok" : (effStatus === "failed" ? "err" : "warn"),
          effStatus === "done" ? 3600 : 6000);
      }
    }).catch(function (e) { if (!silent) toast(e.message, "err"); });
  }

  function cancelSearch() {
    if (!searchRunId) return;
    api.post("/api/search/cancel?run_id=" + searchRunId).then(function () {
      toast("已请求中断：当前批次结束后停止，已处理结果保留", "info");
    }).catch(function (e) { toast(e.message, "err"); });
  }

  function loadBuffer() {
    var box = $("s-buffer");
    if (box && !state.bufferReady) box.replaceChildren(skeleton("rows", 3));
    return api.get("/api/buffer?status=pending&limit=200").then(function (data) {
      var counts = data.counts || {};
      $("s-buffer-counts").textContent = "待读 " + (counts.pending || 0) + " · 已读 "
        + (counts.read || 0) + " · 已丢弃 " + (counts.rejected || 0);
      state.bufferReady = true;
      renderBuffer(data.items || []);
    }).catch(function (e) { toast(e.message, "err"); });
  }

  function renderBuffer(items) {
    var box = $("s-buffer"); box.replaceChildren();
    if (!items.length) {
      box.appendChild(emptyState("📥", "缓冲区是空的",
        "在上面填好条件点「开始检索」，命中的论文会先落到这里，由你决定是否加入详细阅读。"));
      return;
    }
    items.forEach(function (b) {
      var verified = !!b.summary_text;
      var ops = el("div", { class: "b-ops" }, [
        el("button", { class: "mini", text: "预览", onclick: function () { previewBuffer(b, row); } }),
      ]);
      if (!verified) {
        ops.appendChild(el("button", {
          class: "mini", title: "对该篇补跑快速总结与归属复核（不下载全文）",
          text: "补跑总结",
          onclick: function (ev) { summarizeBuffer(b, ev.target); },
        }));
      }
      ops.appendChild(el("button", { class: "mini primary", text: "加入详细阅读", onclick: function () { promoteBuffer(b, row); } }));
      ops.appendChild(el("button", { class: "mini danger", text: "丢弃", onclick: function () { rejectBuffer(b, row); } }));

      var row = el("div", { class: "buffer-item" }, [
        el("div", { class: "b-head" }, [
          el("span", { class: "b-title", text: b.title || b.arxiv_id }),
          el("span", { class: "b-score", text: (b.match_score || 0).toFixed(2) }),
        ]),
        el("div", { class: "b-meta", text: [(b.authors || []).slice(0, 3).join(", "),
          (b.published || "").slice(0, 10), b.arxiv_id].filter(Boolean).join(" · ") }),
        el("div", { class: "b-badges" }, [
          el("span", { class: "b-chip" + (verified ? " ok" : " warn"),
            text: verified ? "✓ 已复核总结" : "⚠ 待复核（仅预筛）" }),
          el("span", { class: "b-chip path", text: "🧭 " + (b.verified_path || b.suggested_path || "模型未给出路径") }),
        ]),
        el("div", { class: "b-reason", text: b.verify_reason || b.screen_reason || "" }),
        ops,
        el("div", { class: "b-detail hidden" }),
      ]);
      box.appendChild(row);
    });
  }

  function summarizeBuffer(b, btn) {
    btn.disabled = true;
    btn.textContent = "总结中…";
    api.post("/api/buffer/" + b.id + "/summarize", {}).then(function () {
      toast("快速总结已完成", "ok");
      loadBuffer();
    }).catch(function (e) {
      toast("总结失败：" + e.message, "err");
      btn.disabled = false; btn.textContent = "补跑总结";
    });
  }

  function previewBuffer(b, row) {
    var detail = row.querySelector(".b-detail");
    if (!detail.classList.contains("hidden")) { detail.classList.add("hidden"); return; }
    api.get("/api/buffer/" + b.id).then(function (full) {
      detail.replaceChildren();
      detail.appendChild(el("div", { class: "b-sec", text: "摘要" }));
      detail.appendChild(el("div", { class: "b-text", text: full.abstract || "（无）" }));
      detail.appendChild(el("div", { class: "b-sec", text: "快速总结（含“解决了什么 / 未来展望”）" }));
      var md = el("div", { class: "md" });
      setMd(md, full.summary_text || "（尚未生成：点“加入详细阅读”会自动补跑）");
      detail.appendChild(md);
      detail.appendChild(el("div", { class: "b-sec", text: "归属复核理由" }));
      detail.appendChild(el("div", { class: "b-text", text: full.verify_reason || "—" }));
      detail.classList.remove("hidden");
    }).catch(function (e) { toast(e.message, "err"); });
  }

  function promoteBuffer(b, row) {
    askConfirm("加入详细阅读", [
      "《" + shortText(b.title || b.arxiv_id, 46) + "》",
      "会下载全文并自动跑：快速总结 → 方向树归属 → 深度精读 → 局部图谱 → 阶段综述。",
      "处理在后台进行，可关闭窗口；导入后可在左侧方向文件夹里看到它。",
    ], { okText: "开始处理" }).then(function (ok) {
      if (!ok) return null;
      var badge = el("span", { class: "b-state", text: "排队中…" });
      row.querySelector(".b-ops").appendChild(badge);
      return api.post("/api/buffer/" + b.id + "/promote?background=true&trigger_pipeline=true", {}).then(function () {
        var timer = setInterval(function () {
          api.get("/api/buffer/" + b.id).then(function (full) {
            if (full.promote_state === "running" || full.promote_state === "queued") {
              badge.textContent = full.promote_state === "running" ? "处理中（下载/精读/图谱）…" : "排队中…";
              return;
            }
            clearInterval(timer);
            if (full.promote_state === "done") {
              badge.textContent = "已入库 ✓";
              toast("已加入详细阅读，并完成知识网络 pipeline", "ok");
              loadFolders(); loadPapers(); loadBuffer();
            } else {
              badge.textContent = "失败";
              toast("提升失败：" + (full.promote_error || "未知错误"), "err");
            }
          }).catch(function () { /* 轮询失败就下次再试 */ });
        }, 3000);
      }).catch(function (e) { badge.remove(); toast(e.message, "err"); });
    });
  }

  function rejectBuffer(b, row) {
    api.post("/api/buffer/" + b.id + "/reject", {}).then(function () {
      row.remove(); loadBuffer(); toast("已丢弃", "info");
    }).catch(function (e) { toast(e.message, "err"); });
  }

  // ---------------- 方向节点论文图谱（md + 无依赖 SVG） ----------------
  var currentGraphNodeId = null;
  var REL_LABEL = { cites: "引用", extends: "扩展", improves: "改进", contradicts: "对立",
    complements: "互补", applies: "应用", baseline_of: "基线", uses: "使用" };

  function showNodeGraph(nodeId) {
    currentGraphNodeId = nodeId;
    $("pdf-frame").classList.add("hidden");
    $("viewer-empty").classList.add("hidden");
    $("viewer-toolbar").classList.add("hidden");
    $("graph-view").classList.remove("hidden");
    loadNodeGraph(nodeId);
  }

  function hideNodeGraph() {
    currentGraphNodeId = null;
    $("graph-view").classList.add("hidden");
    // 关掉图谱后既没有图谱也没有 PDF 时，把空态露出来（否则预览区留白）
    if ($("pdf-frame").classList.contains("hidden")) $("viewer-empty").classList.remove("hidden");
  }

  function showViewerEmpty() {
    $("pdf-frame").classList.add("hidden");
    $("viewer-toolbar").classList.add("hidden");
    $("graph-view").classList.add("hidden");
    $("viewer-empty").classList.remove("hidden");
  }

  // 图谱关掉后回到上一状态：选中的论文还在就回到 PDF，否则回到空态
  function restoreViewerAfterGraph() {
    currentGraphNodeId = null;
    if (state.currentPaper) selectPaper(state.currentPaper);
    else showViewerEmpty();
  }

  function loadNodeGraph(nodeId) {
    $("graph-svg-box").replaceChildren(skeleton("graph", 2));
    $("graph-papers").replaceChildren();
    $("graph-legend").classList.add("hidden");
    $("pdf-frame").classList.add("hidden");
    $("viewer-empty").classList.add("hidden");
    $("viewer-toolbar").classList.add("hidden");
    $("graph-view").classList.remove("hidden");
    $("graph-del").disabled = false;

    api.get("/api/knowledge/nodes/" + nodeId).then(function (payload) {
      var node = payload.node || {}, graph = payload.graph || {};
      $("graph-path").textContent = "🧭 " + (node.path || "");
      $("graph-meta").textContent = [
        "论文 " + (node.paper_count || 0) + " 篇（子树 " + (node.subtree_paper_count || 0) + "）",
        "关系边 " + ((graph.edges || []).length) + " 条",
        node.has_synthesis ? "已有阶段综述" : "暂无阶段综述",
        node.graph_md_path || "（md 尚未落盘）",
      ].join("　·　");
      $("graph-svg-box").innerHTML = graphSvg(graph);
      $("graph-svg-box").scrollLeft = 0;
      renderGraphLegend(graph);
      renderGraphPapers(payload.papers || []);
      $("graph-md-link").setAttribute("href", "/api/knowledge/nodes/" + nodeId + "/graph.md");
    }).catch(function (e) { toast(e.message, "err"); });

    fetch("/api/knowledge/nodes/" + nodeId + "/graph.md")
      .then(function (r) { return r.text(); })
      .then(function (text) { setMd($("graph-md"), text); })
      .catch(function () { setMd($("graph-md"), "（图谱文档加载失败）"); });
  }

  // 关系图例：把边上出现的 relation 与节点归属颜色讲清楚（否则圆和线看不懂）
  function renderGraphLegend(graph) {
    var box = $("graph-legend");
    var kinds = {};
    (graph.edges || []).forEach(function (e) { kinds[e.relation] = (kinds[e.relation] || 0) + 1; });
    var keys = Object.keys(kinds);
    box.replaceChildren();
    if (keys.length) {
      box.appendChild(el("span", { class: "lg-title", text: "关系" }));
      keys.forEach(function (k) {
        box.appendChild(el("span", { class: "lg-item" }, [
          el("span", { class: "lg-line" }),
          el("span", { text: (REL_LABEL[k] || k) + "　" + kinds[k] }),
        ]));
      });
    }
    box.appendChild(el("span", { class: "lg-title", text: "归属" }));
    box.appendChild(el("span", { class: "lg-item" }, [el("span", { class: "lg-dot primary" }), el("span", { text: "primary" })]));
    box.appendChild(el("span", { class: "lg-item" }, [el("span", { class: "lg-dot" }), el("span", { text: "secondary" })]));
    box.classList.remove("hidden");
  }

  // 图谱下方的论文卡片：图看不清标题时，从卡片直接打开 PDF
  function renderGraphPapers(papers) {
    var box = $("graph-papers");
    box.replaceChildren();
    if (!papers.length) return;
    box.appendChild(el("div", { class: "gp-title", text: "该方向下的论文（点击打开 PDF）" }));
    var grid = el("div", { class: "gp-grid" });
    papers.forEach(function (p) {
      var card = el("div", {
        class: "gp-card" + (p.role === "primary" ? " primary" : ""),
        title: p.reason || "",
      }, [
        el("div", { class: "gp-name", text: p.title || p.arxiv_id }),
        el("div", { class: "gp-meta", text: [p.arxiv_id, (p.added_at || "").slice(0, 10),
          p.role === "primary" ? "primary" : "secondary"].filter(Boolean).join(" · ") }),
      ]);
      card.addEventListener("click", function () { openPaperById(p.id); });
      grid.appendChild(card);
    });
    box.appendChild(grid);
  }

  function openPaperById(paperId) {
    api.get("/api/papers/" + paperId).then(function (p) {
      selectPaper(p);
      toast("已在预览中打开：" + shortText(p.title || p.arxiv_id, 30));
    }).catch(function (e) { toast(e.message, "err"); });
  }

  // 删除当前方向节点（图谱视图里的入口）：论文保留，子方向上移一层
  function deleteCurrentGraphNode() {
    if (!currentGraphNodeId) return;
    var path = ($("graph-path").textContent || "").replace("🧭 ", "").trim();
    var nodeId = currentGraphNodeId;
    askConfirm("删除方向节点", [
      "删除「" + path + "」？",
      "会移除该节点及其论文归属、关系边与阶段综述；论文与 PDF 保留，子方向上移一层。",
    ], { okText: "删除", danger: true }).then(function (ok) {
      if (!ok) return null;
      $("graph-del").disabled = true;
      return api.del("/api/knowledge/nodes/" + nodeId).then(function () {
        toast("已删除「" + path + "」", "ok");
        if (currentGraphNodeId === nodeId) restoreViewerAfterGraph();
        return loadFolders();
      }).then(loadPapers).catch(function (e) {
        toast(e.message, "err");
        $("graph-del").disabled = false;
      });
    });
  }

  function graphSvg(graph) {
    var nodes = (graph.nodes || []).slice(), edges = graph.edges || [];
    if (!nodes.length) {
      return '<div class="g-empty">该方向下还没有论文：从「ArXiv 检索」把匹配论文加入详细阅读后会自动挂载。</div>';
    }
    // 画布按内容自适应：节点少时不留大片空白（窄栏下文字也不会被缩得看不清）
    var tau = Math.PI * 2;
    var radius = Math.min(212, 118 + nodes.length * 11);
    var w = Math.round(2 * (radius + 96)), h = Math.round(2 * (radius + 74));
    var cx = w / 2, cy = h / 2;
    var twoRings = nodes.length > 9;             // 节点多时外内双环，避免标签互压
    var outer = Math.ceil(nodes.length / 2);
    var pos = {};
    nodes.forEach(function (n, i) {
      var count = twoRings ? (i < outer ? outer : nodes.length - outer) : nodes.length;
      var slot = twoRings ? (i < outer ? i : i - outer) : i;
      if (twoRings && i >= outer) slot += 0.5;   // 内环错开半格
      var r = (twoRings && i >= outer) ? radius * 0.56 : radius;
      var angle = (slot / Math.max(1, count)) * tau - Math.PI / 2;
      pos[n.id] = { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
    });

    var out = ['<svg viewBox="0 0 ' + w + " " + h + '" class="graph-svg" xmlns="http://www.w3.org/2000/svg">'];
    out.push("<defs>"
      + '<marker id="garrow" viewBox="0 0 10 10" refX="20" refY="5" markerWidth="6" markerHeight="6"'
      + ' orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#9aa6c2"/></marker>'
      + '<radialGradient id="gnfill" cx="35%" cy="30%" r="75%">'
      + '<stop offset="0%" stop-color="#8b96ff"/><stop offset="100%" stop-color="#4b57e0"/></radialGradient>'
      + "</defs>");
    // 辅助环：让“环形布局”看起来是有意为之，而不是散点
    if (!twoRings) {
      out.push('<circle cx="' + cx + '" cy="' + cy + '" r="' + radius.toFixed(1)
        + '" class="gn-ring"/>');
    }
    edges.forEach(function (e) {
      var s = pos[e.source], t = pos[e.target];
      if (!s || !t) return;
      var mx = (s.x + t.x) / 2, my = (s.y + t.y) / 2;
      var dx = t.x - s.x, dy = t.y - s.y, len = Math.sqrt(dx * dx + dy * dy) || 1;
      var bow = Math.min(46, len * 0.16);        // 轻微弯曲，避免多条边重叠成一条直线
      var qx = mx - (dy / len) * bow, qy = my + (dx / len) * bow;
      var mutual = e.direction === "<->";
      out.push('<path d="M ' + s.x + " " + s.y + " Q " + qx + " " + qy + " " + t.x + " " + t.y
        + '" class="ge-line' + (mutual ? " mutual" : "") + '" marker-end="url(#garrow)"><title>'
        + escapeHtml((REL_LABEL[e.relation] || e.relation) + " " + e.strength + "：" + (e.rationale || ""))
        + "</title></path>");
      out.push('<text x="' + qx.toFixed(1) + '" y="' + (qy - 6).toFixed(1)
        + '" class="ge-label" text-anchor="middle">' + escapeHtml(e.relation) + "</text>");
    });
    nodes.forEach(function (n) {
      var p = pos[n.id], primary = n.role === "primary";
      var labelY = (p.y >= cy ? 28 : -20);
      out.push('<g class="gn">'
        + '<circle cx="' + p.x + '" cy="' + p.y + '" r="15" class="gn-halo' + (primary ? " primary" : "") + '"/>'
        + '<circle cx="' + p.x + '" cy="' + p.y + '" r="' + (primary ? 10.5 : 9) + '" class="gn-dot'
        + (primary ? " primary" : "") + '"><title>' + escapeHtml(n.label || "") + "</title></circle>"
        + "</g>");
      out.push('<text x="' + p.x + '" y="' + (p.y + labelY) + '" class="gn-label" text-anchor="middle">'
        + escapeHtml(shortText(n.label || "", 22)) + "</text>");
    });
    out.push("</svg>");
    return out.join("");
  }

  function shortText(text, n) {
    text = String(text || "");
    return text.length > n ? text.slice(0, n - 1) + "…" : text;
  }

  // ---------------- 阅读产物（快速总结 / 深度精读）页签 ----------------
  // 产物由知识网络 pipeline 生成并落在 paper_artifacts 表；这里把“对话 / 快速总结 / 深度精读”
  // 做成对话栏里的三个视图，避免跑完 pipeline 后产物无处可看。
  var ARTIFACT_META = {
    summary: { title: "快速总结", steps: ["summary"] },
    deep_reading: { title: "深度精读", steps: ["deep"] },
  };
  var STEP_TO_ARTIFACT = { summary: "summary", deep: "deep_reading" };
  var artifactView = "chat";        // chat | summary | deep_reading
  var artifactText = "";            // 当前展示的 Markdown 原文（供复制/新窗口）
  var artifactPaperId = null;

  function setArtifactTabs(meta) {
    meta = meta || {};
    document.querySelectorAll(".art-tab").forEach(function (tab) {
      var view = tab.getAttribute("data-view");
      var badge = tab.querySelector(".art-meta");
      tab.classList.toggle("active", view === artifactView);
      if (view === "chat") return;
      var info = meta[view];
      tab.classList.toggle("empty", !info);
      tab.classList.toggle("attn", !!info && !!(info.attn));
      if (badge) badge.textContent = info ? shortChars(info.chars) : "未生成";
    });
  }

  function shortChars(chars) {
    chars = Number(chars) || 0;
    return chars >= 1000 ? (chars / 1000).toFixed(1) + "k字" : chars + "字";
  }

  // 拉取该论文已生成的产物列表（页签角标用），顺带记住哪些是刚生成的（高亮）
  function loadPaperArtifacts(paper) {
    if (!paper) { setArtifactTabs({}); return Promise.resolve({}); }
    artifactPaperId = paper.id;
    return api.get("/api/papers/" + paper.id + "/knowledge").then(function (data) {
      var meta = {};
      ((data && data.artifacts) || []).forEach(function (a) {
        if (!ARTIFACT_META[a.kind]) return;
        meta[a.kind] = { chars: a.chars, updated_at: a.updated_at, model: a.model,
                         reasoning_effort: a.reasoning_effort };
      });
      state.artifacts = meta;
      setArtifactTabs(meta);
      return meta;
    }).catch(function () { setArtifactTabs({}); return {}; });
  }

  function showChatView() {
    artifactView = "chat";
    artifactText = "";
    $("artifact-view").classList.add("hidden");
    $("messages").classList.remove("hidden");
    setArtifactTabs(state.artifacts || {});
  }

  function openArtifact(kind) {
    var paper = state.currentPaper;
    var info = ARTIFACT_META[kind];
    if (!paper || !info) return;
    artifactView = kind;
    $("messages").classList.add("hidden");
    $("artifact-view").classList.remove("hidden");
    setArtifactTabs(state.artifacts || {});
    document.querySelector('.art-tab[data-view="' + kind + '"]').classList.remove("attn");
    if (state.artifacts) state.artifacts[kind] = state.artifacts[kind] || {};
    if (state.artifacts && state.artifacts[kind]) state.artifacts[kind].attn = false;

    var body = $("av-body");
    body.replaceChildren(skeleton("rows", 3));
    $("av-title").textContent = "🧾".replace("🧾", kind === "summary" ? "🧾 " : "📖 ") + info.title;
    $("av-meta").textContent = "加载中…";
    $("artifact-view").setAttribute("data-kind", kind);

    api.get("/api/papers/" + paper.id + "/artifacts/" + kind).then(function (data) {
      artifactText = data.content_md || "";
      var meta = [shortChars(data.chars), (data.updated_at || "").slice(0, 16).replace("T", " "),
        data.model || "", data.reasoning_effort ? ("think:" + data.reasoning_effort) : ""];
      $("av-meta").textContent = meta.filter(Boolean).join(" · ");
      setMd(body, artifactText);
      if (data.reasoning_md) {              // 仅当配置开启落库推理时才有内容
        var block = makeReasoningBlock(body);
        renderReasoning(block, data.reasoning_md, false);
      }
      body.scrollTop = 0;
    }).catch(function (e) {
      artifactText = "";
      $("av-meta").textContent = "尚未生成";
      body.replaceChildren(emptyState("🧠", "还没有" + info.title,
        "点上面的「🧠 知识网络」跑一次即可生成（增量运行不会重跑已完成步骤）。"));
      var tip = el("button", { class: "mini primary av-run", text: "现在生成",
        onclick: function () { runPaperPipeline(paper, false, info.steps); } });
      body.firstChild.appendChild(tip);
      if (String(e.message).indexOf("还没有") < 0) toast(e.message, "err");
    });
  }

  function copyArtifact() {
    if (!artifactText) { toast("没有可复制的内容", "warn"); return; }
    var done = function () { toast("已复制全部 Markdown", "ok"); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(artifactText).then(done, function () { legacyCopy(artifactText, done); });
    } else {
      legacyCopy(artifactText, done);
    }
  }

  function legacyCopy(text, done) {
    var ta = el("textarea", { style: "position:fixed;top:-1000px;opacity:0" });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); }
    catch (e) { toast("复制失败，请手动选中复制", "err"); }
    ta.remove();
  }

  // ---------------- 知识网络 pipeline（对库内任一论文一键运行） ----------------
  // 与「加入详细阅读」是同一套后端流程（summary→tree→deep→graph→synthesis），
  // 区别只是论文已在库里：不需要下载与归属，直接跑五步并实时显示进度。
  var PIPELINE_STEPS = [
    { name: "summary", title: "快速总结" },
    { name: "tree", title: "方向树归属" },
    { name: "deep", title: "深度精读" },
    { name: "graph", title: "局部图谱" },
    { name: "synthesis", title: "阶段综述" },
  ];
  var PIPELINE_STATE = { running: "运行中…", done: "已完成", failed: "失败", cancelled: "已终止" };
  var pipelineRunning = false;
  var pipelinePaperId = null;
  var pipelineSteps = null;      // 步骤名与标题由后端 /api/knowledge/steps 提供（本地列表仅兜底）

  function loadPipelineSteps() {
    if (pipelineSteps) return Promise.resolve(pipelineSteps);
    return api.get("/api/knowledge/steps").then(function (data) {
      var byName = {};
      (data.steps || []).forEach(function (s) { byName[s.name] = s.title; });
      pipelineSteps = PIPELINE_STEPS.map(function (s) {
        return { name: s.name, title: byName[s.name] || s.title };
      });
      return pipelineSteps;
    }).catch(function () {
      pipelineSteps = PIPELINE_STEPS.slice();
      return pipelineSteps;
    });
  }

  function renderPipelinePanel(paper, status, steps, subtitle) {
    var box = $("pipeline-panel");
    box.replaceChildren();
    box.appendChild(el("div", { class: "pp-head" }, [
      el("span", { class: "pp-title",
        text: "知识网络 · " + shortText(paper.title || paper.arxiv_id, 24)
          + (subtitle ? "（" + subtitle + "）" : "") }),
      el("span", { class: "pp-state" + (status ? " " + status : ""), text: PIPELINE_STATE[status] || "" }),
      status === "running"
        ? el("button", { class: "mini pp-stop danger", title: "终止本次知识网络（当前步会被标记为已终止）",
            text: "■ 终止", onclick: function () { stopPipeline(); } })
        : null,
      el("button", { class: "mini pp-close", title: "收起面板", text: "✕",
        onclick: function () { if (!pipelineRunning) $("pipeline-panel").classList.add("hidden"); } }),
    ]));
    var list = el("div", { class: "pp-steps" });
    (steps || PIPELINE_STEPS).forEach(function (s) {
      list.appendChild(el("div", { class: "pp-step todo", "data-step": s.name }, [
        el("span", { class: "pp-dot", text: "○" }),
        el("span", { class: "pp-name", text: s.title }),
        el("span", { class: "pp-detail", text: "" }),
      ]));
    });
    box.appendChild(list);
    box.classList.remove("hidden");
  }

  var PP_MARK = { done: "✓", running: "◐", skipped: "–", failed: "■", todo: "○" };

  function setPipelineState(status) {
    var chip = $("pipeline-panel").querySelector(".pp-state");
    if (chip) { chip.className = "pp-state " + (status || ""); chip.textContent = PIPELINE_STATE[status] || ""; }
    var stop = $("pipeline-panel").querySelector(".pp-stop");
    if (stop) stop.remove();      // 已结束/已终止时不再提供终止按钮
  }

  /** 终止当前知识网络：先告诉服务端，再把前端连接断开（见 stopActiveStream） */
  function stopPipeline() {
    if (!pipelineRunning) return;
    if (!stopActiveStream()) return;
    pipelineRunning = false;
    setPipelineState("cancelled");
    var panel = $("pipeline-panel");
    var runningRow = panel.querySelector(".pp-step.running");
    if (runningRow) {
      runningRow.className = "pp-step failed";
      runningRow.querySelector(".pp-dot").textContent = "■";
      runningRow.querySelector(".pp-detail").textContent = "已终止";
    }
    panel.querySelectorAll(".pp-step.todo").forEach(function (row) {
      row.className = "pp-step skipped";
      row.querySelector(".pp-dot").textContent = "–";
      row.querySelector(".pp-detail").textContent = "已终止";
    });
    toast("已终止知识网络：已完成的步骤与产物保留，下次运行会从断点继续", "warn", 6000);
  }

  function updatePipelineStep(step, status, detail, extra) {
    var row = $("pipeline-panel").querySelector('.pp-step[data-step="' + step + '"]');
    if (!row) return;
    row.className = "pp-step " + status;
    row.querySelector(".pp-dot").textContent = PP_MARK[status] || "○";
    var text = detail || "";
    if (extra && extra.elapsed_s !== undefined && extra.elapsed_s !== null) {
      text += (text ? "　" : "") + Math.round(extra.elapsed_s) + "s";
    }
    if (extra && extra.usage && extra.usage.total_tokens) {
      text += (text ? " · " : "") + extra.usage.total_tokens + " tokens";
    }
    row.querySelector(".pp-detail").textContent = text;
  }

  function openPipelineDialog(paper) {
    paper = paper || state.currentPaper;
    if (!paper) { toast("请先选择一篇论文", "err"); return; }
    loadPipelineSteps().then(function (steps) {
      return askChoice("运行知识网络", [
        "对《" + shortText(paper.title || paper.arxiv_id, 44) + "》依次执行："
          + steps.map(function (s) { return s.title; }).join(" → ") + "。",
        "已完成的步骤会自动跳过（省钱）；换了模型或 prompt 时可选择强制重跑。",
        "进度显示在右侧对话区顶部，任务在后台运行，中途切换论文或关闭窗口都不会中断。",
      ], [
        { text: "增量运行", value: "run" },
        { text: "强制重跑", value: "force" },
      ]);
    }).then(function (choice) {
      if (!choice) return;
      runPaperPipeline(paper, choice === "force");
    });
  }

  function runPaperPipeline(paper, force, onlySteps) {
    if (pipelineRunning) { toast("已有一篇论文的知识网络在运行中，请等它结束", "warn"); return; }
    var allSteps = pipelineSteps || PIPELINE_STEPS;
    var subset = (onlySteps && onlySteps.length)
      ? allSteps.filter(function (s) { return onlySteps.indexOf(s.name) >= 0; })
      : allSteps;
    var subtitle = (onlySteps && onlySteps.length && ARTIFACT_META[STEP_TO_ARTIFACT[onlySteps[0]]])
      ? "重新生成 " + ARTIFACT_META[STEP_TO_ARTIFACT[onlySteps[0]]].title : "";
    pipelineRunning = true;
    pipelinePaperId = paper.id;
    renderPipelinePanel(paper, "running", subset, subtitle);
    var refresh = function () {
      loadFolders(); loadPapers();
      if (currentGraphNodeId) loadNodeGraph(currentGraphNodeId);
      if (state.currentPaper && state.currentPaper.id === pipelinePaperId) updateCtxStatus(state.currentPaper);
    };
    streamSSE("/api/papers/" + paper.id + "/knowledge/run?stream=true",
              { steps: (onlySteps && onlySteps.length) ? onlySteps : null, force: !!force }, {
      kind: "pipeline",
      onAborted: function () {
        // 用户点了「终止」：状态与步骤文案已由 stopPipeline 就地更新
        pipelineRunning = false;
      },
      onDelta: function () { /* 本通道不发 delta */ },
      onStep: function (ev) { updatePipelineStep(ev.step, ev.status, ev.detail, ev); },
      onError: function (msg) {
        pipelineRunning = false;
        setPipelineState("failed");
        toast("知识网络失败：" + msg, "err", 8000);
      },
      onDone: function (payload) {
        pipelineRunning = false;
        var records = (payload && payload.steps) || [];
        var ok = (payload && payload.status) === "done";
        var cancelled = (payload && (payload.status === "cancelled" || payload.cancelled));
        var done = records.filter(function (s) { return s.status === "done"; }).length;
        if (cancelled) {
          pipelineRunning = false;
          setPipelineState("cancelled");
          var panel = $("pipeline-panel");
          panel.querySelectorAll(".pp-step.todo").forEach(function (row) {
            row.className = "pp-step skipped";
            row.querySelector(".pp-dot").textContent = "–";
            row.querySelector(".pp-detail").textContent = "已终止";
          });
          toast("知识网络已终止：已完成 " + done + " 步，产物保留", "warn", 6000);
          refresh();
          loadPaperArtifacts(paper);
          return;
        }
        setPipelineState(ok ? "done" : "failed");
        refresh();
        // 产物刷新后直接打开，避免“跑完了但找不到输出”
        loadPaperArtifacts(paper).then(function (meta) {
          meta = meta || {};
          ["summary", "deep_reading"].forEach(function (kind) {
            var step = ARTIFACT_META[kind].steps[0];
            if (meta[kind]) meta[kind].attn = records.some(function (s) {
              return s.step === step && s.status === "done";
            });
          });
          setArtifactTabs(meta);
          var target = (onlySteps && onlySteps.length) ? STEP_TO_ARTIFACT[onlySteps[0]]
            : (meta.summary && meta.summary.attn ? "summary"
              : (meta.deep_reading && meta.deep_reading.attn ? "deep_reading" : null));
          if (target && meta[target] && state.currentPaper && state.currentPaper.id === paper.id) {
            openArtifact(target);
            toast("已生成「" + ARTIFACT_META[target].title + "」（" + done + " 步完成），上方页签可切换对话与两份产物",
                  "ok", 7000);
          } else if (ok) {
            toast("知识网络完成：" + done + " 步已执行，已沉淀进方向树与图谱", "ok", 5000);
          } else {
            toast("知识网络未完成（看面板里的失败步骤）", "warn", 8000);
          }
        });
      },
    });
  }

  // ---------------- 论文列表 ----------------
  function loadPapers() {
    var qs = [];
    if (state.selectedFolderId !== null) qs.push("folder_id=" + state.selectedFolderId);
    if (state.search) qs.push("q=" + encodeURIComponent(state.search));
    var url = "/api/papers" + (qs.length ? "?" + qs.join("&") : "");
    state.papersLoading = true; renderPapers();
    return api.get(url).then(function (list) {
      state.papers = list; state.papersLoading = false; renderPapers();
      $("all-count").textContent = state.selectedFolderId === null ? list.length : $("all-count").textContent;
    }).catch(function (e) { state.papersLoading = false; renderPapers(); toast(e.message, "err"); });
  }
  function renderPapers() {
    var box = $("paper-list");
    if (state.papersLoading) { box.replaceChildren(skeleton("cards", 4)); return; }
    box.replaceChildren();
    if (!state.papers.length) {
      box.appendChild(state.search
        ? emptyState("🔎", "没有符合条件的论文", "换个关键词试试，或清空搜索框。")
        : emptyState("📄", "这里还没有论文",
            "把 ArXiv 链接或 ID 粘贴到顶部「添加」，或点「🔎 ArXiv 检索」批量筛出值得读的论文。"));
      return;
    }
    state.papers.forEach(function (p) {
      var badges = el("div", { class: "p-meta" });
      (p.categories || []).slice(0, 3).forEach(function (c) { badges.appendChild(el("span", { class: "badge", text: c })); });
      if (p.folder_name) badges.appendChild(el("span", {
        class: "badge folder" + (p.folder_is_direction ? " direction" : ""),
        text: (p.folder_is_direction ? "🧭 " : "📁 ") + p.folder_name,
      }));
      if (p.text_truncated) badges.appendChild(el("span", { class: "badge trunc", text: "已截断" }));
      if (!p.has_text) badges.appendChild(el("span", { class: "badge notext", text: "⚠ 无正文" }));
      var ops = el("div", { class: "p-ops" }, [
        el("button", { title: "跑知识网络 pipeline", text: "🧠",
          onclick: function (e) { e.stopPropagation(); openPipelineDialog(p); } }),
        el("button", { title: "移动到文件夹", text: "📁", onclick: function (e) { e.stopPropagation(); openMoveModal(p); } }),
        el("button", { class: "del", title: "删除论文", text: "🗑", onclick: function (e) { e.stopPropagation(); deletePaper(p); } }),
      ]);
      var item = el("div", {
        class: "paper-item" + (state.currentPaper && state.currentPaper.id === p.id ? " active" : ""),
        "data-id": p.id,
      }, [
        ops,
        el("div", { class: "p-title", text: p.title || p.arxiv_id }),
        el("div", { class: "p-authors", text: (p.authors || []).join(", ") || "—" }),
        badges,
      ]);
      item.addEventListener("click", function () { selectPaper(p); });
      box.appendChild(item);
    });
  }
  function selectPaper(p) {
    hideNodeGraph();
    state.currentPaper = p; renderPapers();
    $("viewer-empty").classList.add("hidden");
    var frame = $("pdf-frame"); frame.classList.remove("hidden");
    frame.src = "/api/papers/" + p.id + "/pdf";
    $("viewer-toolbar").classList.remove("hidden");
    $("viewer-title").textContent = p.title || p.arxiv_id;
    $("chat-title").textContent = p.title || p.arxiv_id;
    updateCtxStatus(p);
    loadSessions();
    // 阅读产物页签：换论文就重新拉一次，已生成的直接可看
    loadPaperArtifacts(p).then(function (meta) {
      if (artifactView !== "chat" && meta && meta[artifactView]) openArtifact(artifactView);
      else if (artifactView !== "chat") showChatView();
    });
  }

  // 上下文状态：明确告知用户“全文/仅摘要/已截断”，避免默默降级
  function updateCtxStatus(p) {
    var box = $("ctx-status");
    if (!p) { box.className = "ctx-status hidden"; box.textContent = ""; return; }
    box.classList.remove("hidden");
    box.replaceChildren();
    if (p.has_text) {
      box.className = "ctx-status ok";
      var n = (p.text_chars || 0).toLocaleString();
      box.appendChild(el("span", { text: "✓ 已将全文 " + n + " 字符加入对话上下文" }));
      if (p.text_truncated) box.appendChild(el("span", { text: "（原文过长，已截断至上限）" }));
    } else {
      box.className = "ctx-status warn";
      box.appendChild(el("span", { text: "⚠ 未抽取到正文，当前仅标题/摘要入上下文。可点“↻ 重抽全文”修复。" }));
    }
  }

  // ---------------- 添加论文 ----------------
  function addPaper() {
    var url = $("new-url").value.trim();
    if (!url) { toast("请输入 ArXiv 链接或 ID", "err"); return; }
    var btn = $("add-btn"); btn.disabled = true; btn.textContent = "入库中…";
    var folderVal = $("target-folder").value;
    var body = { url: url };
    if (folderVal) body.folder_id = parseInt(folderVal, 10);
    api.post("/api/papers/from-arxiv", body).then(function (paper) {
      toast("已入库：" + (paper.title || paper.arxiv_id), "ok");
      $("new-url").value = "";
      return Promise.all([loadFolders(), loadPapers()]).then(function () { selectPaper(paper); });
    }).catch(function (e) { toast("入库失败：" + e.message, "err"); })
      .then(function () { btn.disabled = false; btn.textContent = "添加"; });
  }

  // ---------------- 论文操作：移动 / 删除 / 重抽全文 ----------------
  var movingPaper = null;
  function openMoveModal(p) {
    movingPaper = p;
    renderFolderSelects();
    $("move-paper-title").textContent = p.title || p.arxiv_id;
    var sel = $("move-folder");
    sel.value = p.folder_id != null ? String(p.folder_id) : "";
    $("move-modal").classList.remove("hidden");
  }
  function confirmMove() {
    if (!movingPaper) return;
    var val = $("move-folder").value;
    if (!val) { toast("请选择目标文件夹", "err"); return; }
    var fid = parseInt(val, 10);
    api.patch("/api/papers/" + movingPaper.id, { folder_id: fid }).then(function (updated) {
      $("move-modal").classList.add("hidden");
      toast("已移动到：" + (updated.folder_name || ""), "ok");
      if (state.currentPaper && state.currentPaper.id === updated.id) state.currentPaper = updated;
      movingPaper = null;
      return Promise.all([loadFolders(), loadPapers()]);
    }).catch(function (e) { toast("移动失败：" + e.message, "err"); });
  }
  function deletePaper(p) {
    askConfirm("从库中删除", [
      "《" + (p.title || p.arxiv_id) + "》",
      "将同时删除本地 PDF 与其对话记录（不可恢复）。",
    ], { okText: "删除", danger: true }).then(function (ok) {
      if (!ok) return null;
      return api.del("/api/papers/" + p.id).then(function () {
        toast("已删除", "ok");
        if (state.currentPaper && state.currentPaper.id === p.id) clearCurrentPaper();
        return Promise.all([loadFolders(), loadPapers()]);
      }).catch(function (e) { toast("删除失败：" + e.message, "err"); });
    });
  }
  function clearCurrentPaper() {
    state.currentPaper = null; state.sessions = []; state.currentSessionId = null;
    state.artifacts = {};
    $("pdf-frame").classList.add("hidden"); $("pdf-frame").removeAttribute("src");
    $("viewer-empty").classList.remove("hidden");
    $("viewer-toolbar").classList.add("hidden");
    $("chat-title").textContent = "未选择论文";
    $("session-select").replaceChildren();
    $("pipeline-panel").classList.add("hidden");
    showChatView();
    updateCtxStatus(null);
    renderMessages([]);
  }
  function reextractPaper() {
    var p = state.currentPaper; if (!p) { toast("请先选择一篇论文", "err"); return; }
    var btn = $("reextract-paper"); btn.disabled = true; var old = btn.textContent; btn.textContent = "重抽中…";
    api.post("/api/papers/" + p.id + "/reextract").then(function (updated) {
      state.currentPaper = updated;
      updateCtxStatus(updated);
      loadPapers();
      toast(updated.has_text ? ("全文已重抽（" + (updated.text_chars || 0).toLocaleString() + " 字符）") : "重抽完成", "ok");
    }).catch(function (e) { toast("重抽失败：" + e.message, "err"); })
      .then(function () { btn.disabled = false; btn.textContent = old; });
  }

  // ---------------- 对话 ----------------
  function loadSessions() {
    var p = state.currentPaper; if (!p) return;
    api.get("/api/papers/" + p.id + "/sessions").then(function (list) {
      state.sessions = list;
      var sel = $("session-select"); sel.innerHTML = "";
      list.forEach(function (s) { sel.appendChild(el("option", { value: String(s.id), text: s.title || ("会话 " + s.id) })); });
      if (list.length) { state.currentSessionId = list[0].id; sel.value = String(list[0].id); loadMessages(); }
      else { state.currentSessionId = null; renderMessages([]); }
    }).catch(function (e) { toast(e.message, "err"); });
  }
  function newSession() {
    var p = state.currentPaper; if (!p) { toast("请先选择一篇论文", "err"); return; }
    api.post("/api/papers/" + p.id + "/sessions").then(function (s) {
      return loadSessions().then(function () { state.currentSessionId = s.id; $("session-select").value = String(s.id); renderMessages([]); });
    }).catch(function (e) { toast(e.message, "err"); });
  }
  function loadMessages() {
    if (!state.currentSessionId) { renderMessages([]); return; }
    api.get("/api/sessions/" + state.currentSessionId + "/messages")
      .then(renderMessages).catch(function (e) { toast(e.message, "err"); });
  }
  function renderMessages(msgs) {
    var box = $("messages"); box.replaceChildren();
    if (!msgs || !msgs.length) {
      box.appendChild(emptyState("💬", "开始提问吧",
        "例如：这篇论文解决了什么问题？核心方法是什么？和已有工作相比有什么不同？"));
      return;
    }
    msgs.forEach(function (m) {
      if (m.role === "system") return;
      var node = el("div", { class: "msg " + m.role });
      setMd(node, m.content);
      if (m.reasoning_content) {
        var block = makeReasoningBlock(node);   // 历史消息默认折叠
        renderReasoning(block, m.reasoning_content, false);
      }
      box.appendChild(node);
    });
    box.scrollTop = box.scrollHeight;
  }
  function sendMessage() {
    if (state.streaming) return;
    var input = $("chat-input");
    var content = input.value.trim();
    if (!content) return;
    if (!state.currentPaper) { toast("请先选择一篇论文", "err"); return; }
    if (artifactView !== "chat") showChatView();   // 在产物阅读视图里提问时，自动回到对话

    var start = function (sid) {
      state.currentSessionId = sid;
      input.value = "";
      var box = $("messages");
      if (box.querySelector(".empty")) box.replaceChildren();
      var userNode = el("div", { class: "msg user" }); setMd(userNode, content);
      box.appendChild(userNode);
      var ai = el("div", { class: "msg assistant pending" });
      var answer = el("div", { class: "answer" });   // 正文独立节点：推理块要留在同一个气泡里
      ai.appendChild(answer);
      box.appendChild(ai); box.scrollTop = box.scrollHeight;

      var raw = "", rawReasoning = "", reasonBlock = null;
      state.streaming = true; setSendButton(true);
      var finalize = function (aborted) {
        ai.classList.remove("pending");
        if (raw) setMd(answer, aborted ? (raw + "\n\n> ⏹ 已终止") : raw);
        if (reasonBlock) { renderReasoning(reasonBlock, rawReasoning, false); reasonBlock.wrap.open = false; }
        state.streaming = false; setSendButton(false);
        loadSessions();
      };
      streamChat(sid, content, {
        kind: "chat",
        onDelta: function (delta, reasoning) {
          if (reasoning) {
            if (!reasonBlock) { reasonBlock = makeReasoningBlock(ai); reasonBlock.wrap.open = true; }
            rawReasoning += reasoning;
            renderReasoning(reasonBlock, rawReasoning, true);
          }
          if (delta) { raw += delta; setMd(answer, raw); }
          box.scrollTop = box.scrollHeight;
        },
        onDone: function () { finalize(false); },
        onCancelled: function () { finalize(true); toast("已终止本次生成，已生成的内容已保留", "warn", 5000); },
        onAborted: function () { finalize(true); toast("已终止本次生成，已生成的内容已保留", "warn", 5000); },
        onError: function (errMsg) {
          ai.classList.remove("pending");
          if (!raw && !rawReasoning) ai.remove();
          box.appendChild(el("div", { class: "msg error", text: "出错：" + errMsg }));
          state.streaming = false; setSendButton(false);
        },
      });
    };

    if (state.currentSessionId) start(state.currentSessionId);
    else api.post("/api/papers/" + state.currentPaper.id + "/sessions").then(function (s) {
      state.sessions.unshift(s);
      var sel = $("session-select");
      sel.appendChild(el("option", { value: String(s.id), text: s.title }));
      sel.value = String(s.id);
      start(s.id);
    }).catch(function (e) { toast(e.message, "err"); });
  }

  // ---------------- 流式请求的中止（「终止」按钮） ----------------
  // 只 abort 连接是拦不住服务端的：SSE 生成器跑在服务端线程里，会继续把模型输出读完。
  // 所以统一先调 POST /api/cancel/{token} 置位服务端标记，再 abort 掉前端连接。
  var activeStream = null;      // {token, controller, kind, onAborted}

  function newToken() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "");
    return "t" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }

  function stopActiveStream() {
    if (!activeStream) return false;
    var cur = activeStream;
    activeStream = null;
    api.post("/api/cancel/" + encodeURIComponent(cur.token), {})
      .then(function (res) {
        if (!res || res.stopped === false) toast("任务已结束，无需终止", "info");
      })
      .catch(function () { /* 取消失败也要把前端连接断掉 */ })
      .then(function () { if (cur.controller) cur.controller.abort(); });
    if (cur.onAborted) cur.onAborted();
    return true;
  }

  // 以 fetch + ReadableStream 消费后端 SSE（POST，无法用 EventSource）
  // frame 协议：delta{text,reasoning} / tool{name,ok} / step{...} / cancelled / error{message} / done{...}
  function streamSSE(url, body, handlers) {
    var token = newToken();
    var controller = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var payload = body || {};
    if (payload.cancel_token === undefined) payload.cancel_token = token;
    activeStream = { token: token, controller: controller, kind: (handlers && handlers.kind) || "stream",
                     onAborted: handlers && handlers.onAborted };
    var finish = function () { if (activeStream && activeStream.token === token) activeStream = null; };
    fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), signal: controller ? controller.signal : undefined,
    }).then(function (resp) {
      if (!resp.ok || !resp.body) return resp.text().then(function (t) { finish(); handlers.onError("HTTP " + resp.status + " " + t); });
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = "";
      var finished = false;
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { finish(); if (!finished) handlers.onDone({}); return; }
          buf += decoder.decode(res.value, { stream: true });
          var idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            var frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
            var line = frame.split("\n").filter(function (l) { return l.indexOf("data:") === 0; })[0];
            if (!line) continue;
            var payloadObj;
            try { payloadObj = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
            if (payloadObj.type === "delta") handlers.onDelta(payloadObj.text || "", payloadObj.reasoning || "");
            else if (payloadObj.type === "tool") { if (handlers.onTool) handlers.onTool(payloadObj); }
            else if (payloadObj.type === "step") { if (handlers.onStep) handlers.onStep(payloadObj); }
            else if (payloadObj.type === "cancelled") {
              finished = true; finish();
              if (handlers.onCancelled) handlers.onCancelled(payloadObj);
              else handlers.onDone({ status: "cancelled", cancelled: true });
              return;
            }
            else if (payloadObj.type === "error") { finished = true; finish(); handlers.onError(payloadObj.message || "未知错误"); return; }
            else if (payloadObj.type === "done") { finished = true; finish(); handlers.onDone(payloadObj); return; }
          }
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      finish();
      // abort 是主动行为，交给 onAborted 处理，不当成错误
      if (e && (e.name === "AbortError" || String(e.message || "").indexOf("aborted") >= 0)) return;
      handlers.onError(e.message || String(e));
    });
  }

  function streamChat(sessionId, content, handlers) {
    return streamSSE("/api/sessions/" + sessionId + "/messages", { content: content }, handlers);
  }

  // 发送按钮在流式中变为「停止」（终止本次生成，已生成内容保留）
  function setSendButton(streaming) {
    var btn = $("send-btn");
    btn.classList.toggle("danger", !!streaming);
    btn.textContent = streaming ? "■ 停止" : "发送";
    btn.disabled = false;
    btn.title = streaming ? "终止本次生成（已生成的内容会保留并落库）" : "";
  }

  function stopChat() {
    if (!state.streaming) return;
    if (!stopActiveStream()) return;
    state.streaming = false;
    setSendButton(false);
  }

  // 技能流式入口（供控制台/后续 UI 复用同一套推理渲染）：
  //   __arxivreader_stream_skill("quick_summary")
  function streamSkillIntoChat(skillName) {
    var paper = state.currentPaper;
    if (!paper) { toast("请先选择一篇论文", "err"); return Promise.resolve(); }
    if (artifactView !== "chat") showChatView();
    var box = $("messages");
    if (box.querySelector(".empty")) box.replaceChildren();
    var asks = el("div", { class: "msg user" }); setMd(asks, "运行技能：" + skillName);
    box.appendChild(asks);
    var ai = el("div", { class: "msg assistant pending" });
    var answer = el("div", { class: "answer" }); ai.appendChild(answer);
    box.appendChild(ai); box.scrollTop = box.scrollHeight;
    var raw = "", rawReasoning = "", block = null;
    state.streaming = true; $("send-btn").disabled = true;
    return streamSSE("/api/papers/" + paper.id + "/skills/" + skillName + "?stream=true", {},
      {
        onDelta: function (delta, reasoning) {
          if (reasoning) {
            if (!block) { block = makeReasoningBlock(ai); block.wrap.open = true; }
            rawReasoning += reasoning;
            renderReasoning(block, rawReasoning, true);
          }
          if (delta) { raw += delta; setMd(answer, raw); }
          box.scrollTop = box.scrollHeight;
        },
        onDone: function (payload) {
          ai.classList.remove("pending");
          if (!raw && payload && payload.output) setMd(answer, payload.output);
          if (block) { renderReasoning(block, rawReasoning, false); block.wrap.open = false; }
          state.streaming = false; $("send-btn").disabled = false;
          toast("技能完成：" + skillName, "info");
        },
        onError: function (msg) {
          ai.classList.remove("pending");
          box.appendChild(el("div", { class: "msg error", text: "技能出错：" + msg }));
          state.streaming = false; $("send-btn").disabled = false;
        },
      });
  }
  window.__arxivreader_stream_skill = streamSkillIntoChat;

  // ---------------- 设置 ----------------
  // 首次使用最大的坑是“能打开界面但所有 AI 功能都不工作”：未配置时把「设置」提请用户注意。
  function markSettingsAttention(on) {
    var btn = $("open-settings");
    if (!btn) return;
    btn.classList.toggle("attn", !!on);
    btn.textContent = on ? "⚠ 设置" : "设置";
    btn.title = on ? "尚未配置模型：先在这里填 Base URL 与 API Key" : "LLM 端点与采样参数";
  }

  function checkLLMConfigured(quiet) {
    return api.get("/api/settings").then(function (s) {
      var ok = !!(s.base_url && s.has_api_key);
      markSettingsAttention(!ok);
      if (!ok && !quiet) {
        toast("尚未配置模型：打开右上角「设置」填 Base URL 与 API Key 后，总结 / 检索 / 问答才能工作", "warn", 8000);
      }
      return ok;
    }).catch(function () { return true; });
  }

  function openSettings() {
    api.get("/api/settings").then(function (s) {
      $("set-base-url").value = s.base_url || "";
      $("set-model").value = s.model || "";
      $("set-temperature").value = s.temperature;
      $("set-max-tokens").value = s.max_tokens;
      $("set-top-p").value = s.top_p;
      $("set-api-key").value = "";
      $("key-hint").textContent = s.has_api_key ? ("当前已保存 Key：" + s.api_key_preview) : "尚未配置 API Key";
      $("settings-test-result").textContent = "";
      $("settings-modal").classList.remove("hidden");
    }).catch(function (e) { toast(e.message, "err"); });
  }
  function collectSettings() {
    var body = {
      base_url: $("set-base-url").value.trim(),
      model: $("set-model").value.trim(),
      temperature: parseFloat($("set-temperature").value),
      max_tokens: parseInt($("set-max-tokens").value, 10),
      top_p: parseFloat($("set-top-p").value),
    };
    var key = $("set-api-key").value.trim();
    if (key) body.api_key = key;   // 留空则不修改
    return body;
  }
  function saveSettings(silent) {
    return api.put("/api/settings", collectSettings()).then(function (s) {
      $("key-hint").textContent = s.has_api_key ? ("当前已保存 Key：" + s.api_key_preview) : "尚未配置 API Key";
      $("set-api-key").value = "";
      markSettingsAttention(!(s.base_url && s.has_api_key));
      if (!silent) toast("设置已保存", "ok");
      return s;
    }).catch(function (e) { toast(e.message, "err"); });
  }
  function testLLM() {
    var r = $("settings-test-result"); r.className = "hint"; r.textContent = "测试中…";
    saveSettings(true).then(function () { return api.post("/api/settings/test"); }).then(function (res) {
      r.className = "hint " + (res.ok ? "ok" : "err");
      r.textContent = (res.ok ? "✓ " : "✗ ") + res.detail;
    }).catch(function (e) { r.className = "hint err"; r.textContent = "✗ " + e.message; });
  }

  // ---------------- 规则 ----------------
  function openRules() { renderFolderSelects(); loadRules(); $("rules-modal").classList.remove("hidden"); }
  function loadRules() {
    api.get("/api/rules").then(function (rules) {
      var box = $("rule-list"); box.replaceChildren();
      if (!rules.length) { box.appendChild(el("div", { class: "empty", text: "还没有规则。未命中规则的论文会进入 Inbox。" })); return; }
      rules.forEach(function (r) {
        var fname = r.folder_name || "（未设置）";
        var item = el("div", { class: "rule-item" + (r.enabled ? "" : " disabled") }, [
          el("div", { class: "r-main" }, [
            el("span", { class: "r-type", text: r.match_type }),
            el("span", { class: "r-pat", text: " " + r.pattern }),
            el("span", { class: "r-folder", text: "  → " + fname + " · p" + r.priority + (r.name ? (" · " + r.name) : "") }),
          ]),
          el("div", { class: "r-ops" }, [
            el("button", { class: "mini", text: r.enabled ? "停用" : "启用", onclick: function () { toggleRule(r); } }),
            el("button", { class: "mini", text: "删除", onclick: function () { deleteRule(r); } }),
          ]),
        ]);
        box.appendChild(item);
      });
    }).catch(function (e) { toast(e.message, "err"); });
  }
  function addRule() {
    var pattern = $("rule-pattern").value.trim();
    if (!pattern) { toast("请填写匹配值", "err"); return; }
    var folderVal = $("rule-folder").value;
    var body = {
      name: $("rule-name").value.trim(),
      match_type: $("rule-type").value,
      pattern: pattern,
      folder_id: folderVal ? parseInt(folderVal, 10) : null,
      priority: parseInt($("rule-priority").value, 10) || 100,
      enabled: true,
    };
    api.post("/api/rules", body).then(function () {
      $("rule-pattern").value = ""; $("rule-name").value = "";
      loadRules(); toast("规则已添加", "ok");
    }).catch(function (e) { toast(e.message, "err"); });
  }
  function toggleRule(r) { api.patch("/api/rules/" + r.id, { enabled: !r.enabled }).then(loadRules).catch(function (e) { toast(e.message, "err"); }); }
  function deleteRule(r) {
    askConfirm("删除规则", ["删除「" + (r.name || (r.match_type + " " + r.pattern)) + "」？"],
      { okText: "删除", danger: true }).then(function (ok) {
      if (!ok) return null;
      return api.del("/api/rules/" + r.id).then(loadRules).catch(function (e) { toast(e.message, "err"); });
    });
  }

  // ---------------- 布局：拖拽伸缩 + 折叠隐藏 ----------------
  var PANES = ["sidebar", "list", "viewer", "chat"];
  var RESIZER_OF = { sidebar: "rz-sidebar", list: "rz-list", chat: "rz-chat" };
  var WIDTH_VAR = { sidebar: "--sidebar-w", list: "--list-w", chat: "--chat-w" };

  function paneEl(name) { return document.querySelector('.pane[data-pane="' + name + '"]'); }

  function applyWidths() {
    var L = loadLayout();
    ["sidebar", "list", "chat"].forEach(function (name) {
      if (L["w_" + name]) document.documentElement.style.setProperty(WIDTH_VAR[name], L["w_" + name] + "px");
    });
  }
  function applyVisibility() {
    var L = loadLayout();
    PANES.forEach(function (name) {
      var hidden = !!L["hide_" + name];
      var pe = paneEl(name); if (pe) pe.classList.toggle("collapsed", hidden);
      var rzName = RESIZER_OF[name];
      if (rzName) { var rz = $(rzName); if (rz) rz.classList.toggle("collapsed", hidden); }
    });
    var center = $("center");
    if (center) center.classList.toggle("list-grow", !!L["hide_viewer"]);  // 预览隐藏时列表充满
    document.querySelectorAll(".layout-toggles .lt").forEach(function (b) {
      b.classList.toggle("active", !L["hide_" + b.getAttribute("data-pane")]);
    });
  }
  function togglePane(name) {
    var L = loadLayout();
    L["hide_" + name] = !L["hide_" + name];
    saveLayout(L);
    applyVisibility();
  }
  function startResize(e, rz) {
    var name = rz.getAttribute("data-for");
    var varName = WIDTH_VAR[name]; if (!varName) return;
    var pe = paneEl(name); if (!pe) return;
    var startX = e.clientX, startW = pe.getBoundingClientRect().width;
    var dir = (name === "chat") ? -1 : 1;   // chat 在右侧，向左拖增大宽度
    document.body.classList.add("resizing"); rz.classList.add("dragging");
    function onMove(ev) {
      var w = Math.round(startW + dir * (ev.clientX - startX));
      w = Math.max(160, Math.min(w, window.innerWidth - 220));
      document.documentElement.style.setProperty(varName, w + "px");
    }
    function onUp() {
      document.body.classList.remove("resizing"); rz.classList.remove("dragging");
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      var patch = {}; patch["w_" + name] = Math.round(pe.getBoundingClientRect().width); saveLayout(patch);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    e.preventDefault();
  }

  // ---------------- 事件绑定 ----------------
  function bind() {
    $("add-btn").addEventListener("click", addPaper);
    $("new-url").addEventListener("keyup", function (e) { if (e.key === "Enter") addPaper(); });
    $("folder-all").addEventListener("click", function () { selectFolder(null); });
    $("new-folder").addEventListener("click", function () {
      askText("新建顶层文件夹", "", "文件夹名").then(function (name) {
        if (!name) return null;
        return api.post("/api/folders", { name: name, parent_id: null }).then(loadFolders)
          .catch(function (e) { toast(e.message, "err"); });
      });
    });

    var searchTimer = null;
    $("search").addEventListener("input", function () {
      state.search = $("search").value.trim();
      clearTimeout(searchTimer); searchTimer = setTimeout(loadPapers, 300);
    });

    $("send-btn").addEventListener("click", function () {
      if (state.streaming) stopChat();
      else sendMessage();
    });
    $("chat-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
      if (e.key === "Escape" && state.streaming) { e.preventDefault(); stopChat(); }
    });
    $("new-session").addEventListener("click", newSession);
    $("session-select").addEventListener("change", function () {
      state.currentSessionId = parseInt($("session-select").value, 10) || null; loadMessages();
    });

    $("open-settings").addEventListener("click", openSettings);
    $("save-settings").addEventListener("click", function () { saveSettings(false); });
    $("test-llm").addEventListener("click", testLLM);
    $("open-rules").addEventListener("click", openRules);
    $("add-rule").addEventListener("click", addRule);

    // 论文操作（预览工具栏）
    $("run-pipeline").addEventListener("click", function () { openPipelineDialog(state.currentPaper); });
    $("move-paper").addEventListener("click", function () { if (state.currentPaper) openMoveModal(state.currentPaper); });
    $("delete-paper").addEventListener("click", function () { if (state.currentPaper) deletePaper(state.currentPaper); });
    $("reextract-paper").addEventListener("click", reextractPaper);
    $("move-confirm").addEventListener("click", confirmMove);

    // 布局：顶栏切换按钮 + 各面板隐藏按钮 + 拖拽分隔条
    document.querySelectorAll(".layout-toggles .lt").forEach(function (b) {
      b.addEventListener("click", function () { togglePane(b.getAttribute("data-pane")); });
    });
    document.querySelectorAll(".pane-hide").forEach(function (b) {
      b.addEventListener("click", function (e) { e.stopPropagation(); togglePane(b.getAttribute("data-hide")); });
    });
    document.querySelectorAll(".resizer").forEach(function (rz) {
      rz.addEventListener("pointerdown", function (e) { startResize(e, rz); });
    });

    document.querySelectorAll("[data-close]").forEach(function (b) {
      b.addEventListener("click", function () {
        var target = b.getAttribute("data-close");
        if (target === "dialog-modal") closeDialog(null);
        else $(target).classList.add("hidden");
      });
    });
    document.querySelectorAll(".modal").forEach(function (m) {
      m.addEventListener("click", function (e) {
        if (e.target !== m) return;
        if (m.id === "dialog-modal") closeDialog(null);
        else m.classList.add("hidden");
      });
    });

    // 自绘弹窗的确认 / 取消 / 回车
    $("dlg-ok").addEventListener("click", function () { closeDialog(currentDialogValue()); });
    $("dlg-cancel").addEventListener("click", function () { closeDialog(null); });
    $("dlg-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); closeDialog(currentDialogValue()); }
    });

    // 阅读产物页签：对话 / 快速总结 / 深度精读
    document.querySelectorAll(".art-tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        var view = tab.getAttribute("data-view");
        if (view === "chat") showChatView();
        else openArtifact(view);
      });
    });
    $("av-close").addEventListener("click", showChatView);
    $("av-copy").addEventListener("click", copyArtifact);
    $("av-open").addEventListener("click", function () {
      if (!state.currentPaper) return;
      var kind = $("artifact-view").getAttribute("data-kind") || artifactView;
      if (!ARTIFACT_META[kind]) return;
      window.open("/api/papers/" + state.currentPaper.id + "/artifacts/" + kind + "?raw=true", "_blank");
    });
    $("av-regen").addEventListener("click", function () {
      var kind = $("artifact-view").getAttribute("data-kind") || artifactView;
      var info = ARTIFACT_META[kind];
      if (!info || !state.currentPaper) return;
      askConfirm("重新生成「" + info.title + "」", [
        "对《" + shortText(state.currentPaper.title || state.currentPaper.arxiv_id, 40) + "》重跑「" + info.title + "」，会覆盖当前内容。",
        "会真实调用模型，深度精读可能耗时几分钟。",
      ], { okText: "开始重跑" }).then(function (ok) {
        if (ok) runPaperPipeline(state.currentPaper, true, info.steps);
      });
    });

    // 检索页与图谱
    if ($("open-search")) $("open-search").addEventListener("click", openSearch);
    if ($("s-run")) $("s-run").addEventListener("click", startSearch);
    if ($("s-cancel")) $("s-cancel").addEventListener("click", cancelSearch);
    if ($("graph-refresh")) $("graph-refresh").addEventListener("click", function () {
      if (currentGraphNodeId) loadNodeGraph(currentGraphNodeId);
    });
    if ($("graph-export")) $("graph-export").addEventListener("click", function () { exportKnowledgeFiles(); });
    if ($("graph-del")) $("graph-del").addEventListener("click", deleteCurrentGraphNode);

    // 右键菜单与弹窗：点空白处 / 按 Esc 关闭；⌘K（Ctrl+K）直接开检索
    document.addEventListener("click", function (e) {
      var menu = $("ctx-menu");
      if (menu && !menu.classList.contains("hidden") && !menu.contains(e.target)) closeCtxMenu();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        closeCtxMenu();
        if (!$("dialog-modal").classList.contains("hidden")) { closeDialog(null); return; }
        var openModal = Array.prototype.filter.call(document.querySelectorAll(".modal"), function (m) {
          return !m.classList.contains("hidden");
        });
        if (openModal.length) { openModal.forEach(function (m) { m.classList.add("hidden"); }); return; }
        if (artifactView !== "chat") showChatView();   // 产物阅读中按 Esc 回到对话
        return;
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        openSearch();
      }
    });
  }

  // ---------------- 启动 ----------------
  function init() {
    applyWidths();
    applyVisibility();
    bind();
    checkLLMConfigured(false);                  // 未配置模型时提醒（不影响先入库/先读 PDF）
    loadFolders().then(loadPapers).catch(function (e) { toast("初始化失败：" + e.message, "err"); });
  }
  // 暴露 Markdown 渲染器，便于调试/自检（不影响正常功能）
  window.__arxivreader_md = renderMarkdown;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
