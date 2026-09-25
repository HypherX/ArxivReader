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
  function toast(msg, type) {
    var t = $("toast");
    t.textContent = msg;
    t.className = "toast " + (type || "info");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = "toast hidden"; }, 3600);
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
  function inlineMd(s) {
    var codes = [];
    s = s.replace(/`([^`]+)`/g, function (_, c) { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (_, t, u) {
      return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + t + '</a>';
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(/\u0000(\d+)\u0000/g, function (_, i) { return "<code>" + codes[+i] + "</code>"; });
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
        class: "folder-row" + (state.selectedFolderId === n.id ? " selected" : ""),
        style: "padding-left:" + pad + "px",
        "data-id": n.id,
      }, [
        el("span", { class: "fname", text: n.name }),
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
      frag.appendChild(row);
      if (n.children && n.children.length) frag.appendChild(buildTree(n.children, depth + 1));
    });
    return frag;
  }
  function folderOp(act, node) {
    if (act === "add") {
      var name = prompt("在「" + node.name + "」下新建子文件夹：");
      if (name && name.trim()) api.post("/api/folders", { name: name.trim(), parent_id: node.id })
        .then(loadFolders).then(loadPapers).catch(function (e) { toast(e.message, "err"); });
    } else if (act === "rename") {
      var nn = prompt("重命名文件夹：", node.name);
      if (nn && nn.trim() && nn.trim() !== node.name)
        api.patch("/api/folders/" + node.id, { name: nn.trim() })
          .then(loadFolders).then(loadPapers).catch(function (e) { toast(e.message, "err"); });
    } else if (act === "del") {
      if (!confirm("删除文件夹「" + node.name + "」？其中的子文件夹与论文将上移（不会删除论文）。")) return;
      api.del("/api/folders/" + node.id).then(function () {
        if (state.selectedFolderId === node.id) state.selectedFolderId = null;
        return loadFolders();
      }).then(loadPapers).catch(function (e) { toast(e.message, "err"); });
    }
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
  }

  // ---------------- 论文列表 ----------------
  function loadPapers() {
    var qs = [];
    if (state.selectedFolderId !== null) qs.push("folder_id=" + state.selectedFolderId);
    if (state.search) qs.push("q=" + encodeURIComponent(state.search));
    var url = "/api/papers" + (qs.length ? "?" + qs.join("&") : "");
    return api.get(url).then(function (list) {
      state.papers = list; renderPapers();
      $("all-count").textContent = state.selectedFolderId === null ? list.length : $("all-count").textContent;
    }).catch(function (e) { toast(e.message, "err"); });
  }
  function renderPapers() {
    var box = $("paper-list"); box.replaceChildren();
    if (!state.papers.length) { box.appendChild(el("div", { class: "empty", text: "暂无论文。粘贴 ArXiv 链接到顶部添加。" })); return; }
    state.papers.forEach(function (p) {
      var badges = el("div", { class: "p-meta" });
      (p.categories || []).slice(0, 3).forEach(function (c) { badges.appendChild(el("span", { class: "badge", text: c })); });
      if (p.folder_name) badges.appendChild(el("span", { class: "badge folder", text: "📁 " + p.folder_name }));
      if (p.text_truncated) badges.appendChild(el("span", { class: "badge trunc", text: "已截断" }));
      if (!p.has_text) badges.appendChild(el("span", { class: "badge notext", text: "⚠ 无正文" }));
      var ops = el("div", { class: "p-ops" }, [
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
    state.currentPaper = p; renderPapers();
    $("viewer-empty").classList.add("hidden");
    var frame = $("pdf-frame"); frame.classList.remove("hidden");
    frame.src = "/api/papers/" + p.id + "/pdf";
    $("viewer-toolbar").classList.remove("hidden");
    $("viewer-title").textContent = p.title || p.arxiv_id;
    $("chat-title").textContent = p.title || p.arxiv_id;
    updateCtxStatus(p);
    loadSessions();
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
    if (!confirm('从库中删除《' + (p.title || p.arxiv_id) + '》？\n将同时删除本地 PDF 与其对话记录。')) return;
    api.del("/api/papers/" + p.id).then(function () {
      toast("已删除", "ok");
      if (state.currentPaper && state.currentPaper.id === p.id) clearCurrentPaper();
      return Promise.all([loadFolders(), loadPapers()]);
    }).catch(function (e) { toast("删除失败：" + e.message, "err"); });
  }
  function clearCurrentPaper() {
    state.currentPaper = null; state.sessions = []; state.currentSessionId = null;
    $("pdf-frame").classList.add("hidden"); $("pdf-frame").removeAttribute("src");
    $("viewer-empty").classList.remove("hidden");
    $("viewer-toolbar").classList.add("hidden");
    $("chat-title").textContent = "未选择论文";
    $("session-select").replaceChildren();
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
    if (!msgs || !msgs.length) { box.appendChild(el("div", { class: "empty", text: "开始提问吧。例如：这篇论文解决了什么问题？核心方法是什么？" })); return; }
    msgs.forEach(function (m) {
      if (m.role === "system") return;
      var node = el("div", { class: "msg " + m.role });
      setMd(node, m.content);
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

    var start = function (sid) {
      state.currentSessionId = sid;
      input.value = "";
      var box = $("messages");
      if (box.querySelector(".empty")) box.replaceChildren();
      var userNode = el("div", { class: "msg user" }); setMd(userNode, content);
      box.appendChild(userNode);
      var ai = el("div", { class: "msg assistant pending" });
      box.appendChild(ai); box.scrollTop = box.scrollHeight;

      var raw = "";
      state.streaming = true; $("send-btn").disabled = true;
      streamChat(sid, content, function (delta) {
        raw += delta; setMd(ai, raw); box.scrollTop = box.scrollHeight;
      }, function () {
        ai.classList.remove("pending"); if (raw) setMd(ai, raw);
        state.streaming = false; $("send-btn").disabled = false;
        loadSessions();
      }, function (errMsg) {
        ai.classList.remove("pending");
        if (!raw) ai.remove();
        box.appendChild(el("div", { class: "msg error", text: "出错：" + errMsg }));
        state.streaming = false; $("send-btn").disabled = false;
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

  // 以 fetch + ReadableStream 消费后端 SSE（POST，无法用 EventSource）
  function streamChat(sessionId, content, onDelta, onDone, onError) {
    fetch("/api/sessions/" + sessionId + "/messages", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: content }),
    }).then(function (resp) {
      if (!resp.ok || !resp.body) return resp.text().then(function (t) { onError("HTTP " + resp.status + " " + t); });
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = "";
      var finished = false;
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { if (!finished) onDone(); return; }
          buf += decoder.decode(res.value, { stream: true });
          var idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            var frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
            var line = frame.split("\n").filter(function (l) { return l.indexOf("data:") === 0; })[0];
            if (!line) continue;
            var payload;
            try { payload = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
            if (payload.type === "delta") onDelta(payload.text || "");
            else if (payload.type === "error") { finished = true; onError(payload.message || "未知错误"); return; }
            else if (payload.type === "done") { finished = true; onDone(payload); return; }
          }
          return pump();
        });
      }
      return pump();
    }).catch(function (e) { onError(e.message || String(e)); });
  }

  // ---------------- 设置 ----------------
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
  function deleteRule(r) { if (!confirm("删除该规则？")) return; api.del("/api/rules/" + r.id).then(loadRules).catch(function (e) { toast(e.message, "err"); }); }

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
      var name = prompt("新建顶层文件夹：");
      if (name && name.trim()) api.post("/api/folders", { name: name.trim(), parent_id: null })
        .then(loadFolders).catch(function (e) { toast(e.message, "err"); });
    });

    var searchTimer = null;
    $("search").addEventListener("input", function () {
      state.search = $("search").value.trim();
      clearTimeout(searchTimer); searchTimer = setTimeout(loadPapers, 300);
    });

    $("send-btn").addEventListener("click", sendMessage);
    $("chat-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
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
      b.addEventListener("click", function () { $(b.getAttribute("data-close")).classList.add("hidden"); });
    });
    document.querySelectorAll(".modal").forEach(function (m) {
      m.addEventListener("click", function (e) { if (e.target === m) m.classList.add("hidden"); });
    });
  }

  // ---------------- 启动 ----------------
  function init() {
    applyWidths();
    applyVisibility();
    bind();
    loadFolders().then(loadPapers).catch(function (e) { toast("初始化失败：" + e.message, "err"); });
  }
  // 暴露 Markdown 渲染器，便于调试/自检（不影响正常功能）
  window.__arxivreader_md = renderMarkdown;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
