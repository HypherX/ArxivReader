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
    outputLanguage: "zh",
    onlyCurrent: false,
    folderSearch: "",
  };

  // ---------------- 小工具 ----------------
  function $(id) { return document.getElementById(id); }
  function el(tag, props, children) {
    var n = document.createElement(tag);
    if (props) for (var k in props) {
      if (k === "class") n.className = props[k];
      else if (k === "text") {
        if (typeof props[k] === "function") I18n.bind(n, props[k]);
        else I18n.write(n, props[k]);
      }
      else if (k.indexOf("on") === 0) n.addEventListener(k.slice(2).toLowerCase(), props[k]);
      else if (k === "style") n.setAttribute("style", props[k]);
      else if (typeof props[k] === "function") I18n.bind(n, props[k], k);
      else n.setAttribute(k, props[k]);
    }
    (children || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  var toastTimer = null;
  var TOAST_ICON = { info: "ℹ", ok: "✓", err: "✕", warn: "!" };
  function toast(msg, type, ms) {
    var t = $("toast"), kind = type || "info";
    replaceChildren(t, 
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
      I18n.bind($("dlg-title"), function () { return opts.title || I18n.t("ui.36f33adaf094"); });
      var msg = $("dlg-message");
      replaceChildren(msg);
      (opts.lines || []).forEach(function (line) {
        msg.appendChild(el("p", { class: "dlg-line", text: line }));
      });
      var withInput = opts.input !== undefined && opts.input !== null;
      $("dlg-input-wrap").classList.toggle("hidden", !withInput);
      if (withInput) {
        I18n.bind($("dlg-input-label"), function () { return opts.inputLabel || I18n.t("ui.d44e9b3d3b31"); });
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
      replaceChildren(actions);
      choices.forEach(function (c) {
        actions.appendChild(el("button", {
          class: "primary" + (c.danger ? " danger" : ""),
          text: c.text,
          onclick: function () { closeDialog(c.value); },
        }));
      });
      $("dlg-ok").classList.toggle("hidden", choices.length > 0);
      I18n.bind($("dlg-ok"), function () { return opts.okText || I18n.t("ui.fac2a67ad878"); });
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
    return openDialog({ title: title, inputLabel: label || I18n.t("ui.d44e9b3d3b31"), input: value || "",
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
  function loadLayout() { try { return JSON.parse(ReaderPreferences.getItem(LS_LAYOUT)) || {}; } catch (e) { return {}; } }
  function saveLayout(patch) {
    try {
      var cur = loadLayout();
      for (var k in patch) { if (patch.hasOwnProperty(k)) cur[k] = patch[k]; }
      ReaderPreferences.setItem(LS_LAYOUT, JSON.stringify(cur));
    } catch (e) { /* localStorage 不可用时静默降级 */ }
  }

  // ---------------- Markdown 安全渲染 ----------------
  // 所有阅读入口共用本地 Markdown / KaTeX 解析与 DOMPurify 清洗。
  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function replaceChildren(node) {
    I18n.unbind(node);
    node.replaceChildren.apply(node, Array.prototype.slice.call(arguments, 1));
  }
  function lang(zh, en) { return state.outputLanguage === "en" ? en : zh; }
  function renderMarkdown(text) { return window.ReaderRenderer.render(text, { language: state.outputLanguage }); }
  function setMd(node, text, streaming) {
    var opts = { language: state.outputLanguage, toc: !streaming && (node.id === "av-body" || node.id === "review-content") };
    window.ReaderRenderer[streaming ? "stream" : "set"](node, text, opts);
  }

  // ---------------- 推理过程（reasoning_content）折叠块 ----------------
  // 推理折叠区与正文使用同一个 Markdown / 公式渲染器，
  // 流式期间自动展开、结束后自动折叠，历史消息默认折叠可手动展开。
  function makeReasoningBlock(parent) {
    var label = el("span", { class: "reasoning-label", text: function () { return I18n.t("ui.649f3672d833"); } });
    var hint = el("span", { class: "reasoning-hint", text: function () { return I18n.t("ui.384ccd9a5f9a"); } });
    var head = el("summary", { class: "reasoning-head" }, [label, hint]);
    var body = el("div", { class: "reasoning-body" });
    var wrap = el("details", { class: "reasoning" }, [head, body]);
    parent.insertBefore(wrap, parent.firstChild);
    return { wrap: wrap, body: body, label: label };
  }
  function renderReasoning(block, text, streaming) {
    setMd(block.body, text, streaming);
    I18n.bind(block.label, function () { return I18n.t("ui.a62902c0dc99") + text.length + I18n.t("ui.457271dfe88b") + (streaming ? I18n.t("ui.84348e8905f7") : "") + ")"; });
  }

  // ---------------- API 封装 ----------------
  function req(method, url, body) {
    var opt = { method: method, headers: {} };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      if (r.status === 204) return null;
      return r.text().then(function (txt) {
        var data = txt ? JSON.parse(txt) : null;
        if (!r.ok) throw I18n.apiError(data, r.status);
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
      out.push({ id: n.id, name: n.name, system_kind: n.system_kind, depth: depth });
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
      I18n.write($("all-count"), state.selectedFolderId === null ? total : countAll(tree));
    });
  }
  function countAll(nodes) {
    return nodes.reduce(function (s, n) { return s + (n.paper_count || 0) + countAll(n.children || []); }, 0);
  }
  function renderFolderTree() {
    var box = $("folder-tree"); replaceChildren(box);
    box.appendChild(buildTree(state.folders, 0));
  }
  function folderIcon() {
    var icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("class", "folder-icon");
    icon.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(icon.namespaceURI, "path");
    path.setAttribute("d", "M3 7V5a1 1 0 011-1h5l2 3h9a1 1 0 011 1v11H3Z");
    icon.appendChild(path); return icon;
  }
  function folderMatches(node) {
    return !state.folderSearch || ((node.path || node.name) + " " + (node.display_name || "")).toLowerCase().indexOf(state.folderSearch) >= 0
      || (node.children || []).some(folderMatches);
  }
  function buildTree(nodes, depth) {
    var frag = document.createDocumentFragment();
    nodes.forEach(function (n) {
      if (!folderMatches(n)) return;
      var children = n.children || [], collapsed = !!(loadLayout().foldersCollapsed || {})[n.id];
      var toggle = el("button", { class: "folder-toggle", text: children.length ? (collapsed ? "›" : "⌄") : "·",
        "aria-label": function () { return collapsed ? I18n.t("ui.a49f5287fc9e") : I18n.t("ui.47c91fadcc3e"); }, "aria-expanded": String(!collapsed),
        onclick: function (e) {
          e.stopPropagation();
          var saved = loadLayout().foldersCollapsed || {}; saved[n.id] = !collapsed;
          saveLayout({ foldersCollapsed: saved }); renderFolderTree();
        } });
      var row = el("div", {
        class: "folder-row" + (state.selectedFolderId === n.id ? " selected" : ""),
        style: "--folder-depth:" + depth, "data-id": n.id, role: "treeitem", tabindex: "0",
        title: function () { return (n.system_kind === "inbox" ? I18n.t("system.inbox") : (n.path || n.name)) + I18n.t("ui.feef1407b5dc") + n.paper_count + I18n.t("ui.c595735ca64c") + n.subtree_paper_count; },
        "aria-level": String(depth + 1), "aria-selected": String(state.selectedFolderId === n.id)
      }, [toggle, folderIcon(), el("span", { class: "fname", text: function () { return n.system_kind === "inbox" ? I18n.t("system.inbox") : n.name; } }),
        el("span", { class: "count", text: String(n.paper_count || 0) + " / " + (n.subtree_paper_count || 0) }),
        el("button", { class: "folder-menu mini", title: function () { return I18n.t("ui.76a53e4eb3e4"); }, text: "⋯", onclick: function (e) {
          e.stopPropagation(); openCtxMenu(e, n);
        } })]);
      row.addEventListener("click", function () { selectFolder(n.id); });
      row.addEventListener("keydown", function (e) { if (e.key === "Enter") selectFolder(n.id); });
      row.addEventListener("contextmenu", function (e) { e.preventDefault(); openCtxMenu(e, n); });
      frag.appendChild(row);
      if (children.length && (!collapsed || state.folderSearch)) frag.appendChild(buildTree(children, depth + 1));
    });
    return frag;
  }

  // 🧭 文件夹是方向树的镜像：改名要走方向节点（后代路径与图谱 md 才会一起平移），
  // 删除则是删方向节点（论文保留），普通文件夹则维持原来的"子项上移"语义。
  function addSubFolder(node) {
    askText(function () { return I18n.t("ui.b719a1912da4"); }, "", function () { return I18n.t("ui.7d24c4ee6a1f"); }, { lines: [function () { return I18n.t("ui.926818783cf9") + node.name + I18n.t("ui.b5078a8c4512"); }] })
      .then(function (name) {
        if (!name) return null;
        return api.post("/api/folders", { name: name, parent_id: node.id })
          .then(loadFolders).then(loadPapers).catch(function (e) { toast(function () { return e.message; }, "err"); });
      });
  }

  function renameFolder(node) {
    var dirNodeId = node.direction_node_id;
    askText(function () { return dirNodeId ? I18n.t("ui.a19b01883e49") : I18n.t("ui.da2f8f756e74"); }, node.name, function () { return I18n.t("ui.92ddb51db6c4"); },
      dirNodeId ? { lines: [function () { return I18n.t("ui.7330109ae79b"); }] } : null)
      .then(function (name) {
        if (!name || name === node.name) return null;
        var target = dirNodeId ? ("/api/knowledge/nodes/" + dirNodeId)
          : ("/api/folders/" + node.id);
        return api.patch(target, { name: name }).then(function () {
          toast(function () { return I18n.t("ui.c45371380339") + name + "」"; }, "ok");
          return loadFolders();
        }).then(loadPapers).then(function () {
          if (dirNodeId && currentGraphNodeId === dirNodeId) loadNodeGraph(dirNodeId);
        }).catch(function (e) { toast(function () { return e.message; }, "err"); });
      });
  }

  function deleteFolder(node) {
    askConfirm(function () { return I18n.t("ui.780dfc4c1142"); }, [function () { return I18n.t("folder.delete_question", { name: node.system_kind === "inbox" ? I18n.t("system.inbox") : (node.path || node.name) }); },
      function () { return node.parent_id ? I18n.t("ui.308d1fb0ab01") : I18n.t("ui.25c214d1a84b"); },
      function () { return I18n.t("ui.8abd5282befe"); }],
      { okText: I18n.t("ui.2f9daa828907"), danger: true }).then(function (ok) {
      if (!ok) return null;
      return api.del("/api/folders/" + node.id).then(function () {
        if (state.selectedFolderId === node.id) selectFolder(null);
        if (currentGraphNodeId === node.direction_node_id) restoreViewerAfterGraph();
        return loadFolders();
      }).then(loadPapers).catch(function (e) { toast(function () { return e.message; }, "err"); });
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
      { text: function () { return I18n.t("ui.772165206ef1"); }, run: function () { addSubFolder(node); } },
      { text: function () { return I18n.t("ui.ed983c10213a"); }, run: function () { renameFolder(node); } },
      { text: function () { return I18n.t("ui.b809b39820ad"); }, run: function () { moveFolder(node); } },
      { text: function () { return I18n.t("ui.2a6909752eaa"); }, run: function () { openReview(node); } },
      { text: function () { return I18n.t("ui.f21995584a5a"); }, run: function () { openReview(node, true); } },
    ];
    if (node.direction_node_id) {
      items.push({ text: function () { return I18n.t("ui.8d6f896b66b5"); }, run: function () {
        api.post("/api/translations/node/" + node.direction_node_id).then(loadFolders).catch(function (err) { toast(function () { return err.message; }, "err"); });
      } });
      items.push({ text: function () { return I18n.t("ui.05a3914c8231"); }, run: function () { showNodeGraph(node.direction_node_id); } });
      items.push({ text: function () { return I18n.t("ui.091d49b93f80"); }, run: function () { exportKnowledgeFiles(); } });
    }
    items.push({ sep: true });
    items.push({ text: function () { return I18n.t("ui.56849580143e"); }, danger: true, run: function () { deleteFolder(node); } });

    replaceChildren(menu);
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
    toast(function () { return I18n.t("ui.04d9c55a7031"); });
    api.post("/api/knowledge/export", {}).then(function (res) {
      toast(function () { return I18n.t("ui.f88a7f925889") + (res.count || 0) + I18n.t("ui.ede4fd4c79b3")
        + (res.moved_papers ? I18n.t("ui.57cdd388d069") + res.moved_papers + I18n.t("ui.8da5cb9a82ba") : ""); }, "ok");
      if (currentGraphNodeId) loadNodeGraph(currentGraphNodeId);
      return loadFolders();
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function renderFolderSelects() {
    [["target-folder", true], ["rule-folder", false], ["move-folder", false]].forEach(function (pair) {
      var sel = $(pair[0]); if (!sel) return;
      var prev = sel.value; replaceChildren(sel);
      if (pair[1]) sel.appendChild(el("option", { value: "", text: function () { return I18n.t("ui.464c0b9395bc"); } }));
      state.flatFolders.forEach(function (f) {
        sel.appendChild(el("option", { value: String(f.id), text: function () { return "　".repeat(f.depth) + (f.system_kind === "inbox" ? I18n.t("system.inbox") : f.name); } }));
      });
      if (prev) sel.value = prev;
    });
  }
  function selectFolder(id) {
    state.selectedFolderId = id;
    $("folder-all").classList.toggle("selected", id === null);
    $("folder-scope").classList.toggle("hidden", id === null);
    var node = id !== null ? state.folderMap[id] : null;
    var path = [], cursor = node;
    while (cursor) { path.unshift(cursor); cursor = state.folderMap[cursor.parent_id]; }
    var bread = $("folder-breadcrumb"); replaceChildren(bread);
    bread.appendChild(el("button", { class: "mini", text: function () { return I18n.t("ui.c4fb397688ed"); }, onclick: function () { selectFolder(null); } }));
    path.forEach(function (part) {
      bread.appendChild(document.createTextNode(" / "));
      bread.appendChild(el("button", { class: "mini", text: function () { return part.system_kind === "inbox" ? I18n.t("system.inbox") : part.name; }, title: part.path,
        onclick: function () { selectFolder(part.id); } }));
    });
    renderFolderTree(); loadPapers();
  }
  function moveFolder(node) {
    askChoice(function () { return I18n.t("folder.move_title", { name: node.path || node.name }); }, [function () { return I18n.t("ui.72f97fe684b3"); }],
      [{ text: function () { return I18n.t("ui.4522f8325fde"); }, value: "root" }].concat(state.flatFolders.filter(function (f) {
        var item = state.folderMap[f.id];
        return f.id !== node.id && !(item.path || "").startsWith(node.path + "/");
      }).map(function (f) { return { text: function () { return f.system_kind === "inbox" ? I18n.t("system.inbox") : state.folderMap[f.id].path; }, value: String(f.id) }; })))
      .then(function (id) {
        if (id === null) return;
        return api.patch("/api/folders/" + node.id, { parent_id: id === "root" ? null : Number(id) })
          .then(loadFolders).then(function () { selectFolder(node.id); });
      }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }

  // ---------------- ArXiv 检索页（检索 Pipeline + 待读缓冲区） ----------------
  var SEARCH_CATS = ["cs.CL", "cs.AI", "cs.LG", "cs.CV", "cs.NE", "stat.ML", "cs.RO", "cs.SE"];
  var SEARCH_STAGES = [
    { name: "search", get title() { return I18n.t("ui.085dd39d3615"); } },
    { name: "screen", get title() { return I18n.t("ui.426ef335832a"); } },
    { name: "summarize", get title() { return I18n.t("ui.c17b72ce6f86"); } },
    { name: "buffer", get title() { return I18n.t("ui.ffc240e35e55"); } },
  ];
  var RUN_STATE = { get running() { return I18n.t("ui.1f0eb99b7ed0"); }, get done() { return I18n.t("ui.f28461bb49c8"); }, get failed() { return I18n.t("ui.28384d7afd2e"); },
    get cancelled() { return I18n.t("ui.88cf2dc00c34"); }, get canceled() { return I18n.t("ui.88cf2dc00c34"); }, get stopped() { return I18n.t("ui.88cf2dc00c34"); } };
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
    box.appendChild(el("input", { id: "s-cats-custom", type: "text", placeholder: function () { return I18n.t("ui.0ffbf4133d95"); } }));
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
    try { ReaderPreferences.setItem(LS_SEARCH, JSON.stringify(body || collectSearchBody())); }
    catch (e) { /* localStorage 不可用时静默降级 */ }
  }

  function restoreSearchForm() {
    var saved = null;
    try { saved = JSON.parse(ReaderPreferences.getItem(LS_SEARCH)); } catch (e) { saved = null; }
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
      toast(function () { return I18n.t("ui.232d2e32f87d"); }, "err"); return;
    }
    saveSearchForm(body);
    searchPolls = 0;
    searchPolledRun = null;
    $("s-run").disabled = true;
    $("s-cancel").classList.remove("hidden");
    $("s-progress").classList.remove("hidden");
    replaceChildren($("s-logs"), el("div", { class: "log info", text: function () { return I18n.t("ui.daa71c039991"); } }));
    api.post("/api/search/run", body).then(function (run) {
      searchRunId = run.id;
      renderSearchRun(run);
      pollSearch(run.id, false);
    }).catch(function (e) { toast(function () { return e.message; }, "err"); $("s-run").disabled = false; $("s-cancel").classList.add("hidden"); });
  }

  function stageDetail(logs, stage) {
    for (var i = logs.length - 1; i >= 0; i--) {
      if (logs[i].stage === stage) return statusLabel(logs[i].status);
    }
    return "";
  }
  function renderRunLogs(box, logs) {
    var fingerprint = JSON.stringify(logs);
    if (box.getAttribute("data-last") === fingerprint) return;
    box.setAttribute("data-last", fingerprint);
    replaceChildren(box);
    logs.slice(-60).forEach(function (record) {
      var line = el("div", { class: "log " + (record.level || "info") }, [
        el("span", { class: "lt", text: record.ts || "" }),
        el("span", { text: function () {
          return " " + I18n.t(record.code || "log.updated", Object.assign({}, record.params || {}, {
            stage: I18n.t("stage." + record.stage), status: statusLabel(record.status)
          })) + (record.arxiv_id ? " · " + record.arxiv_id : "");
        } })
      ]);
      if (record.original || record.message) line.appendChild(el("details", {}, [
        el("summary", { text: function () { return I18n.t("log.original"); } }),
        el("div", { text: record.original || record.message })
      ]));
      box.appendChild(line);
    });
    box.scrollTop = box.scrollHeight;
  }

  function renderSearchRun(run) {
    var logs = run.logs || [];
    // 兼容历史数据：stage 已 done 但 status 未写回的 run 视为已结束（否则界面永远停在“运行中”）
    var effStatus = (run.status === "running" && run.stage === "done") ? "done" : run.status;
    var box = $("s-stages"); replaceChildren(box);
    SEARCH_STAGES.forEach(function (s) {
      var done = (run.stages_done || []).indexOf(s.name) >= 0 || logs.some(function (l) { return l.stage === s.name && l.status === "done"; });
      var running = !done && s.name === (run.stage || "") && effStatus === "running";
      box.appendChild(el("div", { class: "stage " + (done ? "done" : (running ? "running" : "todo")) }, [
        el("span", { class: "dot", text: done ? "✓" : (running ? "◐" : "○") }),
        el("span", { class: "st", text: function () { return I18n.t("stage." + s.name); } }),
        el("span", { class: "sd", text: function () { return stageDetail(logs, s.name); } }),
      ]));
    });
    box.appendChild(el("div", { class: "stage " + (effStatus === "done" ? "done" : (effStatus === "running" ? "running" : "failed")) }, [
      el("span", { class: "dot", text: effStatus === "done" ? "✓" : (effStatus === "running" ? "◐" : "■") }),
      el("span", { class: "st", text: function () { return I18n.t("ui.9bf6abcc0b04") + statusLabel(effStatus)
        + (run.cancel_requested && effStatus === "running" ? I18n.t("ui.d2a5e3de518f") : ""); } }),
    ]));

    var c = run.counters || {};
    $("s-bar").style.width = (effStatus === "done" ? 100
      : Math.min(97, Math.round(100 * ((c.screened || 0) + (c.summarized || 0)) / Math.max(1, 2 * (c.found || 1))))) + "%";
    I18n.bind($("s-counters"), function () { return I18n.t("ui.036144607b66") + (c.found || 0) + I18n.t("ui.7dbf2207e6a3") + (c.duplicated || 0)
      + I18n.t("ui.9449f69f0df0") + (c.passed || 0) + I18n.t("ui.d911aa8d7cc7") + (c.summarized || 0)
      + I18n.t("ui.b07357fa4277") + (c.buffered || 0) + I18n.t("ui.d025c89a0f4b") + (c.dropped || 0)
      + (run.error ? I18n.t("ui.b81306749e4d") + I18n.t("error.502") : ""); });

    renderRunLogs($("s-logs"), logs);
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
        toast(function () { return I18n.t("ui.b0ce081ebc06"); }, "warn", 8000);
        return;
      }
      loadBuffer();
      var effStatus = (run.status === "running" && run.stage === "done") ? "done" : run.status;
      if (!silent) {
        toast(function () { return I18n.t("ui.aa0b63126c13") + (effStatus === "done" ? I18n.t("ui.c0b3fbff51cc") : (effStatus === "failed" ? I18n.t("ui.28384d7afd2e") : I18n.t("ui.f006455e3baf")))
          + I18n.t("ui.1610ed07f57b") + ((run.counters || {}).found || 0) + I18n.t("ui.b07357fa4277")
          + ((run.counters || {}).buffered || 0) + I18n.t("ui.78548675f31a")
          + (run.error ? "（" + run.error + "）" : ""); },
          effStatus === "done" ? "ok" : (effStatus === "failed" ? "err" : "warn"),
          effStatus === "done" ? 3600 : 6000);
      }
    }).catch(function (e) { if (!silent) toast(function () { return e.message; }, "err"); });
  }

  function cancelSearch() {
    if (!searchRunId) return;
    api.post("/api/search/cancel?run_id=" + searchRunId).then(function () {
      toast(function () { return I18n.t("ui.9ee2601e89e0"); }, "info");
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }

  function loadBuffer() {
    var box = $("s-buffer");
    if (box && !state.bufferReady) replaceChildren(box, skeleton("rows", 3));
    return api.get("/api/buffer?status=pending&limit=200").then(function (data) {
      var counts = data.counts || {};
      I18n.bind($("s-buffer-counts"), function () { return I18n.t("ui.ecc264f1bbe6") + (counts.pending || 0) + I18n.t("ui.2d7e1bf61a4d")
        + (counts.read || 0) + I18n.t("ui.2dd8d6698f6e") + (counts.rejected || 0); });
      state.bufferReady = true;
      renderBuffer(data.items || []);
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }

  // 批量选择：bufferItems 是当前渲染条目的快照（批量操作只针对它统计可选集合）；
  // bufferSelected 是勾选集合（id -> true），重新加载列表时清空。
  var bufferItems = [];
  var bufferSelected = {};

  function updateBatchBar() {
    var bar = $("s-batch-bar");
    if (!bar) return;
    var selectable = bufferItems.filter(function (it) { return it.selectable; });
    var picked = selectable.filter(function (it) { return bufferSelected[it.id]; }).length;
    bar.classList.toggle("hidden", !selectable.length);
    var selAll = $("s-select-all");
    if (selAll) {
      selAll.disabled = !selectable.length;
      selAll.checked = selectable.length > 0 && picked === selectable.length;
      selAll.indeterminate = picked > 0 && picked < selectable.length;
    }
    var btn = $("s-batch-reject");
    if (btn) {
      btn.disabled = picked === 0;
      I18n.bind(btn, function () { return picked ? I18n.t("ui.164735485c19") + picked + "）" : I18n.t("ui.29670536129f"); });
    }
    var hint = $("s-batch-hint");
    if (hint) I18n.bind(hint, function () { return picked ? I18n.t("ui.52c963128eab") + picked + I18n.t("ui.78548675f31a") : I18n.t("ui.f4394c85673a"); });
  }

  function toggleSelectAllBuffer(on) {
    bufferItems.forEach(function (it) {
      if (!it.selectable) return;
      if (on) bufferSelected[it.id] = true;
      else delete bufferSelected[it.id];
    });
    document.querySelectorAll("#s-buffer .b-pick input").forEach(function (cb) {
      var it = bufferItems.filter(function (x) { return x.id === Number(cb.getAttribute("data-id")); })[0];
      cb.checked = !!(on && it && it.selectable);
    });
    updateBatchBar();
  }

  function rejectSelectedBuffer() {
    var ids = bufferItems
      .filter(function (it) { return it.selectable && bufferSelected[it.id]; })
      .map(function (it) { return it.id; });
    if (!ids.length) { toast(function () { return I18n.t("ui.25f2e796ed2d"); }, "warn"); return; }
    askConfirm(function () { return I18n.t("ui.29670536129f"); }, [
      function () { return I18n.t("ui.9e3a984bce95") + ids.length + I18n.t("ui.1f047aa560fc"); },
      function () { return I18n.t("ui.51bb48599faa"); },
    ], { okText: I18n.t("ui.b16f71b55691"), danger: true }).then(function (ok) {
      if (!ok) return null;
      return api.post("/api/buffer/reject-batch", { ids: ids }).then(function (res) {
        var skipped = (res.skipped_ids || []).length;
        toast(function () { return I18n.t("ui.d96da28b1618") + (res.rejected || 0) + I18n.t("ui.78548675f31a") + (skipped ? I18n.t("ui.af3c221a116e") + skipped + I18n.t("ui.78548675f31a") : ""); },
          res.rejected ? "ok" : "warn");
        return loadBuffer();
      }).catch(function (e) { toast(function () { return e.message; }, "err"); });
    });
  }

  function renderBuffer(items) {
    var box = $("s-buffer"); replaceChildren(box);
    bufferItems = []; bufferSelected = {};
    if (!items.length) {
      box.appendChild(emptyState("📥", function () { return I18n.t("ui.a91dc7dda4e8"); },
        function () { return I18n.t("ui.4cb1b4b58220"); }));
      updateBatchBar();
      return;
    }
    items.forEach(function (b) {
      var verified = !!b.summary_text;
      var selectable = !b.promote_state;      // 提升入库中的条目不可批量丢弃
      bufferItems.push({ id: b.id, selectable: selectable });
      var ops = el("div", { class: "b-ops" }, [
        el("button", { class: "mini", text: function () { return I18n.t("ui.13d61fea9f17"); }, onclick: function () { previewBuffer(b, row); } }),
      ]);
      if (!verified) {
        ops.appendChild(el("button", {
          class: "mini", title: function () { return I18n.t("ui.4a8a84c10769"); },
          text: function () { return I18n.t("ui.62c042c0c494"); },
          onclick: function (ev) { summarizeBuffer(b, ev.target); },
        }));
      }
      ops.appendChild(el("button", { class: "mini primary", text: function () { return I18n.t("ui.396530b08ab5"); }, onclick: function () { promoteBuffer(b, row); } }));
      ops.appendChild(el("button", { class: "mini danger", text: function () { return I18n.t("ui.b16f71b55691"); }, onclick: function () { rejectBuffer(b, row); } }));

      var pickWrap;
      if (selectable) {
        var pick = el("input", { type: "checkbox", "data-id": String(b.id) });
        pick.checked = !!bufferSelected[b.id];
        pick.addEventListener("change", function () {
          if (pick.checked) bufferSelected[b.id] = true;
          else delete bufferSelected[b.id];
          updateBatchBar();
        });
        pickWrap = el("label", { class: "b-pick", title: function () { return I18n.t("ui.44f5b1b6598d"); } }, [pick]);
      } else {
        pickWrap = el("span", { class: "b-pick busy", title: function () { return I18n.t("ui.5b6b025cb0b0"); }, text: "⏳" });
      }

      var row = el("div", { class: "buffer-item" }, [
        el("div", { class: "b-head" }, [
          pickWrap,
          el("span", { class: "b-title", text: b.title || b.arxiv_id }),
          el("span", { class: "b-score", text: (b.match_score || 0).toFixed(2) }),
        ]),
        el("div", { class: "b-meta", text: [(b.authors || []).slice(0, 3).join(", "),
          (b.published || "").slice(0, 10), b.arxiv_id].filter(Boolean).join(" · ") }),
        el("div", { class: "b-badges" }, [
          el("span", { class: "b-chip" + (verified ? " ok" : " warn"),
            text: function () { return verified ? I18n.t("ui.77cc7fbb5f5d") : I18n.t("ui.8245f0ab274b"); } }),
          el("span", { class: "b-chip path", text: function () { return b.verified_path || b.suggested_path || I18n.t("ui.92be9f13c41f"); } }),
        ]),
        el("div", { class: "b-reason", text: b.verify_reason || b.screen_reason || "" }),
        ops,
        el("div", { class: "b-detail hidden" }),
      ]);
      box.appendChild(row);
    });
    updateBatchBar();
  }

  function summarizeBuffer(b, btn) {
    btn.disabled = true;
    I18n.bind(btn, function () { return I18n.t("ui.e7e21d6b6951"); });
    api.post("/api/buffer/" + b.id + "/summarize", {}).then(function () {
      toast(function () { return I18n.t("ui.944a544eb929"); }, "ok");
      loadBuffer();
    }).catch(function (e) {
      toast(function () { return I18n.t("ui.3779ec5d5a96") + e.message; }, "err");
      btn.disabled = false; I18n.bind(btn, function () { return I18n.t("ui.62c042c0c494"); });
    });
  }

  function previewBuffer(b, row) {
    var detail = row.querySelector(".b-detail");
    if (!detail.classList.contains("hidden")) { detail.classList.add("hidden"); return; }
    api.get("/api/buffer/" + b.id).then(function (full) {
      replaceChildren(detail);
      detail.appendChild(el("div", { class: "b-sec", text: function () { return I18n.t("ui.21c04b2eeeb4"); } }));
      detail.appendChild(el("div", { class: "b-text", text: function () { return full.abstract || I18n.t("ui.c7bcc6d27f3a"); } }));
      detail.appendChild(el("div", { class: "b-sec", text: function () { return I18n.t("ui.2d97cc22a742"); } }));
      var md = el("div", { class: "md" });
      setMd(md, full.summary_text || I18n.t("ui.f1fa19eac425"));
      detail.appendChild(md);
      detail.appendChild(el("div", { class: "b-sec", text: function () { return I18n.t("ui.3c1ba320274e"); } }));
      detail.appendChild(el("div", { class: "b-text", text: full.verify_reason || "—" }));
      detail.classList.remove("hidden");
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }

  function promoteBuffer(b, row) {
    askConfirm(function () { return I18n.t("ui.396530b08ab5"); }, [
      shortText(b.title || b.arxiv_id, 46),
      function () { return I18n.t("ui.78251168cefc"); },
      function () { return I18n.t("ui.3804b9c9b49c"); },
    ], { okText: I18n.t("ui.66b2dbc6cbdc") }).then(function (ok) {
      if (!ok) return null;
      var badge = el("span", { class: "b-state", text: function () { return I18n.t("ui.f938cc565569"); } });
      row.querySelector(".b-ops").appendChild(badge);
      return api.post("/api/buffer/" + b.id + "/promote?background=true&trigger_pipeline=true", {}).then(function () {
        var timer = setInterval(function () {
          api.get("/api/buffer/" + b.id).then(function (full) {
            if (full.promote_state === "running" || full.promote_state === "queued") {
              I18n.bind(badge, function () { return full.promote_state === "running" ? I18n.t("ui.d3f9bc7c59aa") : I18n.t("ui.f938cc565569"); });
              return;
            }
            clearInterval(timer);
            if (full.promote_state === "done") {
              I18n.bind(badge, function () { return I18n.t("ui.150f4279f53b"); });
              toast(function () { return I18n.t("ui.ea6750b944fe"); }, "ok");
              loadFolders(); loadPapers(); loadBuffer();
            } else {
              I18n.bind(badge, function () { return I18n.t("ui.28384d7afd2e"); });
              toast(function () { return I18n.t("ui.0b7955374c21") + (full.promote_error || I18n.t("ui.13a46616e16f")); }, "err");
            }
          }).catch(function () { /* 轮询失败就下次再试 */ });
        }, 3000);
      }).catch(function (e) { badge.remove(); toast(function () { return e.message; }, "err"); });
    });
  }

  function rejectBuffer(b, row) {
    api.post("/api/buffer/" + b.id + "/reject", {}).then(function () {
      row.remove(); loadBuffer(); toast(function () { return I18n.t("ui.ba23cebddcfc"); }, "info");
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }

  // ---------------- 智能关联检索（Related Work 关联发现） ----------------
  // 与 ArXiv 检索页的关系：那是“按检索条件找新论文”；这里是从**当前论文的
  // Related Work / 参考文献**出发反向找相关论文，判定强相关后可直接加入详细阅读。
  var relatedPaper = null;
  var relatedRunId = null;
  var relatedTimer = null;
  var relatedPolledRun = null;
  var relatedPolls = 0;
  var relatedStages = null;        // 步骤清单从后端读取（不写死中文名，与 STAGE_TITLES 同源）
  var RELATED_POLL_LIMIT = 900;    // 2s × 900 ≈ 30 分钟硬上限，服务端异常时不无限轮询

  function openRelated(paper) {
    paper = paper || state.currentPaper;
    if (!paper) { toast(function () { return I18n.t("ui.7912dc9e9b8b"); }, "err"); return; }
    clearTimeout(relatedTimer);
    relatedRunId = null; relatedPolledRun = null; relatedPolls = 0;
    relatedPaper = paper;
    I18n.bind($("related-head-hint"), function () { return I18n.t("ui.70e4ba4049f2") + shortText(paper.title || paper.arxiv_id, 40); });
    $("related-modal").classList.remove("hidden");
    loadRelated().then(function (run) {
      if (run && run.status === "running") pollRelated(run.id, true);
    });
  }

  function loadRelated() {
    if (!relatedPaper) return Promise.resolve();
    var box = $("r-buffer"), paperId = relatedPaper.id;
    if (box && !box.getAttribute("data-ready")) replaceChildren(box, skeleton("rows", 3));
    return api.get("/api/papers/" + paperId + "/related?include_discarded=" + $("r-show-discarded").checked).then(function (data) {
      if (!relatedPaper || relatedPaper.id !== paperId) return null;
      box.setAttribute("data-ready", "1");
      relatedStages = data.stages || [];
      relatedRunId = data.run ? data.run.id : null;
      renderRelatedRun(data.run);
      renderRelatedCandidates(data.candidates || [], data.counts || {});
      return data.run;
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }

  function renderRelatedRun(run) {
    var stages = relatedStages || [];
    var logs = (run && run.logs) || [];
    var effStatus = run ? ((run.status === "running" && run.stage === "done") ? "done" : run.status) : "";
    var running = effStatus === "running";
    $("r-progress").classList.toggle("hidden", !run);
    var box = $("r-stages"); replaceChildren(box);
    if (run) {
      stages.forEach(function (s) {
        var done = (run.stages_done || []).indexOf(s.name) >= 0 || logs.some(function (l) { return l.stage === s.name && l.status === "done"; });
        var isRunning = !done && s.name === (run.stage || "") && running;
        box.appendChild(el("div", { class: "stage " + (done ? "done" : (isRunning ? "running" : "todo")) }, [
          el("span", { class: "dot", text: done ? "✓" : (isRunning ? "◐" : "○") }),
          el("span", { class: "st", text: function () { return I18n.t("stage." + s.name); } }),
          el("span", { class: "sd", text: function () { return stageDetail(logs, s.name); } }),
        ]));
      });
      box.appendChild(el("div", { class: "stage " + (effStatus === "done" ? "done" : (running ? "running" : "failed")) }, [
        el("span", { class: "dot", text: effStatus === "done" ? "✓" : (running ? "◐" : "■") }),
        el("span", { class: "st", text: function () { return I18n.t("ui.9bf6abcc0b04") + statusLabel(effStatus)
          + (run.cancel_requested && running ? I18n.t("ui.22b07c7b75c0") : ""); } }),
      ]));
    }

    var c = (run && run.counters) || {};
    $("r-bar").style.width = (effStatus === "done" ? 100
      : Math.min(96, Math.round(100 * ((c.summarized || 0) + (c.strong || 0) + (c.related || 0) + (c.uncertain || 0) + (c.unrelated || 0))
          / Math.max(1, 2 * (c.selected || 1))))) + "%";
    I18n.bind($("r-counters"), function () {
    var counterLabels = [["seeds", I18n.t("ui.fbbf1f51ea4a"), "References"], ["queries", I18n.t("ui.6e02610c0cad"), "Topic queries"],
      ["found", I18n.t("ui.393df9bb13ea"), "Found"], ["duplicated", I18n.t("ui.1bd91a7d0c53"), "Already saved"], ["selected", I18n.t("ui.a4f1d1644ce6"), "Selected"],
      ["summarized", I18n.t("ui.0b3f90e2d9b9"), "Summarized"], ["strong", I18n.t("ui.52ec36274dee"), "Highly related"], ["related", I18n.t("ui.7dfa43a32757"), "Related"],
      ["uncertain", I18n.t("ui.ccf9206892f8"), "Uncertain"], ["unrelated", I18n.t("ui.d3892c577fc7"), "Unrelated"], ["remaining", I18n.t("ui.b5e75830ac4f"), "Remaining"],
      ["failed", I18n.t("ui.9a0392d0d436"), "Candidate failures"], ["search_failed", I18n.t("ui.bf4cedf76a4e"), "Query failures"], ["extraction_failed", I18n.t("ui.a1d66d5b150e"), "Extraction failures"]];
    return run ? counterLabels.map(function (entry) {
      return lang(entry[1], entry[2]) + " " + (c[entry[0]] || 0);
    }).join(" · ") + (run.error ? " · " + I18n.t("ui.2227758ede17") + I18n.t("error.502") : "") : ""; });

    renderRunLogs($("r-logs"), logs);
    $("r-run").disabled = running;
    $("r-continue").disabled = running || !(c.remaining > 0);
    $("r-retry").disabled = running;
    $("r-cancel").classList.toggle("hidden", !running);
  }

  function renderRelatedCandidates(items, counts) {
    I18n.bind($("r-counts"), function () { return I18n.t("ui.648dde4b5aef") + (counts.unprocessed || 0)
      + " · " + I18n.t("ui.d525ad41f960") + (counts.failed || 0) + " · " + I18n.t("ui.1f68122bb043") + (counts.promoted || 0); });
    var box = $("r-buffer"); replaceChildren(box);
    if (!items.length) {
      box.appendChild(emptyState("🧲", function () { return I18n.t("ui.06c012d8e4b6"); }, function () { return I18n.t("ui.6faeeecd3420"); }));
      return;
    }
    items.forEach(function (c) {
      var promoted = c.status === "promoted" || c.promote_state === "done";
      var ops = el("div", { class: "b-ops" }, [
        el("button", { class: "mini", text: function () { return I18n.t("ui.13d61fea9f17"); }, onclick: function () { previewRelated(c, row); } })
      ]);
      if (c.in_library && c.promoted_paper_id) {
        ops.appendChild(el("button", { class: "mini", text: function () { return I18n.t("ui.c771248e511f"); },
          onclick: function () { openPaperById(c.promoted_paper_id); } }));
      }
      if (!promoted || c.promote_state === "failed") {
        ops.appendChild(el("button", { class: "mini primary", text: function () { return c.promote_state === "failed" ? I18n.t("ui.7c77f8645948") : I18n.t("ui.396530b08ab5"); },
          onclick: function () { promoteRelated(c, row); } }));
      }
      ops.appendChild(el("button", { class: "mini danger", text: function () { return I18n.t("ui.b16f71b55691"); },
        onclick: function () { discardRelated(c, row); } }));

      var badges = el("div", { class: "b-badges" }, [
        el("span", { class: "b-chip", text: function () { return relevanceLabel(c.relevance) + " " + (c.relevance_score || 0).toFixed(2); } }),
        el("span", { class: "b-chip", text: function () { return I18n.t("ui.443b94ca42e8") + (c.match_score || 0).toFixed(2); } }),
      ]);
      if (c.retryable) ops.appendChild(el("button", { class: "mini", text: function () { return I18n.t("ui.b8784c8dd563"); }, onclick: function () {
        var paperId = relatedPaper && relatedPaper.id;
        api.post("/api/related/candidates/" + c.id + "/retry").then(function (run) {
          if (!relatedPaper || relatedPaper.id !== paperId) return;
          relatedRunId = run.id; pollRelated(run.id, false);
        }).catch(function (e) { toast(function () { return e.message; }, "err"); });
      } }));
      if (c.processing_error) badges.appendChild(el("span", { class: "b-chip warn", text: c.processing_error }));
      if (c.in_library) badges.appendChild(el("span", { class: "b-chip path", text: function () { return I18n.t("ui.c18f5840e535"); } }));
      else if (c.in_buffer) badges.appendChild(el("span", { class: "b-chip warn", text: function () { return I18n.t("ui.a3311364c3a8"); } }));
      if (promoted) badges.appendChild(el("span", { class: "b-chip ok", text: function () { return I18n.t("ui.47c8c2e7cd40"); } }));
      if (c.promote_state === "running" || c.promote_state === "queued") {
        badges.appendChild(el("span", { class: "b-chip warn", text: function () { return I18n.t("ui.694b71bc8013"); } }));
      }

      var row = el("div", { class: "buffer-item" }, [
        el("div", { class: "b-head" }, [
          el("span", { class: "b-title", text: c.title || c.arxiv_id }),
          el("span", { class: "b-score", text: (c.relevance_score || 0).toFixed(2) }),
        ]),
        el("div", { class: "b-meta", text: [(c.authors || []).slice(0, 3).join(", "),
          (c.published || "").slice(0, 10), c.arxiv_id].filter(Boolean).join(" · ") }),
        badges,
        el("div", { class: "b-reason", text: function () { return languageNotice(c.output_language); } }),
        ops,
        el("div", { class: "b-detail hidden" }),
      ]);
      var reason = el("div", { class: "b-reason" }); setMd(reason, c.relevance_reason || ""); row.insertBefore(reason, ops);
      box.appendChild(row);
    });
  }

  function relevanceLabel(value) {
    var zh = { get strong() { return I18n.t("ui.52ec36274dee"); }, get related() { return I18n.t("ui.7dfa43a32757"); }, get uncertain() { return I18n.t("ui.ccf9206892f8"); }, get unrelated() { return I18n.t("ui.d3892c577fc7"); } };
    var en = { strong: "Highly related", related: "Related", uncertain: "Uncertain", unrelated: "Unrelated" };
    return (state.outputLanguage === "en" ? en : zh)[value] || I18n.t("ui.7d73bb99923b");
  }
  function previewRelated(c, row) {
    var detail = row.querySelector(".b-detail");
    if (!detail.classList.contains("hidden")) { detail.classList.add("hidden"); return; }
    replaceChildren(detail);
    detail.appendChild(el("div", { class: "b-sec", text: function () { return I18n.t("ui.d09a36f538a0"); } }));
    detail.appendChild(el("div", { class: "b-text", text: c.seed_title || "—" }));
    detail.appendChild(el("div", { class: "b-sec", text: function () { return I18n.t("ui.2037bcfdea8d"); } }));
    var rationale = el("div"); setMd(rationale, c.relevance_reason || "—"); detail.appendChild(rationale);
    detail.appendChild(el("p", { class: "hint", text: function () { return languageNotice(c.summary_language); } }));
    var sourceLabels = { reference: [I18n.t("ui.37e527a45cd7"), "Reference lookup"], topic: [I18n.t("ui.937c6ce33b75"), "Topic expansion"],
      title: [I18n.t("ui.80a0f6798e54"), "Title search"], arxiv_id: [I18n.t("ui.1d4569d9489a"), "arXiv ID lookup"], id: [I18n.t("ui.1d4569d9489a"), "arXiv ID lookup"] };
    (c.query_sources || []).forEach(function (source) {
      var label = sourceLabels[source.kind] || [I18n.t("ui.c68479c3575f"), "Search source"], seed = source.seed || {};
      var query = source.query || source.arxiv_id || seed.title || seed.arxiv_id || (seed.terms || []).join(" / ");
      detail.appendChild(el("p", { class: "hint", text: function () { return lang(label[0], label[1]) + ": " + query; } }));
    });
    detail.appendChild(el("div", { class: "b-sec",
      text: function () { return I18n.t("ui.711db2c132d0"); } }));
    var md = el("div", { class: "md" });
    setMd(md, c.summary_text || I18n.t("ui.a5520ba281f8"));
    detail.appendChild(md);
    detail.appendChild(el("div", { class: "b-sec", text: function () { return I18n.t("ui.21c04b2eeeb4"); } }));
    detail.appendChild(el("div", { class: "b-text", text: function () { return c.abstract || I18n.t("ui.c7bcc6d27f3a"); } }));
    detail.classList.remove("hidden");
  }

  function startRelated(mode) {
    mode = typeof mode === "string" ? mode : "search";
    if (!relatedPaper) return;
    relatedPolls = 0;
    relatedPolledRun = null;
    $("r-run").disabled = true;
    $("r-cancel").classList.remove("hidden");
    $("r-progress").classList.remove("hidden");
    replaceChildren($("r-logs"), el("div", { class: "log info", text: function () { return I18n.t("ui.4235d4b140f9"); } }));
    var paperId = relatedPaper.id;
    api.post("/api/papers/" + paperId + "/related/run?mode=" + mode, {}).then(function (run) {
      if (!relatedPaper || relatedPaper.id !== paperId) return;
      relatedRunId = run.id;
      renderRelatedRun(run);
      pollRelated(run.id, false);
    }).catch(function (e) {
      toast(function () { return e.message; }, "err");
      $("r-run").disabled = false;
      $("r-cancel").classList.add("hidden");
    });
  }

  function pollRelated(runId, silent) {
    clearTimeout(relatedTimer);
    if (!relatedPaper || $("related-modal").classList.contains("hidden")) return;
    var paperId = relatedPaper.id;
    if (relatedPolledRun !== runId) { relatedPolledRun = runId; relatedPolls = 0; }
    api.get("/api/papers/" + paperId + "/related?include_discarded=" + $("r-show-discarded").checked).then(function (data) {
      if (!relatedPaper || relatedPaper.id !== paperId || relatedRunId !== runId) return;
      var run = data.run || null;
      renderRelatedRun(run);
      renderRelatedCandidates(data.candidates || [], data.counts || {});
      relatedPolls += 1;
      // stage 已 done 的 run 就算 status 还写着 running 也不再轮询（兼容历史数据）
      var running = run && run.status === "running" && run.stage !== "done";
      if (running && relatedPolls < RELATED_POLL_LIMIT) {
        relatedTimer = setTimeout(function () { pollRelated(runId, silent); }, 2000);
        return;
      }
      $("r-run").disabled = false;
      $("r-cancel").classList.add("hidden");
      if (running) {
        toast(function () { return I18n.t("ui.1c758af1c978"); }, "warn", 8000);
        return;
      }
      if (!silent && run) {
        var effStatus = (run.status === "running" && run.stage === "done") ? "done" : run.status;
        toast(function () { return I18n.t("ui.540e1df4a7a4") + statusLabel(effStatus)
          + " · " + I18n.t("ui.66fad71a37e4") + ((run.counters || {}).strong || 0)
          + (run.error ? "（" + run.error + "）" : ""); },
          effStatus === "done" ? "ok" : (effStatus === "failed" ? "err" : "warn"), 6000);
      }
    }).catch(function (e) { if (!silent) toast(function () { return e.message; }, "err"); });
  }

  function cancelRelated() {
    if (!relatedPaper) return;
    api.post("/api/papers/" + relatedPaper.id + "/related/cancel", {}).then(function () {
      toast(function () { return I18n.t("ui.f38894999039"); }, "info");
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }

  function promoteRelated(c, row) {
    askConfirm(function () { return I18n.t("ui.396530b08ab5"); }, [
      shortText(c.title || c.arxiv_id, 46),
      function () { return I18n.t("ui.e95f6cbd5563"); },
      function () { return I18n.t("ui.f017e3c8ee72"); },
    ], { okText: I18n.t("ui.66b2dbc6cbdc") }).then(function (ok) {
      if (!ok) return null;
      var badge = el("span", { class: "b-state", text: function () { return I18n.t("ui.f938cc565569"); } });
      row.querySelector(".b-ops").appendChild(badge);
      return api.post("/api/related/candidates/" + c.id + "/promote?background=true&trigger_pipeline=true", {}).then(function () {
        var timer = setInterval(function () {
          api.get("/api/related/candidates/" + c.id).then(function (full) {
            if (full.promote_state === "running" || full.promote_state === "queued") {
              I18n.bind(badge, function () { return full.promote_state === "running" ? I18n.t("ui.779f49f506ee") : I18n.t("ui.f938cc565569"); });
              return;
            }
            clearInterval(timer);
            if (full.promote_state === "done") {
              I18n.bind(badge, function () { return I18n.t("ui.150f4279f53b"); });
              if (full.promote_error) toast(function () { return I18n.t("ui.c39e94f25624") + full.promote_error; }, "warn", 9000);
              else toast(function () { return I18n.t("ui.ea6750b944fe"); }, "ok");
              loadFolders(); loadPapers(); loadRelated();
            } else {
              I18n.bind(badge, function () { return I18n.t("ui.28384d7afd2e"); });
              toast(function () { return I18n.t("ui.94a6e89ecaf3") + (full.promote_error || I18n.t("ui.13a46616e16f")); }, "err", 9000);
            }
          }).catch(function () { /* 轮询失败就下次再试 */ });
        }, 3000);
      }).catch(function (e) { badge.remove(); toast(function () { return e.message; }, "err"); });
    });
  }

  function discardRelated(c, row) {
    api.post("/api/related/candidates/" + c.id + "/discard", {}).then(function () {
      row.remove(); loadRelated(); toast(function () { return I18n.t("ui.23981a2f42a5"); }, "info");
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }

  // ---------------- 方向节点论文图谱（md + 无依赖 SVG） ----------------
  var currentGraphNodeId = null, currentGraph = null, graphZoom = 1, graphRequest = 0;
  var REL_LABEL = { get cites() { return I18n.t("ui.e0ec651f0dae"); }, get extends() { return I18n.t("ui.99a4e1e59743"); }, get improves() { return I18n.t("ui.c5d3067c551f"); }, get contradicts() { return I18n.t("ui.a86d29234503"); },
    get complements() { return I18n.t("ui.1cfecdce14e8"); }, get applies() { return I18n.t("ui.63c73c4730f4"); }, get baseline_of() { return I18n.t("ui.a7ac587ab52d"); }, get uses() { return I18n.t("ui.cdfd0b34e491"); } };

  function showNodeGraph(nodeId) {
    currentGraphNodeId = nodeId;
    hidePdfViewer();
    $("viewer-empty").classList.add("hidden");
    $("viewer-toolbar").classList.add("hidden");
    $("graph-view").classList.remove("hidden");
    loadNodeGraph(nodeId);
  }

  function hideNodeGraph() {
    currentGraphNodeId = null;
    $("graph-view").classList.add("hidden");
    // 关掉图谱后既没有图谱也没有 PDF 时，把空态露出来（否则预览区留白）
    if (!pdfvVisible()) $("viewer-empty").classList.remove("hidden");
  }

  function showViewerEmpty() {
    hidePdfViewer();
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
    var request = ++graphRequest;
    function current() { return currentGraphNodeId === nodeId && request === graphRequest; }
    replaceChildren($("graph-svg-box"), skeleton("graph", 2));
    replaceChildren($("graph-papers"));
    $("graph-legend").classList.add("hidden");
    hidePdfViewer();
    $("viewer-empty").classList.add("hidden");
    $("viewer-toolbar").classList.add("hidden");
    $("graph-view").classList.remove("hidden");
    $("graph-del").disabled = false;

    api.get("/api/knowledge/nodes/" + nodeId).then(function (payload) {
      var node = payload.node || {}, graph = payload.graph || {};
      if (!current()) return;
      I18n.write($("graph-path"), node.path || "");
      I18n.bind($("graph-meta"), function () { return [
        I18n.t("ui.6f849c07cdc5") + (node.paper_count || 0) + I18n.t("ui.1ccafea91530") + (node.subtree_paper_count || 0) + ")",
        I18n.t("ui.ed2c4e1c9c3b") + ((graph.edges || []).length),
        node.has_synthesis ? I18n.t("ui.c95eb164d78d") : I18n.t("ui.3f7bcb58e72e"),
        node.graph_md_path || I18n.t("ui.c653d6ed261a"),
      ].join("　·　"); });
      currentGraph = graph;
      var relationSelect = $("relation-filter"), paperSelect = $("relation-paper");
      replaceChildren(relationSelect, el("option", { value: "", text: function () { return I18n.t("ui.11c63c4d0d1c"); } }));
      Object.keys(REL_LABEL).forEach(function (key) { relationSelect.appendChild(el("option", { value: key, text: function () { return relationLabel(key); } })); });
      replaceChildren(paperSelect, el("option", { value: "", text: function () { return I18n.t("ui.c4fb397688ed"); } }));
      (graph.nodes || []).forEach(function (p) { paperSelect.appendChild(el("option", { value: p.id, text: p.label })); });
      renderRelations();
      renderGraphLegend(graph);
      renderGraphPapers(payload.papers || []);
      $("graph-md-link").setAttribute("href", "/reader.html?node=" + nodeId);
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });

    fetch("/api/knowledge/nodes/" + nodeId + "/graph.md")
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
      .then(function (text) { if (current()) setMd($("graph-md"), text); })
      .catch(function () { if (current()) setMd($("graph-md"), I18n.t("ui.754fab2afca4")); });
  }

  // 关系图例：把边上出现的 relation 与节点归属颜色讲清楚（否则圆和线看不懂）
  function renderGraphLegend(graph) {
    var box = $("graph-legend");
    var kinds = {};
    (graph.edges || []).forEach(function (e) { kinds[e.relation] = (kinds[e.relation] || 0) + 1; });
    var keys = Object.keys(kinds);
    replaceChildren(box);
    if (keys.length) {
      box.appendChild(el("span", { class: "lg-title", text: function () { return I18n.t("ui.253aaff19a39"); } }));
      keys.forEach(function (k) {
        box.appendChild(el("span", { class: "lg-item" }, [
          el("span", { class: "lg-line" }),
          el("span", { text: function () { return relationLabel(k) + "　" + kinds[k]; } }),
        ]));
      });
    }
    box.appendChild(el("span", { class: "lg-title", text: function () { return I18n.t("ui.71db87be72ae"); } }));
    box.classList.remove("hidden");
  }

  // 图谱下方的论文卡片：图看不清标题时，从卡片直接打开 PDF
  function renderGraphPapers(papers) {
    var box = $("graph-papers");
    replaceChildren(box);
    if (!papers.length) return;
    box.appendChild(el("div", { class: "gp-title", text: function () { return I18n.t("ui.665286f9f6fd"); } }));
    var grid = el("div", { class: "gp-grid" });
    papers.forEach(function (p) {
      var card = el("div", {
        class: "gp-card",
        title: p.reason || "",
      }, [
        el("div", { class: "gp-name", text: p.title || p.arxiv_id }),
        el("div", { class: "gp-meta", text: function () {
          var pid = (p.arxiv_id || "").indexOf("local-") === 0 ? I18n.t("ui.4d8e2b7ca915") : p.arxiv_id;
          return [pid, (p.added_at || "").slice(0, 10)].filter(Boolean).join(" · ");
        } }),
      ]);
      card.addEventListener("click", function () { openPaperById(p.id); });
      grid.appendChild(card);
    });
    box.appendChild(grid);
  }

  function openPaperById(paperId) {
    api.get("/api/papers/" + paperId).then(function (p) {
      selectPaper(p);
      toast(function () { return I18n.t("ui.1986b8015ddf") + shortText(p.title || p.arxiv_id, 30); });
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }

  // 删除当前方向节点（图谱视图里的入口）：论文保留，子方向上移一层
  function deleteCurrentGraphNode() {
    if (!currentGraphNodeId) return;
    var path = ($("graph-path").textContent || "").replace("🧭 ", "").trim();
    var nodeId = currentGraphNodeId;
    askConfirm(function () { return I18n.t("ui.079207a7b410"); }, [
      function () { return I18n.t("ui.9fad3beb4471") + path + "」？"; },
      function () { return I18n.t("ui.926a6e7bc677"); },
    ], { okText: I18n.t("ui.2f9daa828907"), danger: true }).then(function (ok) {
      if (!ok) return null;
      $("graph-del").disabled = true;
      return api.del("/api/knowledge/nodes/" + nodeId).then(function () {
        toast(function () { return I18n.t("ui.14f80dfb3d98") + path + "」"; }, "ok");
        if (currentGraphNodeId === nodeId) restoreViewerAfterGraph();
        return loadFolders();
      }).then(loadPapers).catch(function (e) {
        toast(function () { return e.message; }, "err");
        $("graph-del").disabled = false;
      });
    });
  }

  function relationLabel(value) {
    var en = { cites: "Cites", extends: "Extends", improves: "Improves", contradicts: "Contradicts", complements: "Complements", applies: "Applies", baseline_of: "Baseline of", uses: "Uses" };
    return (state.outputLanguage === "en" ? en : REL_LABEL)[value] || I18n.t("ui.4e7796b47148");
  }
  function renderRelations() {
    if (!currentGraph) return;
    var graph = currentGraph, byId = {}, filter = $("relation-filter").value, focus = Number($("relation-paper").value);
    (graph.nodes || []).forEach(function (p) { byId[p.id] = p; });
    var edges = (graph.edges || []).filter(function (edge) {
      return (!filter || edge.relation === filter) && (!focus || edge.source === focus || edge.target === focus);
    });
    var box = $("relation-cards"); replaceChildren(box);
    if (!edges.length) box.appendChild(el("p", { class: "hint", text: function () { return I18n.t("ui.c06dea6ab401"); } }));
    edges.forEach(function (edge) {
      var source = byId[edge.source], target = byId[edge.target];
      if (!source || !target) return;
      var card = el("section", { class: "relation-card" });
      card.appendChild(el("div", { class: "relation-title" }, [
        el("button", { class: "mini", text: source.label, onclick: function () { openPaperById(source.id); } }),
        el("strong", { text: function () { return " → " + relationLabel(edge.relation) + " → "; } }),
        el("button", { class: "mini", text: target.label, onclick: function () { openPaperById(target.id); } })]));
      var translated = edge.translation && edge.translation.output_language === state.outputLanguage;
      var rationale = el("div"); setMd(rationale, translated ? edge.translation.content_md : edge.rationale || ""); card.appendChild(rationale);
      var original = el("details"), originalBody = el("div");
      original.appendChild(el("summary", { text: function () { return I18n.t("ui.43ead8d80d9e"); } }));
      setMd(originalBody, (edge.rationale || "") + "\n\n" + (edge.evidence || "")); original.appendChild(originalBody);
      if (translated) card.appendChild(original);
      else if (edge.evidence) { var evidence = el("blockquote"); setMd(evidence, edge.evidence); card.appendChild(evidence); }
      card.appendChild(el("div", { class: "hint", text: function () { return translated ? I18n.t("ui.13160f00f68e") : languageNotice(edge.output_language); } }));
      if (!translated && edge.output_language !== state.outputLanguage) card.appendChild(el("button", { class: "mini", text: function () { return I18n.t("ui.69aab5c61455"); }, onclick: function () {
        api.post("/api/translations/relation/" + edge.id).then(function (data) { edge.translation = data; renderRelations(); }).catch(function (e) { toast(function () { return e.message; }, "err"); });
      } }));
      box.appendChild(card);
    });
    var ns = "http://www.w3.org/2000/svg";
    function svgEl(name, attrs, text) {
      var element = document.createElementNS(ns, name);
      Object.keys(attrs || {}).forEach(function (key) { element.setAttribute(key, attrs[key]); });
      if (text) I18n.write(element, text); return element;
    }
    var height = Math.max(180, Math.ceil((graph.nodes || []).length / 2) * 130 + 40);
    var svg = svgEl("svg", { viewBox: "0 0 660 " + height, width: 660 * graphZoom, height: height * graphZoom, class: "graph-svg" });
    var positions = {};
    (graph.nodes || []).forEach(function (p, i) { positions[p.id] = { x: 30 + (i % 2) * 330, y: 24 + Math.floor(i / 2) * 130 }; });
    edges.forEach(function (edge) {
      var a = positions[edge.source], b = positions[edge.target]; if (!a || !b) return;
      var line = svgEl("path", { d: "M" + (a.x + 130) + " " + (a.y + 36) + " L" + (b.x + 130) + " " + (b.y + 36), class: "relation-edge" + (focus ? " active" : "") });
      line.appendChild(svgEl("title", {}, relationLabel(edge.relation) + ": " + edge.rationale)); svg.appendChild(line);
    });
    (graph.nodes || []).forEach(function (p) {
      var pos = positions[p.id], group = svgEl("g", { class: "relation-node" + (p.id === focus ? " selected" : ""), tabindex: "0", role: "button" });
      group.appendChild(svgEl("rect", { x: pos.x, y: pos.y, width: 265, height: 76, rx: 9 }));
      group.appendChild(svgEl("title", {}, p.label));
      group.appendChild(svgEl("text", { x: pos.x + 12, y: pos.y + 24 }, shortText(p.label || "", 32)));
      group.appendChild(svgEl("text", { x: pos.x + 12, y: pos.y + 46 }, shortText((p.label || "").slice(31), 32)));
      group.appendChild(svgEl("text", { x: pos.x + 12, y: pos.y + 64 }, p.year || ""));
      function select() { $("relation-paper").value = String(p.id); renderRelations(); }
      group.addEventListener("click", select); group.addEventListener("keydown", function (e) { if (e.key === "Enter") select(); });
      svg.appendChild(group);
    });
    replaceChildren($("graph-svg-box"), svg);
  }

  function shortText(text, n) {
    text = String(text || "");
    return text.length > n ? text.slice(0, n - 1) + "…" : text;
  }

  // ---------------- 阅读产物（快速总结 / 深度精读）页签 ----------------
  // 产物由知识网络 pipeline 生成并落在 paper_artifacts 表；这里把“对话 / 快速总结 / 深度精读”
  // 做成对话栏里的三个视图，避免跑完 pipeline 后产物无处可看。
  var ARTIFACT_META = {
    summary: { get title() { return I18n.t("ui.2d97cc22a742"); }, steps: ["summary"] },
    deep_reading: { get title() { return I18n.t("ui.ce677ccceef5"); }, steps: ["deep"] },
  };
  var STEP_TO_ARTIFACT = { summary: "summary", deep: "deep_reading" };
  var artifactView = "chat";        // chat | summary | deep_reading
  var artifactText = "";            // 当前展示的 Markdown 原文（供复制/新窗口）
  var artifactPaperId = null, artifactVersion = null, artifactRequest = 0;

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
      if (badge) I18n.bind(badge, function () { return info ? shortChars(info.chars) : I18n.t("ui.278e5034a88e"); });
    });
  }

  function shortChars(chars) {
    chars = Number(chars) || 0;
    return (chars >= 1000 ? (chars / 1000).toFixed(1) + "k" : chars) + I18n.t("ui.c55038b272b1");
  }

  // 拉取该论文已生成的产物列表（页签角标用），顺带记住哪些是刚生成的（高亮）
  function loadPaperArtifacts(paper) {
    if (!paper) { setArtifactTabs({}); return Promise.resolve({}); }
    artifactPaperId = paper.id;
    return api.get("/api/papers/" + paper.id + "/knowledge").then(function (data) {
      if (!state.currentPaper || state.currentPaper.id !== paper.id) return null;
      var meta = {};
      ((data && data.artifacts) || []).forEach(function (a) {
        if (!ARTIFACT_META[a.kind]) return;
        meta[a.kind] = { chars: a.chars, updated_at: a.updated_at, model: a.model,
                         reasoning_effort: a.reasoning_effort };
      });
      state.artifacts = meta;
      setArtifactTabs(meta);
      return meta;
    }).catch(function () { if (state.currentPaper && state.currentPaper.id === paper.id) setArtifactTabs({}); return {}; });
  }

  function showChatView() {
    document.body.classList.remove("reading-focus");
    artifactView = "chat";
    artifactRequest++; artifactVersion = null;
    artifactText = "";
    $("artifact-view").classList.add("hidden");
    $("messages").classList.remove("hidden");
    setArtifactTabs(state.artifacts || {});
  }

  function openArtifact(kind, version) {
    var paper = state.currentPaper;
    var info = ARTIFACT_META[kind];
    if (!paper || !info) return;
    artifactView = kind; artifactVersion = version || null; artifactText = "";
    var request = ++artifactRequest;
    function current() { return request === artifactRequest && state.currentPaper && state.currentPaper.id === paper.id && artifactView === kind; }
    var versions = $("av-version");
    replaceChildren(versions, el("option", { value: "", text: function () { return I18n.t("ui.c2a497830ef7"); } })); versions.disabled = true;
    api.get("/api/papers/" + paper.id + "/artifacts/" + kind + "/versions").then(function (rows) {
      if (!current()) return;
      rows.forEach(function (row) {
        versions.appendChild(el("option", { value: String(row.id), text: function () { return I18n.t("ui.d609c36f4a6a") + row.id + " · " + ((row.stats || {}).output_language || "unknown") + " · " + row.created_at; } }));
      });
      versions.value = version ? String(version) : ""; versions.disabled = !rows.length;
    }).catch(function (e) { if (current()) toast(function () { return e.message; }, "err"); });
    $("messages").classList.add("hidden");
    $("artifact-view").classList.remove("hidden");
    setArtifactTabs(state.artifacts || {});
    document.querySelector('.art-tab[data-view="' + kind + '"]').classList.remove("attn");
    if (state.artifacts) state.artifacts[kind] = state.artifacts[kind] || {};
    if (state.artifacts && state.artifacts[kind]) state.artifacts[kind].attn = false;

    var body = $("av-body");
    replaceChildren(body, skeleton("rows", 3));
    I18n.bind($("av-title"), function () { return (kind === "summary" ? "🧾 " : "📖 ") + info.title; });
    I18n.bind($("av-meta"), function () { return I18n.t("ui.4927a53bcc88"); });
    $("artifact-view").setAttribute("data-kind", kind);

    api.get("/api/papers/" + paper.id + "/artifacts/" + kind + (version ? "?version=" + version : "")).then(function (data) {
      if (!current()) return;
      artifactText = data.content_md || "";
      var producedLanguage = (data.stats || {}).output_language || "unknown";
      I18n.bind($("av-meta"), function () {
        return [languageNotice(producedLanguage), shortChars(data.chars), (data.updated_at || "").slice(0, 16).replace("T", " "),
          data.model || "", data.reasoning_effort ? ("think:" + data.reasoning_effort) : ""].filter(Boolean).join(" · ");
      });
      setMd(body, artifactText);
      if (data.reasoning_md) {              // 仅当配置开启落库推理时才有内容
        var block = makeReasoningBlock(body);
        renderReasoning(block, data.reasoning_md, false);
      }
      body.scrollTop = 0;
    }).catch(function (e) {
      if (!current()) return;
      artifactText = "";
      I18n.bind($("av-meta"), function () { return I18n.t("ui.6c985bcd62dc"); });
      replaceChildren(body, emptyState("🧠", info.title,
        function () { return I18n.t("ui.e030e374bf92"); }));
      var tip = el("button", { class: "mini primary av-run", text: function () { return I18n.t("ui.a1b7f83de414"); },
        onclick: function () { runPaperPipeline(paper, false, info.steps); } });
      body.firstChild.appendChild(tip);
      if (String(e.message).indexOf(I18n.t("ui.d2342efd86fa")) < 0) toast(function () { return e.message; }, "err");
    });
  }

  function copyArtifact() {
    if (!artifactText) { toast(function () { return I18n.t("ui.44d6db2fbb4c"); }, "warn"); return; }
    var done = function () { toast(function () { return I18n.t("ui.410a272129c3"); }, "ok"); };
    Desktop.copy(artifactText).then(done, function () { legacyCopy(artifactText, done); });
  }

  function legacyCopy(text, done) {
    var ta = el("textarea", { style: "position:fixed;top:-1000px;opacity:0" });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); }
    catch (e) { toast(function () { return I18n.t("ui.cbc8618279a5"); }, "err"); }
    ta.remove();
  }

  // ---------------- 知识网络 pipeline（对库内任一论文一键运行） ----------------
  // 与「加入详细阅读」共用四步阅读流程，综述从文件夹手动生成。
  // 区别只是论文已在库里：不需要下载与归属，直接跑五步并实时显示进度。
  var PIPELINE_STEPS = [
    { name: "summary", get title() { return I18n.t("ui.2d97cc22a742"); } },
    { name: "tree", get title() { return I18n.t("ui.7c31f59ebde3"); } },
    { name: "deep", get title() { return I18n.t("ui.ce677ccceef5"); } },
    { name: "graph", get title() { return I18n.t("ui.f12aaa18f68f"); } },
  ];
  function pipelineState(status) { return status ? statusLabel(status) : ""; }
  function fallbackPipelineSteps() {
    var en = { summary: "Quick summary", tree: "Classification", deep: "Deep reading", graph: "Relationships" };
    return PIPELINE_STEPS.map(function (s) { return { name: s.name, get title() { return lang(s.title, en[s.name]); } }; });
  }
  var pipelineRunning = false;
  var pipelinePaperId = null;
  var pipelineSteps = null;      // 步骤名与标题由后端 /api/knowledge/steps 提供（本地列表仅兜底）

  function loadPipelineSteps() {
    if (pipelineSteps) return Promise.resolve(pipelineSteps);
    return api.get("/api/knowledge/steps").then(function (data) {
      var byName = {};
      (data.steps || []).forEach(function (s) { byName[s.name] = s.title; });
      pipelineSteps = fallbackPipelineSteps().map(function (s) {
        return { name: s.name, get title() { return I18n.t("stage." + s.name); } };
      });
      return pipelineSteps;
    }).catch(function () {
      pipelineSteps = fallbackPipelineSteps();
      return pipelineSteps;
    });
  }

  function renderPipelinePanel(paper, status, steps, subtitle) {
    var box = $("pipeline-panel");
    replaceChildren(box);
    box.appendChild(el("div", { class: "pp-head" }, [
      el("span", { class: "pp-title",
        text: function () { return I18n.t("ui.815badf7d047") + shortText(paper.title || paper.arxiv_id, 24)
          + (subtitle ? "（" + subtitle + "）" : ""); } }),
      el("span", { class: "pp-state" + (status ? " " + status : ""), text: function () { return pipelineState(status); } }),
      status === "running"
        ? el("button", { class: "mini pp-stop danger", title: function () { return I18n.t("ui.611a5a937a3d"); },
            text: function () { return I18n.t("ui.4808d729a1c3"); }, onclick: function () { stopPipeline(); } })
        : null,
      el("button", { class: "mini pp-close", title: function () { return I18n.t("ui.90bb34c2f1de"); }, text: "✕",
        onclick: function () { if (!pipelineRunning) $("pipeline-panel").classList.add("hidden"); } }),
    ]));
    var list = el("div", { class: "pp-steps" });
    (steps || fallbackPipelineSteps()).forEach(function (s) {
      list.appendChild(el("div", { class: "pp-step todo", "data-step": s.name }, [
        el("span", { class: "pp-dot", text: "○" }),
        el("span", { class: "pp-name", text: function () { return I18n.t("stage." + s.name); } }),
        el("span", { class: "pp-detail", text: "" }),
      ]));
    });
    box.appendChild(list);
    box.classList.remove("hidden");
  }

  var PP_MARK = { done: "✓", running: "◐", skipped: "–", failed: "■", todo: "○" };

  function setPipelineState(status) {
    var chip = $("pipeline-panel").querySelector(".pp-state");
    if (chip) { chip.className = "pp-state " + (status || ""); I18n.bind(chip, function () { return pipelineState(status); }); }
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
      I18n.write(runningRow.querySelector(".pp-dot"), "■");
      I18n.bind(runningRow.querySelector(".pp-detail"), function () { return I18n.t("ui.71e03cd890f4"); });
    }
    panel.querySelectorAll(".pp-step.todo").forEach(function (row) {
      row.className = "pp-step skipped";
      I18n.write(row.querySelector(".pp-dot"), "–");
      I18n.bind(row.querySelector(".pp-detail"), function () { return I18n.t("ui.71e03cd890f4"); });
    });
    toast(function () { return I18n.t("ui.8aed6548a59d"); }, "warn", 6000);
  }

  function updatePipelineStep(step, status, detail, extra) {
    var row = $("pipeline-panel").querySelector('.pp-step[data-step="' + step + '"]');
    if (!row) return;
    row.className = "pp-step " + status;
    I18n.write(row.querySelector(".pp-dot"), PP_MARK[status] || "○");
    I18n.bind(row.querySelector(".pp-name"), function () { return I18n.t("stage." + step); });
    var text = "";
    if (extra && extra.elapsed_s !== undefined && extra.elapsed_s !== null) {
      text += (text ? "　" : "") + Math.round(extra.elapsed_s) + "s";
    }
    if (extra && extra.usage && extra.usage.total_tokens) {
      text += (text ? " · " : "") + extra.usage.total_tokens + " tokens";
    }
    var target = row.querySelector(".pp-detail");
    replaceChildren(target, el("span", { text: function () { return statusLabel(status) + text; } }));
    if (detail) target.appendChild(el("details", {}, [
      el("summary", { text: function () { return I18n.t("log.original"); } }), el("div", { text: detail })
    ]));
  }

  function openPipelineDialog(paper) {
    paper = paper || state.currentPaper;
    if (!paper) { toast(function () { return I18n.t("ui.7912dc9e9b8b"); }, "err"); return; }
    loadPipelineSteps().then(function (steps) {
      return askChoice(function () { return I18n.t("ui.901034152f20"); }, [
        function () { return shortText(paper.title || paper.arxiv_id, 44) + ": " + steps.map(function (s) { return I18n.t("stage." + s.name); }).join(" → "); },
        function () { return I18n.t("ui.18f4736c8abb"); },
        function () { return I18n.t("ui.048b1181d596"); },
      ], [
        { text: function () { return I18n.t("ui.1a471a2283ce"); }, value: "run" },
        { text: function () { return I18n.t("ui.5a3f4d156660"); }, value: "force" },
      ]);
    }).then(function (choice) {
      if (!choice) return;
      runPaperPipeline(paper, choice === "force");
    });
  }

  function runPaperPipeline(paper, force, onlySteps) {
    if (pipelineRunning) { toast(function () { return I18n.t("ui.2c5d0ca1517f"); }, "warn"); return; }
    var allSteps = pipelineSteps || fallbackPipelineSteps();
    var subset = (onlySteps && onlySteps.length)
      ? allSteps.filter(function (s) { return onlySteps.indexOf(s.name) >= 0; })
      : allSteps;
    var subtitle = (onlySteps && onlySteps.length && ARTIFACT_META[STEP_TO_ARTIFACT[onlySteps[0]]])
      ? I18n.t("ui.5714758766c8") + ARTIFACT_META[STEP_TO_ARTIFACT[onlySteps[0]]].title : "";
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
        toast(function () { return I18n.t("ui.6c9df36adf8c") + I18n.value(msg); }, "err", 8000);
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
            I18n.write(row.querySelector(".pp-dot"), "–");
            I18n.bind(row.querySelector(".pp-detail"), function () { return I18n.t("ui.71e03cd890f4"); });
          });
          toast(function () { return I18n.t("ui.53ba45fb3fef") + done; }, "warn", 6000);
          refresh();
          loadPaperArtifacts(paper);
          return;
        }
        setPipelineState(ok ? "done" : "failed");
        refresh();
        // 产物刷新后直接打开，避免“跑完了但找不到输出”
        loadPaperArtifacts(paper).then(function (meta) {
          if (!state.currentPaper || state.currentPaper.id !== paper.id) return;
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
            toast(function () { return I18n.t("ui.97f2f7671059") + ARTIFACT_META[target].title; }, "ok", 7000);
          } else if (ok) {
            toast(function () { return I18n.t("ui.24ebe725ede7") + done; }, "ok", 5000);
          } else {
            toast(function () { return I18n.t("ui.c47052338c96"); }, "warn", 8000);
          }
        });
      },
    });
  }

  // ---------------- PDF 查看器（服务端逐页渲染 + 原生 JS 缩放） ----------------
  // 为什么不用浏览器内置 <iframe> viewer：它跑在跨源上下文里，外部无法控制缩放；
  // 项目约定前端零第三方依赖（不引 CDN 版 PDF.js），因此改为服务端 PyMuPDF 把每页
  // 渲染成 PNG，前端自绘可缩放查看器：
  //   滚轮向上放大 / 向下缩小（默认），触控板捏合（ctrl+wheel）缩放，按住拖动平移；
  //   工具栏可把滚轮切换为“滚动”（此时捏合与 Ctrl+滚轮仍缩放）。
  var PDFV_PT_TO_PX = 96 / 72;             // PDF point -> CSS px（zoom=1 即实际尺寸）
  var PDFV_MIN_REL = 0.5;                  // 相对“适应宽度”的最小缩放
  var PDFV_MAX_REL = 6.0;
  var pdfView = {
    paperId: null, pages: [], zoom: 1, fitZoom: 1, basePx: 0, wheelZoom: true,
    anchorY: null, pendingFactor: 1, rafPending: false,
  };

  function pdfvVisible() { return !$("pdf-viewer").classList.contains("hidden"); }

  function showPdfViewer(paper) {
    pdfView.paperId = paper.id;
    pdfView.pages = [];
    pdfView.zoom = 1;
    pdfView.fitZoom = 1;
    pdfView.pendingFactor = 1;
    $("pdfv-native").setAttribute("href", "/api/papers/" + paper.id + "/pdf");
    $("pdf-viewer").classList.remove("hidden");
    $("viewer-empty").classList.add("hidden");
    var pagesBox = $("pdfv-pages");
    replaceChildren(pagesBox, skeleton("cards", 3));
    I18n.write($("pdfv-pageno"), "…");
    api.get("/api/papers/" + paper.id + "/pages").then(function (data) {
      if (pdfView.paperId !== paper.id) return;      // 加载期间用户已切走
      pdfView.pages = data.pages || [];
      buildPdfPages();
      fitPdfWidth(false);
    }).catch(function (e) {
      if (pdfView.paperId !== paper.id) return;
      replaceChildren(pagesBox, emptyState("📄", function () { return I18n.t("ui.b937d8263bfb"); }, function () { return e.message; }));
      I18n.write($("pdfv-pageno"), "");
    });
  }

  function hidePdfViewer() { $("pdf-viewer").classList.add("hidden"); }

  function buildPdfPages() {
    var box = $("pdfv-pages");
    replaceChildren(box);
    var maxW = 612;
    pdfView.pages.forEach(function (p) { maxW = Math.max(maxW, p.width || 612); });
    pdfView.basePx = maxW * PDFV_PT_TO_PX;
    pdfView.pages.forEach(function (p) {
      var widthPt = p.width || 612, heightPt = p.height || 792;
      var wpx = Math.round(widthPt * PDFV_PT_TO_PX);
      var wrapEl = el("div", {
        class: "pdfv-page", "data-page": String(p.page),
        style: "--vw: " + wpx + "px; aspect-ratio: " + widthPt + " / " + heightPt,
      });
      var img = el("img", {
        src: "/api/papers/" + pdfView.paperId + "/page/" + p.page + "?scale=2",
        alt: function () { return I18n.t("ui.9552fe3bd366") + p.page + I18n.t("ui.85ca81cf6abd"); }, loading: "lazy", draggable: "false",
      });
      img.addEventListener("error", function () {
        img.remove();
        wrapEl.appendChild(el("div", { class: "pdfv-err",
          text: function () { return I18n.t("ui.9552fe3bd366") + p.page + I18n.t("ui.19f143966b64"); } }));
      });
      wrapEl.appendChild(img);
      box.appendChild(wrapEl);
    });
    box.style.setProperty("--pdfv-zoom", String(pdfView.zoom));
    updatePdfZoomLabel();
    updatePdfPageLabel();
  }

  function pdfvContentHeight() { return $("pdfv-pages").getBoundingClientRect().height; }

  function clampPdfZoom(zoom) {
    var min = pdfView.fitZoom * PDFV_MIN_REL;
    var max = Math.max(pdfView.fitZoom * PDFV_MAX_REL, 3);
    return Math.max(min, Math.min(max, zoom));
  }

  /** 缩放并保持锚点处的内容位置不变（滚轮向上放大 / 向下缩小都走这里）。 */
  function applyPdfZoom(factor, anchorY) {
    if (!pdfView.pages.length) return;
    var scroll = $("pdfv-scroll");
    var oldZoom = pdfView.zoom;
    var newZoom = clampPdfZoom(oldZoom * factor);
    if (Math.abs(newZoom - oldZoom) < 1e-4) return;
    var ay = (anchorY === null || anchorY === undefined) ? scroll.clientHeight / 2 : anchorY;
    var oldRatio = (scroll.scrollTop + ay) / Math.max(1, pdfvContentHeight());
    pdfView.zoom = newZoom;
    $("pdfv-pages").style.setProperty("--pdfv-zoom", String(newZoom));
    scroll.scrollTop = Math.max(0, oldRatio * Math.max(1, pdfvContentHeight()) - ay);
    updatePdfZoomLabel();
  }

  function fitPdfWidth(scrollToTop) {
    var scroll = $("pdfv-scroll");
    if (!pdfView.basePx || !scroll.clientWidth) return;
    var avail = Math.max(120, scroll.clientWidth - 26);
    pdfView.fitZoom = Math.max(0.1, Math.min(5, avail / pdfView.basePx));
    pdfView.zoom = pdfView.fitZoom;
    $("pdfv-pages").style.setProperty("--pdfv-zoom", String(pdfView.zoom));
    if (scrollToTop) scroll.scrollTop = 0;
    updatePdfZoomLabel();
    updatePdfPageLabel();
  }

  function updatePdfZoomLabel() {
    var label = $("pdfv-label");
    if (label) I18n.write(label, Math.round(pdfView.zoom * 100) + "%");
  }

  function updatePdfPageLabel() {
    var box = $("pdfv-pageno");
    if (!box) return;
    var total = pdfView.pages.length;
    if (!total) { I18n.write(box, ""); return; }
    var scroll = $("pdfv-scroll");
    var pages = scroll.querySelectorAll(".pdfv-page");
    var viewTop = scroll.getBoundingClientRect().top;
    var current = 1;
    for (var i = 0; i < pages.length; i++) {
      var rect = pages[i].getBoundingClientRect();
      if (rect.top - viewTop <= scroll.clientHeight * 0.45) current = i + 1;
      else break;
    }
    I18n.write(box, current + " / " + total);
  }

  // 滚轮 / 触控板捏合：ctrl+wheel 是浏览器对捏合的标准化事件；
  // 默认模式下普通滚轮也缩放（需求：向上放大、向下缩小），可在工具栏切回滚动。
  function onPdfvWheel(e) {
    if (!pdfvVisible()) return;
    var zoomIntent = e.ctrlKey || pdfView.wheelZoom;
    if (!zoomIntent) return;                 // 滚动模式：交还浏览器默认滚动
    e.preventDefault();
    var rect = $("pdfv-scroll").getBoundingClientRect();
    pdfView.anchorY = e.clientY - rect.top;
    pdfView.pendingFactor *= Math.pow(1.0016, -e.deltaY);
    if (pdfView.rafPending) return;
    pdfView.rafPending = true;
    requestAnimationFrame(function () {
      pdfView.rafPending = false;
      var factor = pdfView.pendingFactor;
      pdfView.pendingFactor = 1;
      applyPdfZoom(factor, pdfView.anchorY);
    });
  }

  var pdfvScrollTimer = null;
  function onPdfvScroll() {
    if (pdfvScrollTimer) return;
    pdfvScrollTimer = setTimeout(function () {
      pdfvScrollTimer = null;
      updatePdfPageLabel();
    }, 120);
  }

  // 按住拖动平移（滚轮被绑为缩放后，拖拽是主要的快速翻阅方式）
  var pdfvDrag = null;
  function beginPdfvDrag(e) {
    if (e.button !== 0 || !pdfvVisible()) return;
    var scroll = $("pdfv-scroll");
    pdfvDrag = { x: e.clientX, y: e.clientY, left: scroll.scrollLeft, top: scroll.scrollTop };
    scroll.classList.add("dragging");
    window.addEventListener("pointermove", movePdfvDrag);
    window.addEventListener("pointerup", endPdfvDrag);
    e.preventDefault();
  }
  function movePdfvDrag(e) {
    if (!pdfvDrag) return;
    var scroll = $("pdfv-scroll");
    scroll.scrollLeft = pdfvDrag.left - (e.clientX - pdfvDrag.x);
    scroll.scrollTop = pdfvDrag.top - (e.clientY - pdfvDrag.y);
  }
  function endPdfvDrag() {
    if (!pdfvDrag) return;
    pdfvDrag = null;
    $("pdfv-scroll").classList.remove("dragging");
    window.removeEventListener("pointermove", movePdfvDrag);
    window.removeEventListener("pointerup", endPdfvDrag);
  }

  function togglePdfvWheel() {
    pdfView.wheelZoom = !pdfView.wheelZoom;
    var btn = $("pdfv-wheel");
    btn.classList.toggle("on", pdfView.wheelZoom);
    I18n.bind(btn, function () { return pdfView.wheelZoom ? I18n.t("ui.1319f2cd3495") : I18n.t("ui.31f236936f09"); });
    toast(function () { return pdfView.wheelZoom
      ? I18n.t("ui.d066c4098c2b")
      : I18n.t("ui.c1c6d2e289eb"); }, "info", 4200);
  }

  function initPdfvEvents() {
    var scroll = $("pdfv-scroll");
    if (!scroll) return;
    scroll.addEventListener("wheel", onPdfvWheel, { passive: false });
    scroll.addEventListener("scroll", onPdfvScroll);
    scroll.addEventListener("pointerdown", beginPdfvDrag);
    if (typeof ResizeObserver !== "undefined") {
      // 栏宽拖拽不触发 window.resize：处于“适应宽度”时跟随容器宽度重新适配
      new ResizeObserver(function () {
        if (!pdfvVisible() || !pdfView.basePx) return;
        if (Math.abs(pdfView.zoom - pdfView.fitZoom) < 0.002) fitPdfWidth(false);
      }).observe(scroll);
    }
  }

  // ---------------- 论文列表 ----------------
  function loadPapers() {
    var qs = [];
    if (state.selectedFolderId !== null) qs.push("folder_id=" + state.selectedFolderId + "&recursive=" + !state.onlyCurrent);
    if (state.search) qs.push("q=" + encodeURIComponent(state.search));
    var url = "/api/papers" + (qs.length ? "?" + qs.join("&") : "");
    state.papersLoading = true; renderPapers();
    return api.get(url).then(function (list) {
      state.papers = list; state.papersLoading = false; renderPapers();
      I18n.write($("all-count"), state.selectedFolderId === null ? list.length : $("all-count").textContent);
    }).catch(function (e) { state.papersLoading = false; renderPapers(); toast(function () { return e.message; }, "err"); });
  }
  function renderPapers() {
    var box = $("paper-list");
    if (state.papersLoading) { replaceChildren(box, skeleton("cards", 4)); return; }
    replaceChildren(box);
    if (!state.papers.length) {
      box.appendChild(state.search
        ? emptyState("🔎", function () { return I18n.t("ui.8b0c166a01a9"); }, function () { return I18n.t("ui.94b41044cb4d"); })
        : emptyState("📄", function () { return I18n.t("ui.177ae5989203"); },
            function () { return I18n.t("ui.24c1450de450"); }));
      return;
    }
    state.papers.forEach(function (p) {
      var badges = el("div", { class: "p-meta" });
      (p.categories || []).slice(0, 3).forEach(function (c) { badges.appendChild(el("span", { class: "badge", text: c })); });
      if (p.folder_name) badges.appendChild(el("span", {
        class: "badge folder" + (p.folder_is_direction ? " direction" : ""),
        text: p.folder_name,
      }));
      if (p.text_truncated) badges.appendChild(el("span", { class: "badge trunc", text: function () { return I18n.t("ui.4fb9aa6ebc50"); } }));
      if (!p.has_text) badges.appendChild(el("span", { class: "badge notext", text: function () { return I18n.t("ui.394aa37b1840"); } }));
      var ops = el("div", { class: "p-ops" }, [
        el("button", { title: function () { return I18n.t("ui.95b8cf2c1641"); }, text: "🧠",
          onclick: function (e) { e.stopPropagation(); openPipelineDialog(p); } }),
        el("button", { title: function () { return I18n.t("ui.711c5507708d"); }, text: "📁", onclick: function (e) { e.stopPropagation(); openMoveModal(p); } }),
        el("button", { class: "del", title: function () { return I18n.t("ui.b5c234015cf4"); }, text: "🗑", onclick: function (e) { e.stopPropagation(); deletePaper(p); } }),
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
    if (!state.currentPaper || state.currentPaper.id !== p.id) {
      stopActiveStream(); state.currentSessionId = null; state.sessions = [];
      artifactRequest++; artifactText = "";
    }
    hideNodeGraph();
    state.currentPaper = p; renderPapers();
    showPdfViewer(p);
    $("viewer-toolbar").classList.remove("hidden");
    I18n.write($("viewer-title"), p.title || p.arxiv_id);
    I18n.write($("chat-title"), p.title || p.arxiv_id);
    updateCtxStatus(p);
    loadSessions();
    // 阅读产物页签：换论文就重新拉一次，已生成的直接可看
    loadPaperArtifacts(p).then(function (meta) {
      if (!state.currentPaper || state.currentPaper.id !== p.id) return;
      if (artifactView !== "chat" && meta && meta[artifactView]) openArtifact(artifactView);
      else if (artifactView !== "chat") showChatView();
    });
  }

  // 上下文状态：明确告知用户“全文/仅摘要/已截断”，避免默默降级
  function updateCtxStatus(p) {
    var box = $("ctx-status");
    if (!p) { box.className = "ctx-status hidden"; I18n.write(box, ""); return; }
    box.classList.remove("hidden");
    replaceChildren(box);
    if (p.has_text) {
      box.className = "ctx-status ok";
      var n = (p.text_chars || 0).toLocaleString();
      box.appendChild(el("span", { text: function () { return I18n.t("ui.f75d1b2e9616") + n + I18n.t("ui.41e795320fb9"); } }));
      if (p.text_truncated) box.appendChild(el("span", { text: function () { return I18n.t("ui.8c768b791bee"); } }));
    } else {
      box.className = "ctx-status warn";
      box.appendChild(el("span", { text: function () { return I18n.t("ui.962d7c4ad434"); } }));
    }
  }

  // ---------------- 添加论文 ----------------
  function addPaper() {
    var url = $("new-url").value.trim();
    if (!url) { toast(function () { return I18n.t("ui.2701bc368e10"); }, "err"); return; }
    var btn = $("add-btn"); btn.disabled = true; I18n.bind(btn, function () { return I18n.t("ui.e6172f1efa34"); });
    var folderVal = $("target-folder").value;
    var body = { url: url };
    if (folderVal) body.folder_id = parseInt(folderVal, 10);
    api.post("/api/papers/from-arxiv", body).then(function (paper) {
      toast(function () { return I18n.t("ui.240b9846ec24") + (paper.title || paper.arxiv_id); }, "ok");
      $("new-url").value = "";
      return Promise.all([loadFolders(), loadPapers()]).then(function () { selectPaper(paper); });
    }).catch(function (e) { toast(function () { return I18n.t("ui.bfabc7837ea6") + e.message; }, "err"); })
      .then(function () { btn.disabled = false; I18n.bind(btn, function () { return I18n.t("ui.7a8a11ead507"); }); });
  }

  // 上传本地 PDF：multipart 表单直连，复用目标文件夹下拉的选择
  function uploadPdf(file) {
    if (!file) return;
    var btn = $("upload-btn"); btn.disabled = true;
    I18n.bind(btn, function () { return I18n.t("ui.9a4c1e7fb205"); });
    var fd = new FormData();
    fd.append("file", file);
    var folderVal = $("target-folder").value;
    if (folderVal) fd.append("folder_id", folderVal);
    fetch("/api/papers/upload", { method: "POST", body: fd }).then(function (r) {
      return r.text().then(function (txt) {
        var data = txt ? JSON.parse(txt) : null;
        if (!r.ok) throw I18n.apiError(data, r.status);
        return data;
      });
    }).then(function (paper) {
      toast(function () { return I18n.t("ui.240b9846ec24") + (paper.title || paper.arxiv_id); }, "ok");
      return Promise.all([loadFolders(), loadPapers()]).then(function () { selectPaper(paper); });
    }).catch(function (e) { toast(function () { return I18n.t("ui.bfabc7837ea6") + e.message; }, "err"); })
      .then(function () { btn.disabled = false; I18n.bind(btn, function () { return I18n.t("ui.1c7f3a9bd2e4"); }); });
  }

  // ---------------- 论文操作：移动 / 删除 / 重抽全文 ----------------
  var movingPaper = null;
  function openMoveModal(p) {
    movingPaper = p;
    renderFolderSelects();
    I18n.write($("move-paper-title"), p.title || p.arxiv_id);
    var sel = $("move-folder");
    sel.value = p.folder_id != null ? String(p.folder_id) : "";
    $("move-modal").classList.remove("hidden");
  }
  function confirmMove() {
    if (!movingPaper) return;
    var val = $("move-folder").value;
    if (!val) { toast(function () { return I18n.t("ui.7383d2b307f9"); }, "err"); return; }
    var fid = parseInt(val, 10);
    api.patch("/api/papers/" + movingPaper.id, { folder_id: fid }).then(function (updated) {
      $("move-modal").classList.add("hidden");
      toast(function () { return I18n.t("ui.87e677e8ece8") + (updated.folder_name || ""); }, "ok");
      if (state.currentPaper && state.currentPaper.id === updated.id) state.currentPaper = updated;
      movingPaper = null;
      return Promise.all([loadFolders(), loadPapers()]);
    }).catch(function (e) { toast(function () { return I18n.t("ui.5f2542f5d237") + e.message; }, "err"); });
  }
  function deletePaper(p) {
    askConfirm(function () { return I18n.t("ui.63f99a3c2164"); }, [
      "《" + (p.title || p.arxiv_id) + "》",
      function () { return I18n.t("ui.e227bb274b10"); },
    ], { okText: I18n.t("ui.2f9daa828907"), danger: true }).then(function (ok) {
      if (!ok) return null;
      return api.del("/api/papers/" + p.id).then(function () {
        toast(function () { return I18n.t("ui.077a6d37719a"); }, "ok");
        if (state.currentPaper && state.currentPaper.id === p.id) clearCurrentPaper();
        return Promise.all([loadFolders(), loadPapers()]);
      }).catch(function (e) { toast(function () { return I18n.t("ui.0580fcee37e0") + e.message; }, "err"); });
    });
  }
  function clearCurrentPaper() {
    state.currentPaper = null; state.sessions = []; state.currentSessionId = null;
    state.artifacts = {};
    hidePdfViewer();
    replaceChildren($("pdfv-pages"));
    pdfView.paperId = null; pdfView.pages = [];
    $("viewer-empty").classList.remove("hidden");
    $("viewer-toolbar").classList.add("hidden");
    I18n.bind($("chat-title"), function () { return I18n.t("ui.e63a45a5fc1e"); });
    replaceChildren($("session-select"));
    $("pipeline-panel").classList.add("hidden");
    showChatView();
    updateCtxStatus(null);
    renderMessages([]);
  }
  function reextractPaper() {
    var p = state.currentPaper; if (!p) { toast(function () { return I18n.t("ui.7912dc9e9b8b"); }, "err"); return; }
    var btn = $("reextract-paper"); btn.disabled = true; var old = btn.textContent; I18n.bind(btn, function () { return I18n.t("ui.285d723101d5"); });
    api.post("/api/papers/" + p.id + "/reextract").then(function (updated) {
      state.currentPaper = updated;
      updateCtxStatus(updated);
      loadPapers();
      toast(function () { return updated.has_text ? (I18n.t("ui.2529cfe0f6de") + (updated.text_chars || 0).toLocaleString() + I18n.t("ui.340d059fcaae")) : I18n.t("ui.28ad7b4c4e98"); }, "ok");
    }).catch(function (e) { toast(function () { return I18n.t("ui.8bf743903bbf") + e.message; }, "err"); })
      .then(function () { btn.disabled = false; I18n.write(btn, old); });
  }

  // ---------------- 对话 ----------------
  function loadSessions(preserveMessages) {
    var p = state.currentPaper; if (!p) return Promise.resolve();
    return api.get("/api/papers/" + p.id + "/sessions").then(function (list) {
      if (!state.currentPaper || state.currentPaper.id !== p.id) return;
      state.sessions = list;
      var sel = $("session-select"); sel.innerHTML = "";
      list.forEach(function (s) { sel.appendChild(el("option", { value: String(s.id), text: function () { return s.title || (I18n.t("ui.64d26a4dd05e") + s.id); } })); });
      var selected = list.find(function (s) { return s.id === state.currentSessionId; }) || list[0];
      if (selected) {
        state.currentSessionId = selected.id; sel.value = String(selected.id);
        if (!preserveMessages) return loadMessages();
      } else { state.currentSessionId = null; if (!preserveMessages) renderMessages([]); }
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function newSession() {
    if (state.streaming) return;
    var p = state.currentPaper; if (!p) { toast(function () { return I18n.t("ui.7912dc9e9b8b"); }, "err"); return; }
    api.post("/api/papers/" + p.id + "/sessions").then(function (s) {
      if (!state.currentPaper || state.currentPaper.id !== p.id) return;
      return loadSessions(true).then(function () {
        if (!state.currentPaper || state.currentPaper.id !== p.id) return;
        state.currentSessionId = s.id; $("session-select").value = String(s.id); renderMessages([]);
      });
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function loadMessages() {
    if (!state.currentSessionId) { renderMessages([]); return; }
    var sid = state.currentSessionId;
    return api.get("/api/sessions/" + sid + "/messages")
      .then(function (messages) { if (state.currentSessionId === sid && !state.streaming) renderMessages(messages); })
      .catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function renderMessages(msgs) {
    var box = $("messages"); replaceChildren(box);
    if (!msgs || !msgs.length) {
      box.appendChild(emptyState("💬", function () { return I18n.t("ui.f5c43014f0a5"); },
        function () { return I18n.t("ui.05536ddcdd77"); }));
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
    if (!state.currentPaper) { toast(function () { return I18n.t("ui.7912dc9e9b8b"); }, "err"); return; }
    if (artifactView !== "chat") showChatView();   // 在产物阅读视图里提问时，自动回到对话
    var paperId = state.currentPaper.id;
    state.streaming = true; setSendButton(true);
    var start = function (sid) {
      if (!state.currentPaper || state.currentPaper.id !== paperId) { state.streaming = false; setSendButton(false); return; }
      state.currentSessionId = sid;
      input.value = "";
      var box = $("messages");
      if (box.querySelector(".empty")) replaceChildren(box);
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
        if (raw) setMd(answer, aborted ? (raw + I18n.t("ui.c50a64657f2a")) : raw);
        if (reasonBlock) { renderReasoning(reasonBlock, rawReasoning, false); reasonBlock.wrap.open = false; }
        state.streaming = false; setSendButton(false);
        loadSessions(true);
      };
      streamChat(sid, content, {
        kind: "chat",
        onDelta: function (delta, reasoning) {
          var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
          if (reasoning) {
            if (!reasonBlock) { reasonBlock = makeReasoningBlock(ai); reasonBlock.wrap.open = true; }
            rawReasoning += reasoning;
            renderReasoning(reasonBlock, rawReasoning, true);
          }
          if (delta) { raw += delta; setMd(answer, raw, true); }
          if (nearBottom) requestAnimationFrame(function () { box.scrollTop = box.scrollHeight; });
        },
        onDone: function () { finalize(false); },
        onCancelled: function () { finalize(true); toast(function () { return I18n.t("ui.459a568188e8"); }, "warn", 5000); },
        onAborted: function () { finalize(true); toast(function () { return I18n.t("ui.459a568188e8"); }, "warn", 5000); },
        onError: function (errMsg) {
          ai.classList.remove("pending");
          if (!raw && !rawReasoning) ai.remove();
          box.appendChild(el("div", { class: "msg error", text: function () { return I18n.t("ui.cfa7c64f924a") + I18n.value(errMsg); } }));
          state.streaming = false; setSendButton(false);
        },
      });
    };

    if (state.currentSessionId) start(state.currentSessionId);
    else api.post("/api/papers/" + state.currentPaper.id + "/sessions").then(function (s) {
      if (!state.currentPaper || state.currentPaper.id !== paperId) { state.streaming = false; setSendButton(false); return; }
      state.sessions.unshift(s);
      var sel = $("session-select");
      sel.appendChild(el("option", { value: String(s.id), text: function () { return I18n.t("stage." + s.name); } }));
      sel.value = String(s.id);
      start(s.id);
    }).catch(function (e) { state.streaming = false; setSendButton(false); toast(function () { return e.message; }, "err"); });
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
        if (!res || res.stopped === false) toast(function () { return I18n.t("ui.e44176e69469"); }, "info");
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
    var stopped = false;
    activeStream = { token: token, controller: controller, kind: (handlers && handlers.kind) || "stream",
                     onAborted: function () { stopped = true; if (handlers.onAborted) handlers.onAborted(); } };
    var finish = function () { if (activeStream && activeStream.token === token) activeStream = null; };
    fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), signal: controller ? controller.signal : undefined,
    }).then(function (resp) {
      if (stopped) { if (resp.body) resp.body.cancel(); return; }
      if (!resp.ok || !resp.body) return resp.json().catch(function () { return {}; }).then(function (data) {
              finish(); var error = I18n.apiError(data, resp.status); handlers.onError(function () { return error.message; });
            });
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = "";
      var finished = false;
      function pump() {
        return reader.read().then(function (res) {
          if (stopped) { reader.cancel(); return; }
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
            else if (payloadObj.type === "error") { finished = true; finish();
                          var errorCode = payloadObj.code || "error.502", errorParams = payloadObj.params;
                          handlers.onError(function () { return I18n.t(errorCode, errorParams); }); reader.cancel(); return;
                        }
            else if (payloadObj.type === "done") { finished = true; finish(); handlers.onDone(payloadObj); return; }
          }
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      finish();
      // abort 是主动行为，交给 onAborted 处理，不当成错误
      if (stopped) return;
      if (e && (e.name === "AbortError" || String(e.message || "").indexOf("aborted") >= 0)) return;
      handlers.onError(function () { return I18n.t("error.502"); });
    });
  }

  function streamChat(sessionId, content, handlers) {
    return streamSSE("/api/sessions/" + sessionId + "/messages", { content: content }, handlers);
  }

  // 发送按钮在流式中变为「停止」（终止本次生成，已生成内容保留）
  function setSendButton(streaming) {
    var btn = $("send-btn");
    btn.classList.toggle("danger", !!streaming);
    I18n.bind(btn, function () { return streaming ? I18n.t("ui.c422f8ba6b25") : I18n.t("ui.edecf0ae6e51"); });
    $("new-session").disabled = !!streaming; $("session-select").disabled = !!streaming;
    btn.disabled = false;
    I18n.bind(btn, function () { return streaming ? I18n.t("ui.5d7ccf8e00d5") : ""; }, "title");
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
    if (!paper) { toast(function () { return I18n.t("ui.7912dc9e9b8b"); }, "err"); return Promise.resolve(); }
    if (artifactView !== "chat") showChatView();
    var box = $("messages");
    if (box.querySelector(".empty")) replaceChildren(box);
    var asks = el("div", { class: "msg user" }); setMd(asks, I18n.t("ui.024a673fc140") + skillName);
    box.appendChild(asks);
    var ai = el("div", { class: "msg assistant pending" });
    var answer = el("div", { class: "answer" }); ai.appendChild(answer);
    box.appendChild(ai); box.scrollTop = box.scrollHeight;
    var raw = "", rawReasoning = "", block = null;
    state.streaming = true; $("send-btn").disabled = true;
    return streamSSE("/api/papers/" + paper.id + "/skills/" + skillName + "?stream=true", {},
      {
        onDelta: function (delta, reasoning) {
          var nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
          if (reasoning) {
            if (!block) { block = makeReasoningBlock(ai); block.wrap.open = true; }
            rawReasoning += reasoning;
            renderReasoning(block, rawReasoning, true);
          }
          if (delta) { raw += delta; setMd(answer, raw, true); }
          if (nearBottom) requestAnimationFrame(function () { box.scrollTop = box.scrollHeight; });
        },
        onDone: function (payload) {
          ai.classList.remove("pending");
          setMd(answer, raw || (payload && payload.output) || "");
          if (block) { renderReasoning(block, rawReasoning, false); block.wrap.open = false; }
          state.streaming = false; $("send-btn").disabled = false;
          toast(function () { return I18n.t("ui.5f0394777472") + skillName; }, "info");
        },
        onError: function (msg) {
          ai.classList.remove("pending");
          box.appendChild(el("div", { class: "msg error", text: function () { return I18n.t("ui.b915bde73559") + I18n.value(msg); } }));
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
    I18n.bind(btn, function () { return on ? I18n.t("ui.8b4422ef5254") : I18n.t("ui.df3d58c7d84b"); });
    I18n.bind(btn, function () { return on ? I18n.t("ui.f5703ebd69c5") : I18n.t("ui.686f48206d7c"); }, "title");
  }

  function checkLLMConfigured(quiet) {
    return api.get("/api/settings").then(function (s) {
      applyLanguage(s.output_language || "zh");
      var ok = !!(s.base_url && s.model && s.has_api_key);
      markSettingsAttention(!ok);
      if (!ok && !quiet) {
        toast(function () { return I18n.t("ui.d8eda049f0dc"); }, "warn", 8000);
        openSettings();
      }
      return ok;
    }).catch(function () { return true; });
  }

  function openSettings() {
    api.get("/api/settings").then(function (s) {
      $("set-language").value = s.output_language || "zh";
      $("set-base-url").value = s.base_url || "";
      $("set-model").value = s.model || "";
      $("set-temperature").value = s.temperature;
      $("set-max-tokens").value = s.max_tokens;
      $("set-top-p").value = s.top_p;
      $("set-reasoning").value = s.reasoning_effort || "";
      $("set-timeout").value = s.request_timeout;
      $("set-retries").value = s.max_retries;
      if (window.Desktop) window.Desktop.storageInfo();
      $("set-api-key").value = "";
      I18n.bind($("key-hint"), function () { return s.has_api_key ? (I18n.t("ui.a49a423c7014") + s.api_key_preview) : I18n.t("ui.080d8395d826"); });
      I18n.write($("settings-test-result"), "");
      $("settings-modal").classList.remove("hidden");
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function collectSettings() {
    var body = {
      output_language: $("set-language").value,
      base_url: $("set-base-url").value.trim(),
      model: $("set-model").value.trim(),
      temperature: $("set-temperature").value === "" ? "" : Number($("set-temperature").value),
      max_tokens: $("set-max-tokens").value === "" ? "" : Number($("set-max-tokens").value),
      top_p: $("set-top-p").value === "" ? "" : Number($("set-top-p").value),
      reasoning_effort: $("set-reasoning").value,
      request_timeout: Number($("set-timeout").value),
      max_retries: Number($("set-retries").value),
    };
    var key = $("set-api-key").value.trim();
    if (key) body.api_key = key;   // 留空则不修改
    return body;
  }
  function saveSettings(silent) {
    return api.put("/api/settings", collectSettings()).then(function (s) {
      I18n.bind($("key-hint"), function () { return s.has_api_key ? (I18n.t("ui.a49a423c7014") + s.api_key_preview) : I18n.t("ui.080d8395d826"); });
      $("set-api-key").value = "";
      markSettingsAttention(!(s.base_url && s.model && s.has_api_key));
      applyLanguage(s.output_language || "zh");
      if (!silent) toast(function () { return I18n.t("ui.bd03c0aad701"); }, "ok");
      return s;
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function testLLM() {
    var r = $("settings-test-result"); r.className = "hint"; I18n.bind(r, function () { return I18n.t("ui.105a9082c346"); });
    $("test-llm").disabled = true;
    api.post("/api/settings/test", collectSettings()).then(function (res) {
      r.className = "hint " + (res.ok ? "ok" : "err");
      I18n.bind(r, function () { return (res.ok ? "✓ " : "✗ ") + I18n.t(res.code, res.params); });
    }).catch(function (e) { r.className = "hint err"; I18n.bind(r, function () { return "✗ " + e.message; }); })
      .finally(function () { $("test-llm").disabled = false; });
  }

  // ---------------- 规则 ----------------
  function openRules() { renderFolderSelects(); loadRules(); $("rules-modal").classList.remove("hidden"); }
  function loadRules() {
    api.get("/api/rules").then(function (rules) {
      var box = $("rule-list"); replaceChildren(box);
      if (!rules.length) { box.appendChild(el("div", { class: "empty", text: function () { return I18n.t("ui.7c8b25b3a0e1"); } })); return; }
      rules.forEach(function (r) {
        var fname = r.folder_name || I18n.t("ui.ccb91ce53df9");
        var item = el("div", { class: "rule-item" + (r.enabled ? "" : " disabled") }, [
          el("div", { class: "r-main" }, [
            el("span", { class: "r-type", text: r.match_type }),
            el("span", { class: "r-pat", text: " " + r.pattern }),
            el("span", { class: "r-folder", text: "  → " + fname + " · p" + r.priority + (r.name ? (" · " + r.name) : "") }),
          ]),
          el("div", { class: "r-ops" }, [
            el("button", { class: "mini", text: function () { return r.enabled ? I18n.t("ui.4e6fd0e28c55") : I18n.t("ui.f4f0ead1116b"); }, onclick: function () { toggleRule(r); } }),
            el("button", { class: "mini", text: function () { return I18n.t("ui.2f9daa828907"); }, onclick: function () { deleteRule(r); } }),
          ]),
        ]);
        box.appendChild(item);
      });
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function addRule() {
    var pattern = $("rule-pattern").value.trim();
    if (!pattern) { toast(function () { return I18n.t("ui.31ce6a2cda6f"); }, "err"); return; }
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
      loadRules(); toast(function () { return I18n.t("ui.1c52c093fe6b"); }, "ok");
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function toggleRule(r) { api.patch("/api/rules/" + r.id, { enabled: !r.enabled }).then(loadRules).catch(function (e) { toast(function () { return e.message; }, "err"); }); }
  function deleteRule(r) {
    askConfirm(function () { return I18n.t("ui.c02fdcf208b3"); }, [function () { return I18n.t("ui.9fad3beb4471") + (r.name || (r.match_type + " " + r.pattern)) + "」？"; }],
      { okText: I18n.t("ui.2f9daa828907"), danger: true }).then(function (ok) {
      if (!ok) return null;
      return api.del("/api/rules/" + r.id).then(loadRules).catch(function (e) { toast(function () { return e.message; }, "err"); });
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

  // ---------------- 语言与手动文件夹报告 ----------------
  function languageNotice(value) {
    if (!value || value === "unknown") return I18n.t("ui.0d8669a15090");
    return value === state.outputLanguage ? I18n.t("ui.2fbb97fb2134") : lang(I18n.t("ui.f22efe590133") + value + I18n.t("ui.f7d52ae7bbac"), "Content language: " + value + "; regenerate if needed");
  }
  function applyLanguage(value) {
    state.outputLanguage = value === "en" ? "en" : "zh";
    I18n.set(state.outputLanguage);
    document.documentElement.lang = state.outputLanguage;
    $("output-language").value = state.outputLanguage;
    $("set-language").value = state.outputLanguage;
    pipelineSteps = null;
    document.querySelectorAll(".art-tab").forEach(function (tab) {
      var kind = tab.getAttribute("data-view");
      I18n.bind(tab.firstChild, function () { return kind === "chat" ? I18n.t("ui.6bf6d3f3d626") : ARTIFACT_META[kind].title; });
    });
    setArtifactTabs(state.artifacts || {});
    setSendButton(state.streaming);
  }
  function downloadMarkdown(text, name) {
    if (window.Desktop && window.Desktop.available()) return window.Desktop.saveMarkdown(text, name);
    var url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
    var link = el("a", { href: url, download: name }); link.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  function statusLabel(value) {
    var labels = { queued: [I18n.t("ui.d6f766f2adf5"), "Queued"], running: [I18n.t("ui.1f0eb99b7ed0"), "Running"], done: [I18n.t("ui.f28461bb49c8"), "Completed"],
      failed: [I18n.t("ui.28384d7afd2e"), "Failed"], cancelled: [I18n.t("ui.a37778f17c5f"), "Cancelled"], interrupted: [I18n.t("ui.88cf2dc00c34"), "Interrupted"] };
    return labels[value] ? lang(labels[value][0], labels[value][1]) : (I18n.messages["state." + value] ? I18n.t("state." + value) : value || "");
  }
  var reviewFolder = null, selectedReview = null, reviewTimer = null, reviewRequest = 0;
  function openReview(folder, historyOnly) {
    clearTimeout(reviewTimer); selectedReview = null; reviewFolder = folder || null;
    var request = ++reviewRequest;
    $("review-modal").classList.remove("hidden");
    I18n.bind($("review-heading"), function () { return folder ? (folder.path || folder.name) : I18n.t("ui.c0e397128de3"); });
    $("review-form").classList.toggle("hidden", !folder || !!historyOnly);
    I18n.write($("review-progress"), ""); replaceChildren($("review-actions")); replaceChildren($("review-content"));
    $("review-title").value = ""; $("review-focus").value = ""; $("review-language").value = state.outputLanguage;
    if (folder && !historyOnly) {
      $("review-create").disabled = true;
      I18n.bind($("review-preview"), function () { return I18n.t("ui.6c76b58964bb"); });
      api.get("/api/folders/" + folder.id + "/review-preview").then(function (data) {
        if (request !== reviewRequest) return;
        var coverage = data.coverage || {};
        I18n.bind($("review-preview"), function () { return data.path + " · " + data.paper_count + I18n.t("ui.8fc1ca802fc5")
          + " · " + I18n.t("ui.13403a4640f7") + [coverage.notes, coverage.full_text, coverage.abstract_only].join(" / ")
          + " · " + I18n.t("ui.6ce287827294") + data.estimated_evidence_batches
          + " · " + I18n.t("ui.853ec0583f0d") + data.estimated_min_calls; });
        $("review-create").disabled = !data.paper_count;
      }).catch(function (e) { if (request === reviewRequest) I18n.bind($("review-preview"), function () { return e.message; }); });
    }
    loadReviewHistory();
  }
  function loadReviewHistory() {
    var request = reviewRequest;
    return api.get("/api/reviews" + (reviewFolder ? "?folder_id=" + reviewFolder.id : "")).then(function (rows) {
      if (request !== reviewRequest) return;
      var box = $("review-history"); replaceChildren(box);
      rows.forEach(function (row) {
        box.appendChild(el("button", { class: "ghost", text: function () { return row.title + " · " + statusLabel(row.status) + " · " + row.output_language + " · " + row.created_at; },
          onclick: function () { showReview(row.id); } }));
      });
      if (!rows.length) box.appendChild(el("p", { class: "hint", text: function () { return I18n.t("ui.b8c0795296f5"); } }));
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function showReview(id) {
    clearTimeout(reviewTimer); selectedReview = id;
    return api.get("/api/reviews/" + id).then(function (row) {
      if (selectedReview !== id) return;
      I18n.bind($("review-progress"), function () { return row.title + " · " + statusLabel(row.status) + " · " + languageNotice(row.output_language)
        + "\n" + (row.coverage_known ? I18n.t("ui.b72cf3834256") + row.covered_papers + "/" + row.paper_count : I18n.t("ui.5c0648cb0268"))
        + " · " + I18n.t("ui.dd111aff30ab") + row.completed_batches + "/" + row.total_batches
        + " · tokens " + ((row.usage || {}).total_tokens || 0) + (row.error ? "\n" + row.error : ""); });
      var actions = $("review-actions"); replaceChildren(actions);
      if (row.status === "running" || row.status === "queued") {
        actions.appendChild(el("button", { class: "mini danger", text: function () { return I18n.t("ui.ca4d973c0b00"); }, onclick: function () {
          api.post("/api/reviews/" + id + "/cancel").then(function () { showReview(id); }).catch(function (e) { toast(function () { return e.message; }, "err"); });
        } }));
        if (!$("review-modal").classList.contains("hidden")) reviewTimer = setTimeout(function () { showReview(id); }, 2000);
      } else if (row.status !== "done") {
        actions.appendChild(el("button", { class: "mini", text: function () { return I18n.t("ui.72de91a211af"); }, onclick: function () {
          api.post("/api/reviews/" + id + "/resume").then(function () { showReview(id); }).catch(function (e) { toast(function () { return e.message; }, "err"); });
        } }));
      }
      if (row.content_md) {
        actions.appendChild(el("a", { class: "mini", href: "/reader.html?review=" + id, target: "_blank", rel: "noopener", text: function () { return I18n.t("ui.180d3f4b2f14"); } }));
        actions.appendChild(el("a", { class: "mini", href: "/api/reviews/" + id + "/export", text: function () { return I18n.t("ui.6aa9c09687c9"); } }));
      }
      setMd($("review-content"), row.content_md || "");
      if (row.status !== "running" && row.status !== "queued") loadReviewHistory();
    }).catch(function (e) { toast(function () { return e.message; }, "err"); });
  }
  function bindReadingUpgrade() {
    $("folder-search").addEventListener("input", function () { state.folderSearch = this.value.trim().toLowerCase(); renderFolderTree(); });
    $("only-current").addEventListener("change", function () { state.onlyCurrent = this.checked; loadPapers(); });
    $("folder-review").addEventListener("click", function () { openReview(state.folderMap[state.selectedFolderId]); });
    $("open-reviews").addEventListener("click", function () { openReview(null, true); });
    $("output-language").addEventListener("change", function () {
      var value = this.value;
      api.put("/api/settings", { output_language: value }).then(function (s) {
        applyLanguage(s.output_language);
        toast(function () { return I18n.t("ui.89d26e589c4a"); }, "ok");
      }).catch(function (e) { $("output-language").value = state.outputLanguage; toast(function () { return e.message; }, "err"); });
    });
    $("av-version").addEventListener("change", function () { openArtifact(artifactView, Number(this.value) || null); });
    $("av-focus").addEventListener("click", function () { document.body.classList.toggle("reading-focus"); });
    $("av-download").addEventListener("click", function () { downloadMarkdown(artifactText, artifactView + ".md"); });
    $("review-create").addEventListener("click", function () {
      if (!reviewFolder) return;
      this.disabled = true;
      var request = reviewRequest;
      api.post("/api/folders/" + reviewFolder.id + "/reviews", { title: $("review-title").value,
        focus: $("review-focus").value, output_language: $("review-language").value }).then(function (row) {
        if (request !== reviewRequest) return;
        $("review-form").classList.add("hidden"); loadReviewHistory(); showReview(row.id);
      }).catch(function (e) { toast(function () { return e.message; }, "err"); }).finally(function () { $("review-create").disabled = false; });
    });
    $("r-continue").addEventListener("click", function () { startRelated("continue"); });
    $("r-retry").addEventListener("click", function () { startRelated("retry"); });
    $("r-show-discarded").addEventListener("change", loadRelated);
    $("graph-toggle").addEventListener("click", function () { $("graph-svg-box").classList.toggle("hidden"); });
    $("relation-filter").addEventListener("change", renderRelations);
    $("relation-paper").addEventListener("change", renderRelations);
    $("graph-zoom-in").addEventListener("click", function () { graphZoom = Math.min(3, graphZoom * 1.2); renderRelations(); });
    $("graph-zoom-out").addEventListener("click", function () { graphZoom = Math.max(.5, graphZoom / 1.2); renderRelations(); });
    $("graph-reset").addEventListener("click", function () { graphZoom = 1; $("relation-paper").value = ""; $("relation-filter").value = ""; renderRelations(); });
  }

  // ---------------- 事件绑定 ----------------
  function bind() {
    bindReadingUpgrade();
    $("add-btn").addEventListener("click", addPaper);
    $("new-url").addEventListener("keyup", function (e) { if (e.key === "Enter") addPaper(); });
    $("upload-btn").addEventListener("click", function () { $("pdf-upload-input").click(); });
    $("pdf-upload-input").addEventListener("change", function (e) {
      var f = e.target.files && e.target.files[0];
      e.target.value = "";   // 允许连续选择同一文件（后端会对重复内容报 409）
      uploadPdf(f);
    });
    $("folder-all").addEventListener("click", function () { selectFolder(null); });
    $("new-folder").addEventListener("click", function () {
      askText(function () { return I18n.t("ui.fb641318480e"); }, "", function () { return I18n.t("ui.7d24c4ee6a1f"); }).then(function (name) {
        if (!name) return null;
        return api.post("/api/folders", { name: name, parent_id: null }).then(loadFolders)
          .catch(function (e) { toast(function () { return e.message; }, "err"); });
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

    // PDF 查看器：缩放按钮 / 滚轮与拖拽监听
    if ($("pdfv-in")) $("pdfv-in").addEventListener("click", function () { applyPdfZoom(1.25, null); });
    if ($("pdfv-out")) $("pdfv-out").addEventListener("click", function () { applyPdfZoom(1 / 1.25, null); });
    if ($("pdfv-fit")) $("pdfv-fit").addEventListener("click", function () { fitPdfWidth(false); });
    if ($("pdfv-label")) $("pdfv-label").addEventListener("click", function () { fitPdfWidth(false); });
    if ($("pdfv-wheel")) $("pdfv-wheel").addEventListener("click", togglePdfvWheel);
    initPdfvEvents();

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
      window.Desktop.openReader("/reader.html?paper=" + state.currentPaper.id + "&kind=" + kind + (artifactVersion ? "&version=" + artifactVersion : ""));
    });
    $("av-regen").addEventListener("click", function () {
      var kind = $("artifact-view").getAttribute("data-kind") || artifactView;
      var info = ARTIFACT_META[kind];
      if (!info || !state.currentPaper) return;
      askConfirm(function () { return I18n.t("ui.90c6d515e59a") + info.title; }, [
        shortText(state.currentPaper.title || state.currentPaper.arxiv_id, 40),
        function () { return I18n.t("ui.898e5141af63"); },
        function () { return I18n.t("ui.82d6dc5546cc"); },
      ], { okText: I18n.t("ui.71fde5e6250c") }).then(function (ok) {
        if (ok) runPaperPipeline(state.currentPaper, true, info.steps);
      });
    });

    // 检索页与图谱
    if ($("open-search")) $("open-search").addEventListener("click", openSearch);
    if ($("s-run")) $("s-run").addEventListener("click", startSearch);
    if ($("s-cancel")) $("s-cancel").addEventListener("click", cancelSearch);
    if ($("s-select-all")) $("s-select-all").addEventListener("change", function () {
      toggleSelectAllBuffer($("s-select-all").checked);
    });
    if ($("s-batch-reject")) $("s-batch-reject").addEventListener("click", rejectSelectedBuffer);

    // 智能关联检索（预览工具栏入口 + 弹窗内操作）
    if ($("run-related")) $("run-related").addEventListener("click", function () { openRelated(state.currentPaper); });
    if ($("r-run")) $("r-run").addEventListener("click", startRelated);
    if ($("r-cancel")) $("r-cancel").addEventListener("click", cancelRelated);
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
    bind();
    window.addEventListener("reader-language", function (event) {
      if (state.outputLanguage !== event.detail) applyLanguage(event.detail);
    });
    ReaderPreferences.init().then(function () {
      applyWidths(); applyVisibility();
      return checkLLMConfigured(false);
    }).then(loadFolders).then(loadPapers).then(function () {
      var id = new URLSearchParams(location.search).get("paper");
      if (/^\d+$/.test(id || "")) openPaperById(Number(id));
    }).catch(function (e) { toast(function () { return I18n.t("ui.d1106aabd242") + e.message; }, "err"); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();