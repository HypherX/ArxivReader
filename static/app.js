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
    [["target-folder", true], ["rule-folder", false]].forEach(function (pair) {
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
      var item = el("div", {
        class: "paper-item" + (state.currentPaper && state.currentPaper.id === p.id ? " active" : ""),
        "data-id": p.id,
      }, [
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
    $("chat-title").textContent = p.title || p.arxiv_id;
    loadSessions();
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
      box.appendChild(el("div", { class: "msg " + m.role, text: m.content }));
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
      box.appendChild(el("div", { class: "msg user", text: content }));
      var ai = el("div", { class: "msg assistant pending", text: "" });
      box.appendChild(ai); box.scrollTop = box.scrollHeight;

      state.streaming = true; $("send-btn").disabled = true;
      streamChat(sid, content, function (delta) {
        ai.textContent += delta; box.scrollTop = box.scrollHeight;
      }, function () {
        ai.classList.remove("pending"); state.streaming = false; $("send-btn").disabled = false;
        loadSessions();
      }, function (errMsg) {
        ai.classList.remove("pending");
        if (!ai.textContent) ai.remove();
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

    document.querySelectorAll("[data-close]").forEach(function (b) {
      b.addEventListener("click", function () { $(b.getAttribute("data-close")).classList.add("hidden"); });
    });
    document.querySelectorAll(".modal").forEach(function (m) {
      m.addEventListener("click", function (e) { if (e.target === m) m.classList.add("hidden"); });
    });
  }

  // ---------------- 启动 ----------------
  function init() {
    bind();
    loadFolders().then(loadPapers).catch(function (e) { toast("初始化失败：" + e.message, "err"); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
