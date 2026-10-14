/* 划词翻译：选中文字 -> 浮出小动作条 -> 译文进阅读栏右侧的浅色面板。
 *
 * 译文不悬浮、不用深色：面板与整体界面共用同一套 token，可以连续翻好几段并回看，
 * 不必在浮层与正文之间来回瞄。后端按原文哈希缓存，同一段第二次点开不再调用模型。
 */
(function (global) {
  "use strict";

  var memory = Object.create(null);
  var chip = null;
  var pending = false;
  var resolvePaper = function () { return null; };

  function el(id) { return document.getElementById(id); }
  function t(key, params) { return global.I18n.t(key, params); }

  function node(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (key === "text") n.textContent = value == null ? "" : String(value);
      else if (key === "class") n.className = value;
      else n.setAttribute(key, value);
    });
    (children || []).forEach(function (child) { n.appendChild(child); });
    return n;
  }

  function request(paperId, text) {
    var key = paperId + "|" + text;
    if (memory[key]) return Promise.resolve(memory[key]);
    if (pending) return Promise.reject(new Error(t("ui.68925e0239e7")));
    pending = true;
    return fetch("/api/translations/paper/" + paperId, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw global.I18n.apiError(data, response.status);
        return data;
      });
    }).then(function (data) {
      memory[key] = data.content_md || "";
      return memory[key];
    }).finally(function () { pending = false; });
  }

  // ---------------------------------------------------------------- 译文侧栏
  function panel() { return el("translate-panel"); }

  function showPanel() {
    if (!panel()) return;
    panel().classList.remove("hidden");
  }

  function hidePanel() { panel().classList.add("hidden"); }

  function syncEmpty() {
    el("translate-empty").classList.toggle("hidden", !!el("translate-list").firstChild);
  }

  /** 一条记录：原文摘录 + 译文，最新的排在最前面。 */
  function addItem(source) {
    showPanel();
    var list = el("translate-list");
    var item = node("div", { class: "tp-item" }, [
      node("div", { class: "tp-src", text: source.length > 180 ? source.slice(0, 180) + "…" : source }),
      node("div", { class: "tp-body", text: t("ui.7dd03b4cde9c") }),
    ]);
    list.insertBefore(item, list.firstChild);
    syncEmpty();
    return item;
  }

  function translate(source) {
    var paperId = resolvePaper();
    if (!paperId || !source) return;
    var item = addItem(source);
    var body = item.querySelector(".tp-body");
    request(paperId, source).then(function (markdown) {
      body.className = "tp-body md";
      global.ReaderRenderer.set(body, markdown, { language: global.I18n.language() });
      var copy = node("button", { class: "tp-copy", type: "button", text: t("ui.63d90d977348") });
      copy.addEventListener("click", function () { global.Desktop.copy(markdown); });
      item.appendChild(copy);
    }).catch(function (error) {
      body.className = "tp-error";
      body.textContent = (error && error.message) || String(error);
    });
  }

  // ---------------------------------------------------------------- 选区动作条
  function closeChip() { if (chip) { chip.remove(); chip = null; } }

  function openChip(text, rect) {
    closeChip();
    chip = node("div", { class: "tr-chip" }, [
      node("button", { class: "tr-btn", type: "button", text: t("ui.a52a08335185") }),
      node("button", { class: "tr-btn ghost", type: "button", text: t("ui.63d90d977348") }),
    ]);
    document.body.appendChild(chip);
    var buttons = chip.querySelectorAll("button");
    buttons[0].addEventListener("click", function () { closeChip(); translate(text); });
    buttons[1].addEventListener("click", function () { global.Desktop.copy(text); closeChip(); });
    var box = chip.getBoundingClientRect();
    chip.style.left = Math.max(8, Math.min(innerWidth - box.width - 8, rect.x - box.width / 2)) + "px";
    chip.style.top = (rect.top - box.height - 8 > 8 ? rect.top - box.height - 8 : rect.bottom + 8) + "px";
  }

  function clearList() { el("translate-list").innerHTML = ""; syncEmpty(); }

  /** 监听容器内的选中：松手后若有选区就浮出动作条。 */
  function watch(container) {
    container.addEventListener("mouseup", function (event) {
      if (event.button !== 0) return;
      // 让浏览器先把选区定下来，否则拖拽最后一格还在移动时读到的是旧选区
      setTimeout(function () {
        var selection = global.getSelection ? global.getSelection() : null;
        var text = String((selection && selection.toString()) || "").replace(/\s+/g, " ").trim();
        if (text.length < 2) { closeChip(); return; }
        if (!selection.rangeCount || !container.contains(selection.getRangeAt(0).commonAncestorContainer)) {
          closeChip(); return;
        }
        var rect = selection.getRangeAt(0).getBoundingClientRect();
        openChip(text, { x: rect.left + rect.width / 2, y: rect.top, bottom: rect.bottom });
      }, 0);
    }, true);
  }

  function init(getPaperId) {
    resolvePaper = getPaperId || resolvePaper;
    el("tp-close").addEventListener("click", hidePanel);
    el("tp-clear").addEventListener("click", clearList);
    el("tp-toggle").addEventListener("click", function () {
      panel().classList.toggle("hidden");
      syncEmpty();
    });
    document.addEventListener("mousedown", function (event) {
      if (chip && !chip.contains(event.target)) closeChip();
    });
    global.addEventListener("keydown", function (event) { if (event.key === "Escape") closeChip(); });
    global.addEventListener("resize", closeChip);
  }

  global.Translate = { init: init, watch: watch, translate: translate, hide: closeChip,
                       showPanel: showPanel, hidePanel: hidePanel, clear: clearList };
})(window);
