(function (global) {
  "use strict";
  function available() { return !!(global.pywebview && global.pywebview.api); }
  function call(action, payload) {
    return global.pywebview.api.perform(action, payload || {}).then(function (result) {
      if (!result.ok) throw new Error(I18n.t(result.code || "desktop.failed"));
      return result;
    });
  }
  function saveMarkdown(text, name) {
    if (available()) return call("save_markdown", { text: text, name: name });
    var url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
    var link = document.createElement("a"); link.href = url; link.download = name; link.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    return Promise.resolve();
  }
  function copy(text) {
    if (available()) return call("copy", { text: text });
    if (navigator.clipboard) return navigator.clipboard.writeText(text);
    var node = document.createElement("textarea"); node.value = text; document.body.appendChild(node);
    node.select(); var ok = document.execCommand("copy"); node.remove();
    return ok ? Promise.resolve() : Promise.reject(new Error());
  }
  function storageInfo() {
    var box = document.getElementById("desktop-storage");
    if (box) box.hidden = !available();
    if (!available()) return Promise.resolve();
    return call("storage_info").then(function (result) {
      var node = document.getElementById("storage-path");
      if (node) node.textContent = result.path;
    });
  }
  function openReader(path) {
    if (available()) return call("open_reader", { path: path });
    global.open(path, "_blank", "noopener"); return Promise.resolve();
  }
  global.Desktop = { available: available, saveMarkdown: saveMarkdown, copy: copy,
    storageInfo: storageInfo, openReader: openReader };
  function bindStorage() {
    storageInfo().catch(function () {});
    var open = document.getElementById("storage-open"), select = document.getElementById("storage-select");
    if (open) open.onclick = function () { call("open_storage").catch(failed); };
    if (select) select.onclick = function () {
      call("select_storage").then(function (result) { if (result.selected) alert(I18n.t("desktop.restart")); }).catch(failed);
    };
  }
  function failed(error) { alert(error.message || I18n.t("desktop.failed")); }
  global.addEventListener("pywebviewready", bindStorage);
  document.addEventListener("DOMContentLoaded", bindStorage);
  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[href]");
    if (!link || !available()) return;
    var url = new URL(link.href, location.href);
    if (/^https?:$/.test(url.protocol) && url.origin !== location.origin) {
      event.preventDefault(); call("open_external", { url: url.href }).catch(failed);
    } else if (url.origin === location.origin && /^\/api\/papers\/\d+\/pdf$/.test(url.pathname)) {
      event.preventDefault(); call("open_pdf", { id: Number(url.pathname.split("/")[3]) }).catch(failed);
    } else if (url.origin === location.origin && url.pathname === "/" && location.pathname === "/reader.html") {
      event.preventDefault(); call("focus_main").catch(failed);
    } else if (url.protocol !== "http:" && url.protocol !== "https:") event.preventDefault();
  }, true);
})(window);
