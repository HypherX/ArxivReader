/* 全文翻译（pdf2zh）：阅读器工具栏入口 + 翻译弹窗 + PDF 版本切换。
 *
 * 后端是 app/pdftranslate（pdf2zh_next 引擎，迁移自 zotero-pdf2zh）：
 *   - 引擎按需安装到独立环境（设置里可配翻译服务 / 模型 / 语言 / QPS）；
 *   - 弹窗里选输出方式（中英对照 dual / 仅译文 mono / 两份都要）、对照排版（LR/TB）、页范围；
 *   - 任务在后台跑，这里轮询进度；完成后可直接在阅读器里切换 原文 / 对照版 / 译文版。
 * 划词翻译不受版本切换影响：对照版与译文版同样带文字层。
 */
(function (global) {
  "use strict";

  var state = {
    getPaper: function () { return null; },
    engine: null,          // 引擎状态缓存 {installed, version, installing, log, error}
    settings: null,        // 翻译设置缓存（含 services / languages 目录）
    pollTimer: null,       // 任务轮询
    installTimer: null,    // 安装轮询
    task: null,            // 当前在弹窗里跟踪的任务
    source: { paperId: null, kind: "original", taskId: null },
  };

  function el(id) { return document.getElementById(id); }
  function t(key, params) { return global.I18n.t(key, params); }

  function node(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (key === "text") n.textContent = value == null ? "" : String(value);
      else if (key === "class") n.className = value;
      else if (key === "href") n.href = value;
      else n.setAttribute(key, value);
    });
    (children || []).forEach(function (child) { n.appendChild(child); });
    return n;
  }

  function request(method, url, body) {
    return fetch(url, {
      method: method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw global.I18n.apiError(data, response.status);
        return data;
      });
    });
  }
  function get(url) { return request("GET", url); }
  function post(url, body) { return request("POST", url, body || {}); }
  function put(url, body) { return request("PUT", url, body || {}); }

  function showError(error) {
    var box = el("ft-error");
    box.classList.add("err");
    box.textContent = (error && error.message) || String(error || "");
  }
  function clearError() {
    var box = el("ft-error");
    box.classList.remove("err");
    box.textContent = "";
  }

  // ---------------------------------------------------------------- 引擎状态
  function renderEngine(engine) {
    state.engine = engine;
    var status = el("ft-engine-status");
    var installBtn = el("ft-install");
    var hint = el("ft-install-hint");
    status.className = "";
    if (engine.installing) {
      status.textContent = t("ui.10296eeefc10");
      installBtn.classList.add("hidden");
      hint.classList.add("hidden");
      el("ft-install-box").classList.remove("hidden");
      el("ft-install-log").textContent = (engine.log || []).join("\n");
      el("ft-run").disabled = true;
    } else if (engine.installed) {
      status.textContent = t("ui.57ea97c224a3") + (engine.version ? " · pdf2zh_next " + engine.version : "");
      installBtn.classList.add("hidden");
      hint.classList.add("hidden");
      el("ft-install-box").classList.add("hidden");
      el("ft-run").disabled = false;
      if (engine.error) showError(engine.error);
    } else {
      status.textContent = t("ui.362d828f8ee6");
      installBtn.classList.remove("hidden");
      hint.classList.remove("hidden");
      el("ft-run").disabled = true;
      if (engine.error) showError(t("ui.67df2051e5b6") + engine.error);
    }
  }

  function pollEngine() {
    stopInstallPoll();
    state.installTimer = setInterval(function () {
      get("/api/pdf-translation/engine").then(function (engine) {
        renderEngine(engine);
        if (!engine.installing) stopInstallPoll();
      }).catch(function () {});
    }, 1200);
  }

  function stopInstallPoll() {
    if (state.installTimer) { clearInterval(state.installTimer); state.installTimer = null; }
  }

  function installEngine() {
    clearError();
    post("/api/pdf-translation/engine/install").then(function () {
      return get("/api/pdf-translation/engine");
    }).then(function (engine) {
      renderEngine(engine);
      if (engine.installing) pollEngine();
    }).catch(showError);
  }

  // ---------------------------------------------------------------- 任务轮询
  function stopPoll() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  function renderTask(task) {
    state.task = task;
    var running = task.status === "queued" || task.status === "running";
    el("ft-progress-box").classList.toggle("hidden", !running && !task.progress);
    el("ft-bar").style.width = (task.progress || 0) + "%";
    var statusText = {
      queued: t("ui.d6f766f2adf5"), running: t("ui.910fe42c65eb"),
      done: t("ui.0ead1b5ba5c8"), failed: t("ui.7720ed5fffa8"),
      cancelled: t("ui.a37778f17c5f"), interrupted: t("ui.9e3a47fc0897"),
    }[task.status] || task.status;
    var msg = task.message && task.message !== statusText ? " · " + task.message : "";
    el("ft-progress-text").textContent =
      statusText + msg + (task.progress ? " · " + task.progress + "%" : "");
    el("ft-log").textContent = (task.log_tail || []).join("\n");
    el("ft-run").classList.toggle("hidden", running);
    el("ft-rerun").classList.toggle("hidden", running);
    el("ft-cancel").classList.toggle("hidden", !running);
    if (task.status === "failed") showError(t("ui.7720ed5fffa8") + (task.error || ""));
    if (task.status === "done") renderResult(task);
    renderMini(task, statusText);
  }

  // ---------------- 对话区迷你进度卡：关掉弹窗后进度仍可见，点击回到弹窗 ----------------
  var miniTimer = null;

  function renderMini(task, statusText) {
    var box = el("ft-mini");
    if (!box) return;
    var paper = state.getPaper && state.getPaper();
    var terminal = task && task.status !== "running" && task.status !== "queued";
    // 别的论文的任务、或已完成且过了展示期：收起
    if (!task || !paper || task.paper_id !== paper.id ||
        (terminal && box.dataset.until && Date.now() > Number(box.dataset.until))) {
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    box.classList.toggle("done", task.status === "done");
    box.classList.toggle("err", task.status === "failed" || task.status === "cancelled" || task.status === "interrupted");
    el("ft-mini-bar").style.width = (task.progress || 0) + "%";
    el("ft-mini-text").textContent =
      (statusText || task.status) + (task.progress && !terminal ? " · " + task.progress + "%" : "");
    clearTimeout(miniTimer);
    if (terminal) {
      // 完成/失败状态停 10 秒自动收起，不一直占着对话区
      box.dataset.until = String(Date.now() + 10000);
      miniTimer = setTimeout(function () { box.classList.add("hidden"); }, 10000);
    } else {
      delete box.dataset.until;
    }
  }

  function pollTask(taskId) {
    stopPoll();
    state.pollTimer = setInterval(function () {
      get("/api/pdf-translation/tasks/" + taskId).then(function (task) {
        renderTask(task);
        if (task.status !== "queued" && task.status !== "running") {
          stopPoll();
          refreshSwitcher();   // 产物就绪后让阅读器顶栏出现版本切换
        }
      }).catch(function () {});
    }, 1200);
  }

  function startRun() {
    clearError();
    var paper = state.getPaper();
    if (!paper) return;
    var body = {
      output_mode: el("ft-output").value,
      dual_mode: el("ft-dual-mode").value,
    };
    var pages = el("ft-pages").value.trim();
    if (pages) body.pages = pages;
    el("ft-run").disabled = true;
    post("/api/papers/" + paper.id + "/pdf-translation/run", body).then(function (task) {
      renderTask(task);
      pollTask(task.id);
    }).catch(function (e) {
      showError(e);
    }).finally(function () {
      el("ft-run").disabled = false;
    });
  }

  function cancelRun() {
    if (!state.task) return;
    post("/api/pdf-translation/tasks/" + state.task.id + "/cancel").then(renderTask).catch(showError);
  }

  // ---------------------------------------------------------------- 结果与版本切换
  function fileUrl(task, kind) {
    return "/api/pdf-translation/tasks/" + task.id + "/file/" + kind;
  }

  function renderResult(task) {
    var box = el("ft-result");
    var files = el("ft-files");
    files.innerHTML = "";
    if (!task || task.status !== "done" || (!task.has_dual && !task.has_mono)) {
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    [["dual", "ui.1b9ffd878a98", task.has_dual, task.dual_name],
     ["mono", "ui.159c76d7a551", task.has_mono, task.mono_name]].forEach(function (entry) {
      var kind = entry[0], labelKey = entry[1], exists = entry[2], name = entry[3];
      if (!exists) return;
      var row = node("div", { class: "ft-file-row" }, [
        node("span", { class: "ft-file-name", text: name, title: name }),
        node("button", { class: "mini primary", type: "button", text: t(labelKey) }),
        node("a", { class: "mini", href: fileUrl(task, kind), target: "_blank",
                    rel: "noopener", title: t("ui.ade67fee56af"), text: "↗" }),
        node("a", { class: "mini", href: fileUrl(task, kind), download: name,
                    title: t("ui.f931fe76f0a9"), text: "⬇" }),
      ]);
      row.querySelector("button").addEventListener("click", function () {
        switchSource(kind, task.id);
        closeModal();
      });
      files.appendChild(row);
    });
  }

  /** 切换阅读器里的 PDF 版本：original / dual / mono。 */
  function switchSource(kind, taskId) {
    var paper = state.getPaper();
    if (!paper) return;
    state.source = { paperId: paper.id, kind: kind, taskId: taskId || null };
    if (kind === "original" || !taskId) {
      global.PdfView.open(paper);
    } else {
      global.PdfView.open(paper, { url: fileUrl({ id: taskId }, kind) });
    }
    syncSwitcher();
  }

  function syncSwitcher() {
    var select = el("pdf-source");
    if (!select) return;
    var value = state.source.kind === "original" ? "original" : state.source.kind + ":" + state.source.taskId;
    if ([].some.call(select.options, function (o) { return o.value === value; })) select.value = value;
  }

  /** 选中论文变化：重置回原文，并按论文已完成的翻译任务刷新版本切换器。 */
  function onPaperChanged(paper) {
    stopPoll();
    state.task = null;
    var select = el("pdf-source");
    if (!paper) {
      state.source = { paperId: null, kind: "original", taskId: null };
      if (select) { select.innerHTML = ""; select.classList.add("hidden"); }
      return;
    }
    var paperChanged = state.source.paperId !== paper.id;
    if (paperChanged) state.source = { paperId: paper.id, kind: "original", taskId: null };
    get("/api/papers/" + paper.id + "/pdf-translation").then(function (view) {
      var current = state.getPaper();
      if (!current || current.id !== paper.id) return;
      fillSwitcher(view);
      // 有在跑的任务：接上轮询，对话区迷你进度卡同步可见（不用打开弹窗）
      if (view.latest && (view.latest.status === "running" || view.latest.status === "queued")) {
        state.task = view.latest;
        renderMini(view.latest);
        pollTask(view.latest.id);
      } else {
        renderMini(null);
      }
    }).catch(function () {});
  }

  function fillSwitcher(view) {
    var select = el("pdf-source");
    select.innerHTML = "";
    select.appendChild(node("option", { value: "original", text: t("ui.b249017854ee") }));
    var done = null;
    (view.tasks || []).forEach(function (task) {
      if (!done && task.status === "done" && (task.has_dual || task.has_mono)) done = task;
    });
    if (done && done.has_dual) {
      select.appendChild(node("option", { value: "dual:" + done.id, text: t("ui.051d32b068b8") }));
    }
    if (done && done.has_mono) {
      select.appendChild(node("option", { value: "mono:" + done.id, text: t("ui.969774368474") }));
    }
    select.classList.toggle("hidden", select.options.length < 2);
    syncSwitcher();
  }

  function refreshSwitcher() {
    var paper = state.getPaper();
    if (paper) onPaperChanged(paper);
  }

  function onSwitcherChange() {
    var value = el("pdf-source").value;
    if (value === "original") switchSource("original");
    else {
      var parts = value.split(":");
      switchSource(parts[0], Number(parts[1]));
    }
  }

  // ---------------------------------------------------------------- 弹窗
  function openModal() {
    var paper = state.getPaper();
    if (!paper) return;
    clearError();
    el("ft-modal").classList.remove("hidden");
    el("ft-head-hint").textContent = paper.title || paper.arxiv_id;
    el("ft-progress-box").classList.add("hidden");
    el("ft-result").classList.add("hidden");
    el("ft-log").textContent = "";
    Promise.all([
      get("/api/pdf-translation/engine"),
      get("/api/pdf-translation/settings"),
      get("/api/papers/" + paper.id + "/pdf-translation"),
    ]).then(function (results) {
      var engine = results[0], settings = results[1], view = results[2];
      renderEngine(engine);
      if (engine.installing) pollEngine();
      state.settings = settings;
      el("ft-output").value = settings.output_mode || "dual";
      el("ft-dual-mode").value = settings.dual_mode || "LR";
      syncDualModeVisibility();
      el("ft-langs").textContent = langLabel(settings.lang_in) + " → " + langLabel(settings.lang_out);
      el("ft-service-line").textContent = t("ui.f2b6506fbf03") + serviceLabel(settings.service);
      // 已有任务：在跑的接上进度，完成的直接给查看入口
      if (state.task && (state.task.status === "running" || state.task.status === "queued")
          && state.task.paper_id === paper.id) {
        renderTask(state.task);
        pollTask(state.task.id);
      } else if (view.latest) {
        renderTask(view.latest);
        if (view.latest.status === "done") renderResult(view.latest);
      } else {
        el("ft-progress-box").classList.add("hidden");
      }
    }).catch(showError);
  }

  function closeModal() {
    el("ft-modal").classList.add("hidden");
  }

  function langLabel(code) {
    var found = (state.settings && state.settings.languages || []).filter(function (l) {
      return l.code === code;
    })[0];
    return found ? found.label : code;
  }

  function serviceLabel(id) {
    var found = (state.settings && state.settings.services || []).filter(function (s) {
      return s.id === id;
    })[0];
    return found ? found.label : id;
  }

  function syncDualModeVisibility() {
    el("ft-dual-mode-wrap").style.visibility = el("ft-output").value === "mono" ? "hidden" : "visible";
  }

  // ---------------------------------------------------------------- 设置区（挂在设置弹窗里）
  function initSettings() {
    get("/api/pdf-translation/settings").then(function (settings) {
      state.settings = settings;
      var serviceSelect = el("pdftr-service");
      serviceSelect.innerHTML = "";
      settings.services.forEach(function (svc) {
        serviceSelect.appendChild(node("option", { value: svc.id, text: svc.label }));
      });
      ["pdftr-lang-in", "pdftr-lang-out"].forEach(function (id) {
        var select = el(id);
        select.innerHTML = "";
        settings.languages.forEach(function (l) {
          select.appendChild(node("option", { value: l.code, text: l.label }));
        });
      });
      fillSettings(settings);
      serviceSelect.addEventListener("change", syncServiceFields);
      el("pdftr-output").addEventListener("change", function () {
        el("pdftr-dual-mode-wrap").style.visibility =
          el("pdftr-output").value === "mono" ? "hidden" : "visible";
      });
    }).catch(function () {});
  }

  function fillSettings(settings) {
    el("pdftr-service").value = settings.service;
    el("pdftr-model").value = settings.model || "";
    el("pdftr-base-url").value = settings.base_url || "";
    el("pdftr-api-key").value = "";
    el("pdftr-lang-in").value = settings.lang_in || "en";
    el("pdftr-lang-out").value = settings.lang_out || "zh-CN";
    el("pdftr-qps").value = settings.qps || 4;
    el("pdftr-output").value = settings.output_mode || "dual";
    el("pdftr-dual-mode").value = settings.dual_mode || "LR";
    el("pdftr-dual-first").checked = !!settings.dual_translate_first;
    el("pdftr-table").checked = !!settings.translate_table_text;
    el("pdftr-ocr").checked = !!settings.ocr_workaround;
    el("pdftr-compat").checked = !!settings.enhance_compatibility;
    el("pdftr-mirror").checked = !!settings.use_mirror;
    global.I18n.bind(el("pdftr-key-hint"), function () {
      return settings.has_api_key
        ? (t("ui.a49a423c7014") + settings.api_key_preview)
        : t("ui.080d8395d826");
    });
    syncServiceFields();
    el("pdftr-dual-mode-wrap").style.visibility =
      el("pdftr-output").value === "mono" ? "hidden" : "visible";
  }

  /** 按服务显示/隐藏模型、Base URL、Key 输入框（免费服务不需要凭据）。 */
  function syncServiceFields() {
    var svc = (state.settings.services || []).filter(function (s) {
      return s.id === el("pdftr-service").value;
    })[0] || {};
    var hasModel = !!svc.default_model;
    el("pdftr-model").closest("label").classList.toggle("hidden", !hasModel);
    el("pdftr-baseurl-row").classList.toggle("hidden", !svc.needs_url && !svc.default_base_url && svc.id !== "openai");
    el("pdftr-apikey-row").classList.toggle("hidden", !svc.needs_key);
    el("pdftr-key-hint").classList.toggle("hidden", !svc.needs_key);
    if (svc.default_model) el("pdftr-model").placeholder = svc.default_model;
    if (svc.default_base_url) el("pdftr-base-url").placeholder = svc.default_base_url;
  }

  function loadSettings() {
    return get("/api/pdf-translation/settings").then(function (settings) {
      state.settings = settings;
      fillSettings(settings);
    }).catch(function () {});
  }

  function saveSettings() {
    var body = {
      service: el("pdftr-service").value,
      model: el("pdftr-model").value.trim(),
      base_url: el("pdftr-base-url").value.trim(),
      lang_in: el("pdftr-lang-in").value,
      lang_out: el("pdftr-lang-out").value,
      output_mode: el("pdftr-output").value,
      dual_mode: el("pdftr-dual-mode").value,
      dual_translate_first: el("pdftr-dual-first").checked,
      translate_table_text: el("pdftr-table").checked,
      ocr_workaround: el("pdftr-ocr").checked,
      enhance_compatibility: el("pdftr-compat").checked,
      use_mirror: el("pdftr-mirror").checked,
    };
    var qps = parseInt(el("pdftr-qps").value, 10);
    if (qps) body.qps = qps;
    var key = el("pdftr-api-key").value.trim();
    if (key) body.api_key = key;    // 留空 = 不修改（与主设置同一语义）
    return put("/api/pdf-translation/settings", body).then(function (settings) {
      state.settings = settings;
      el("pdftr-api-key").value = "";
      global.I18n.bind(el("pdftr-key-hint"), function () {
        return settings.has_api_key
          ? (t("ui.a49a423c7014") + settings.api_key_preview)
          : t("ui.080d8395d826");
      });
      return settings;
    });
  }

  // ---------------------------------------------------------------- 初始化
  function init(getPaper) {
    state.getPaper = getPaper || state.getPaper;
    el("full-translate").addEventListener("click", openModal);
    el("ft-run").addEventListener("click", startRun);
    el("ft-rerun").addEventListener("click", function () {
      el("ft-progress-box").classList.add("hidden");
      el("ft-result").classList.add("hidden");
      clearError();
    });
    el("ft-cancel").addEventListener("click", cancelRun);
    el("ft-install").addEventListener("click", installEngine);
    el("ft-open-settings").addEventListener("click", function () {
      closeModal();
      var btn = el("open-settings");
      if (btn) btn.click();
    });
    el("ft-output").addEventListener("change", syncDualModeVisibility);
    var mini = el("ft-mini");
    if (mini) {
      mini.addEventListener("click", openModal);
      mini.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openModal(); }
      });
    }
    el("pdf-source").addEventListener("change", onSwitcherChange);
    initSettings();
  }

  global.FullTranslate = {
    init: init,
    openModal: openModal,
    onPaperChanged: onPaperChanged,
    loadSettings: loadSettings,
    saveSettings: saveSettings,
    switchSource: switchSource,
  };
})(window);
