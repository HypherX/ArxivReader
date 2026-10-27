/* 浅色 / 深色。没选过时跟系统，选过以后记在本机偏好里。
 * 首屏由 index.html / reader.html 里的一小段脚本先写上 data-theme，避免闪一下。 */
(function (global) {
  "use strict";

  var KEY = "arxivreader.theme";

  function systemTheme() {
    return global.matchMedia && global.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function storedTheme() {
    try {
      var value = localStorage.getItem(KEY);
      if (value === "dark" || value === "light") return value;
    } catch (e) { /* 隐私模式等读不到本地存储时跟系统 */ }
    return null;
  }

  function remember(value) {
    try {
      if (value === "dark" || value === "light") localStorage.setItem(KEY, value);
      else localStorage.removeItem(KEY);
    } catch (e) { /* 记不住就只在这一页生效 */ }
  }

  function effective() {
    return storedTheme() || systemTheme();
  }

  function paint() {
    var theme = effective();
    document.documentElement.setAttribute("data-theme", theme);
    var btn = document.getElementById("theme-toggle");
    if (!btn || !global.I18n) return;
    var dark = theme === "dark";
    btn.setAttribute("aria-pressed", dark ? "true" : "false");
    global.I18n.bind(btn, function () {
      return (dark ? "☾ " : "☀ ") + global.I18n.t(dark ? "theme.dark" : "theme.light");
    });
    global.I18n.bind(btn, function () {
      return global.I18n.t(dark ? "theme.to_light" : "theme.to_dark");
    }, "title");
  }

  function applyChoice(value) {
    if (value === "dark" || value === "light") remember(value);
    paint();
  }

  function toggle() {
    var next = effective() === "dark" ? "light" : "dark";
    applyChoice(next);
    if (global.ReaderPreferences && global.ReaderPreferences.setItem) {
      global.ReaderPreferences.setItem("theme", next);
    }
  }

  function init() {
    paint();
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.addEventListener("click", toggle);
    if (global.matchMedia) {
      global.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        if (!storedTheme()) paint();
      });
    }
  }

  global.Theme = { init: init, paint: paint, sync: applyChoice, toggle: toggle };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})(window);
