/* 选区文字重建：解决"复制出来和实际选中不一样"。
 *
 * 直接读 selection.toString() 有两个坑（实测 OpenART 原文确认）：
 *   1. 水印混入：发行方埋在页边的微型字（2.5pt 的"上海人工智能实验室"之类）在 DOM 顺序上
 *      夹在正文 span 之间，选中标题时会一并进剪贴板，还会把正文截断错位；
 *   2. 行尾粘连：toString 的换行依赖 span 是否跨页容器，PDF.js 一页一个文字层时整页
 *      粘成一行（"actioncan alter..."）。
 *
 * 这里的做法：取选区与文字层 span 的交集（Range.intersectsNode + 命中时重测交集矩形，
 * 视觉高亮选中的才算数），按 (y, x) 几何顺序重建文本——行间补换行、行内跨度隙过大补空格、
 * 显著小于正文字号的微型 span（水印/潜印）直接丢弃。中英文混排不做逐词空格，
 * 只保证"所见即所选"。
 */
(function (global) {
  "use strict";

  // 小于 0.55 倍行高的纵向差视为同一行；横向间隙超过 0.28 倍行高补一个空格；
  // 小于 0.55 倍正文中位字号的 span 判为水印丢弃。
  var SAME_LINE = 0.55, GAP_SPACE = 0.28, TINY_RATIO = 0.55;

  /** 选区与某个 span 的交集矩形。选区没实际盖住字形时返回零面积矩形。 */
  function intersectRect(range, span) {
    var sub = range.cloneRange();
    // startContainer 在 span 之前 → 交集从 span 开头算起；终点对称处理
    if (span.compareDocumentPosition(range.startContainer) & Node.DOCUMENT_POSITION_PRECEDING) {
      try { sub.setStartBefore(span); } catch (e) { /* doctype 之类的边界直接放弃收紧 */ }
    }
    if (span.compareDocumentPosition(range.endContainer) & Node.DOCUMENT_POSITION_FOLLOWING) {
      try { sub.setEndAfter(span); } catch (e) { /* 同上 */ }
    }
    var rect = sub.getBoundingClientRect();
    sub.detach();
    return rect;
  }

  /** 选区相交的所有文字层 span，带各自被选中那段的几何与所在页码。 */
  function collectHits(range, root) {
    var layers = (root || document).querySelectorAll(".pv-textlayer");
    var hits = [];
    layers.forEach(function (layer) {
      var pageBox = layer.closest ? layer.closest(".pv-page") : null;
      var pageNo = pageBox ? parseInt(pageBox.getAttribute("data-page"), 10) || 0 : 0;
      var spans = layer.querySelectorAll("span");
      for (var i = 0; i < spans.length; i++) {
        var span = spans[i];
        var text = span.textContent;
        if (!text || !text.trim()) continue;
        if (!range.intersectsNode(span)) continue;
        var hitBox = intersectRect(range, span);
        if (hitBox.width < 1 || hitBox.height < 1) continue;   // 只蹭到边框，视觉高亮没盖到
        var spanBox = span.getBoundingClientRect();
        hits.push({
          text: text, page: pageNo,
          x: hitBox.left, y: hitBox.top, right: hitBox.right,
          h: spanBox.height,
        });
      }
    });
    return hits;
  }

  /** 正文中位字号：按字符数加权取中位，抗标题/页眉/角标的极端值。 */
  function bodySize(hits) {
    var sizes = [];
    hits.forEach(function (h) {
      for (var i = 0; i < Math.min(h.text.length, 40); i++) sizes.push(h.h);
    });
    if (!sizes.length) return 0;
    sizes.sort(function (a, b) { return a - b; });
    return sizes[Math.floor(sizes.length / 2)];
  }

  /** 主入口：把当前选区重建成 { text, pages }。选区为空或不在文字层上时退回原生文本。 */
  function fromSelection(root) {
    var selection = global.getSelection ? global.getSelection() : null;
    if (!selection || !selection.rangeCount || selection.isCollapsed) return { text: "", pages: [] };
    var range = selection.getRangeAt(0);
    var hits = collectHits(range, root);
    if (!hits.length) {
      // 重排模式 / 对话框等原生选区：没有文字层几何可用，原样返回
      return { text: String(selection.toString() || ""), pages: [] };
    }

    var body = bodySize(hits);
    hits = hits.filter(function (h) { return body <= 0 || h.h >= body * TINY_RATIO; });
    if (!hits.length) return { text: "", pages: [] };

    var pages = [];
    hits.forEach(function (h) { if (h.page && pages.indexOf(h.page) < 0) pages.push(h.page); });
    pages.sort(function (a, b) { return a - b; });

    // 行聚类：按 y 排序后纵向接近的归入一行，行内再按 x 排
    hits.sort(function (a, b) { return a.y - b.y || a.x - b.x; });
    var lines = [];
    hits.forEach(function (h) {
      var line = lines[lines.length - 1];
      if (line && Math.abs(h.y - line.y) <= line.h * SAME_LINE) {
        line.items.push(h);
        line.y = Math.min(line.y, h.y);
        line.h = Math.max(line.h, h.h);
      } else {
        lines.push({ y: h.y, h: h.h, items: [h] });
      }
    });
    lines.sort(function (a, b) { return a.y - b.y; });

    var text = lines.map(function (line) {
      line.items.sort(function (a, b) { return a.x - b.x; });
      var out = "";
      var prevRight = null;
      line.items.forEach(function (h) {
        if (prevRight !== null) {
          var gap = h.x - prevRight;
          if (gap > line.h * GAP_SPACE && !/^[\s,.;:!?)]/.test(h.text) && !/[\s(]$/.test(out)) out += " ";
        }
        out += h.text;
        prevRight = Math.max(prevRight === null ? -Infinity : prevRight, h.right);
      });
      return out;
    }).join("\n").replace(/[ \t]+\n/g, "\n").trim();
    return { text: text, pages: pages };
  }

  global.SelectionText = { fromSelection: fromSelection };
})(window);
