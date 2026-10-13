(function () {
  "use strict";
  var query = new URLSearchParams(location.search), text = "";
  window.addEventListener("reader-language", function () { document.documentElement.lang = I18n.language(); });
  function id(key) { var value = query.get(key) || ""; return /^\d+$/.test(value) ? value : null; }
  function get(url, raw) {
    return fetch(url).then(function (response) {
      if (!response.ok) return response.json().catch(function () { return {}; }).then(function (data) { throw I18n.apiError(data, response.status); });
      return raw ? response.text() : response.json();
    });
  }
  var kind = query.get("kind"), url, raw = false;
  if (id("paper") && ["summary", "deep_reading"].indexOf(kind) >= 0) url = "/api/papers/" + id("paper") + "/artifacts/" + kind + (id("version") ? "?version=" + id("version") : "");
  else if (id("review")) url = "/api/reviews/" + id("review");
  else if (id("node")) { url = "/api/knowledge/nodes/" + id("node") + "/graph.md"; raw = true; }
  get("/api/settings").then(function (settings) {
    I18n.set(settings.output_language);
    I18n.bind(document, function () { return I18n.language() === "en" ? "ArxivReader · Reading" : I18n.t("ui.1eee4a3647db"); }, "title");
    I18n.bind(document.getElementById("reader-download"), function () { return I18n.language() === "en" ? "Download Markdown" : I18n.t("ui.6aa9c09687c9"); });
    I18n.bind(document.getElementById("reader-focus"), function () { return I18n.language() === "en" ? "Focus mode" : I18n.t("ui.30e98c97a40d"); });
    I18n.bind(document.getElementById("reader-content"), function () { return I18n.language() === "en" ? "Loading…" : I18n.t("ui.4927a53bcc88"); });
    if (!url) throw new Error(I18n.language() === "en" ? "Invalid reading link" : I18n.t("ui.1795605170f9"));
    return get(url, raw);
  }).then(function (data) {
    text = raw ? data : data.content_md;
    var produced = raw ? "unknown" : data.output_language || (data.stats || {}).output_language || "unknown";
    I18n.bind(document.getElementById("reader-meta"), function () {
      var notice = I18n.t("ui.3471133f2637");
      notice += produced === "unknown" ? I18n.t("ui.fdb93a3fbcd1") : produced;
      if (produced !== I18n.language()) notice += I18n.t("ui.ac6586cbd996");
      return raw ? I18n.t("ui.a7f2d0ea3e1e") : notice;
    });
    window.ReaderRenderer.set(document.getElementById("reader-content"), text, { language: I18n.language(), toc: true });
  }).catch(function (error) { I18n.bind(document.getElementById("reader-content"), function () { return (I18n.language() === "en" ? "Failed to load: " : I18n.t("ui.01de8216e0d0")) + error.message; }); });
  document.getElementById("reader-focus").onclick = function () { document.body.classList.toggle("focus"); };
  document.getElementById("reader-download").onclick = function () {
    Desktop.saveMarkdown(text, "reading.md").catch(function () { alert(I18n.t("desktop.failed")); });
  };
})();