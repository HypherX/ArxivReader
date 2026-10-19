/* 在一页的文字项里查找，并记下命中落在哪些文字层 span 上。
 *
 * PDF.js 已经按字形间距把该有的空格放进 item.str。这里把各项直接拼起来，
 * 不再额外插入空格：中文 PDF 经常一字一项，插入空格会让「论文」变成「论 文」，
 * 查找得到 0 条，页面上也就没有任何高亮。
 * div 下标与 TextLayer.textDivs 对齐，只计带 str 的项。
 */
(function (root) {
  "use strict";

  var MAX_MATCHES = 3000;

  function originalOffset(text, lowIndex) {
    if (lowIndex <= 0) return 0;
    var low = 0;
    for (var i = 0; i < text.length; i++) {
      var step = text.charAt(i).toLowerCase().length;
      if (low + step > lowIndex) return i;
      low += step;
      if (low === lowIndex) return i + 1;
    }
    return text.length;
  }

  function matchPage(items, term) {
    var query = String(term || "").trim().toLowerCase();
    if (!query) return [];
    var parts = [];
    var div = 0;
    (items || []).forEach(function (item) {
      if (!item || item.str == null) return;
      var text = String(item.str);
      parts.push({ div: div, text: text, low: text.toLowerCase(), at: 0 });
      div += 1;
    });
    var hay = "";
    parts.forEach(function (part) {
      part.at = hay.length;
      hay += part.low;
    });
    var matches = [];
    var from = 0;
    while (matches.length < MAX_MATCHES) {
      var found = hay.indexOf(query, from);
      if (found < 0) break;
      var end = found + query.length;
      var ranges = [];
      parts.forEach(function (part) {
        var partEnd = part.at + part.low.length;
        var lo = Math.max(found, part.at);
        var hi = Math.min(end, partEnd);
        if (lo >= hi) return;
        var start = originalOffset(part.text, lo - part.at);
        var stop = originalOffset(part.text, hi - part.at);
        if (stop > start) ranges.push({ div: part.div, start: start, end: stop });
      });
      if (ranges.length) matches.push(ranges);
      from = found + query.length;
    }
    return matches;
  }

  /** 输入法上屏用的回车不能交给业务。
   *  composition 期间的 Enter，以及 compositionend 之后紧接着的那一次 Enter，都留给输入法。
   */
  function bindImeKey(input, onKey) {
    var composing = false;
    var swallow = false;
    input.addEventListener("compositionstart", function () { composing = true; });
    input.addEventListener("compositionend", function () {
      composing = false;
      swallow = true;
      setTimeout(function () { swallow = false; }, 0);
    });
    input.addEventListener("keydown", function (event) {
      if (composing || event.isComposing || event.keyCode === 229 || swallow) return;
      onKey(event);
    });
  }

  root.PdfFind = { matchPage: matchPage };
  root.bindImeKey = bindImeKey;
})(typeof window !== "undefined" ? window : globalThis);
