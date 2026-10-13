(function (global) {
  "use strict";
  var md = global.markdownit({ html: false, linkify: false, typographer: false, breaks: false });
  var escape = md.utils.escapeHtml;
  function safeUrl(url) {
    return !/[\u0000-\u0020\\]/.test(url) && /^(https?:\/\/|\/(?!\/)|#|\?)/i.test(url);
  }
  md.validateLink = safeUrl;
  // 不加载模型提供的远程图片，避免追踪或意外联网。
  md.renderer.rules.image = function (tokens, i) { return escape(tokens[i].content); };
  var linkOpen = md.renderer.rules.link_open;
  md.renderer.rules.link_open = function (tokens, i, opts, env, self) {
    tokens[i].attrSet("rel", "noopener noreferrer");
    return linkOpen ? linkOpen(tokens, i, opts, env, self) : self.renderToken(tokens, i, opts);
  };
  function escapedAt(src, pos) {
    var count = 0;
    while (pos > 0 && src[--pos] === "\\") count++;
    return count % 2 === 1;
  }
  function closing(src, delim, start) {
    var pos = src.indexOf(delim, start);
    while (pos >= 0 && escapedAt(src, pos)) pos = src.indexOf(delim, pos + delim.length);
    return pos;
  }
  function opening(src, pos) {
    if (src.slice(pos, pos + 2) === "$$") return ["$$", "$$", true];
    if (src.slice(pos, pos + 2) === "\\[") return ["\\[", "\\]", true];
    if (src.slice(pos, pos + 2) === "\\(") return ["\\(", "\\)", false];
    if (src[pos] === "$" && !/\s/.test(src[pos + 1] || " ")) return ["$", "$", false];
    return null;
  }
  md.inline.ruler.before("escape", "reader_math", function (state, silent) {
    var pair = opening(state.src, state.pos);
    if (!pair) return false;
    var start = state.pos + pair[0].length;
    var end = closing(state.src, pair[1], start);
    if (pair[0] === "$" && (end < 0 || /\s/.test(state.src[end - 1]) || /\d/.test(state.src[end + 1] || ""))) return false;
    if (end < 0) end = state.src.length;
    var complete = end < state.src.length;
    if (!silent) {
      var token = state.push("reader_math", "", 0);
      token.content = state.src.slice(start, end);
      token.meta = { display: pair[2], complete: complete,
        raw: state.src.slice(state.pos, end + (complete ? pair[1].length : 0)) };
    }
    state.pos = end + (complete ? pair[1].length : 0);
    return true;
  });
  md.block.ruler.before("blockquote", "reader_math_block", function (state, startLine, endLine, silent) {
    var start = state.bMarks[startLine] + state.tShift[startLine];
    if (state.sCount[startLine] - state.blkIndent >= 4) return false;
    var pair = opening(state.src, start);
    if (!pair || !pair[2]) return false;
    if (silent) return true;
    var end = closing(state.src, pair[1], start + pair[0].length);
    var limit = state.eMarks[endLine - 1];
    var complete = end >= 0 && end <= limit;
    if (!complete) end = limit;
    var last = startLine;
    while (last + 1 < endLine && state.bMarks[last + 1] <= end) last++;
    // 结束分隔符后若仍有文本，交给行内解析，避免吞掉正文。
    if (complete && state.src.slice(end + pair[1].length, state.eMarks[last]).trim()) return false;
    var token = state.push("reader_math", "", 0);
    token.block = true;
    token.map = [startLine, last + 1];
    token.content = state.src.slice(start + pair[0].length, end);
    token.meta = { display: true, complete: complete,
      raw: state.src.slice(start, end + (complete ? pair[1].length : 0)) };
    state.line = last + 1;
    return true;
  }, { alt: ["paragraph", "reference", "blockquote", "list"] });
  md.renderer.rules.reader_math = function (tokens, i, options, env) {
    var token = tokens[i], meta = token.meta;
    try {
      if (!meta.complete) throw new Error("incomplete");
      return global.katex.renderToString(token.content, {
        displayMode: meta.display, throwOnError: true, trust: false, strict: "error",
        maxExpand: 1000, maxSize: 20, output: "htmlAndMathml"
      });
    } catch (err) {
      var label = env.language === "en" ? "Incomplete or unsupported formula; original preserved" : I18n.t("ui.7f5e6c2f01ff");
      return '<code class="math-error" title="' + label + '">' + escape(meta.raw) + '</code><small class="math-warning">' + label + '</small>';
    }
  };
  function render(text, options) {
    var html = md.render(String(text || ""), options || {});
    return global.DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true, svg: true, mathMl: true },
      FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form", "input", "img"],
      FORBID_ATTR: ["src", "srcset", "formaction"], ALLOW_DATA_ATTR: false
    });
  }
  var headingId = 0, timers = new WeakMap();
  function set(node, text, options) {
    options = options || {};
    clearTimeout(timers.get(node));
    node.classList.add("md");
    I18n.unbind(node);
    node.innerHTML = render(text, options);
    node.querySelectorAll("pre").forEach(function (pre) {
      var code = pre.querySelector("code"), button = document.createElement("button");
      if (!code) return;
      button.className = "code-copy";
      I18n.bind(button, function () { return I18n.t("ui.63d90d977348"); });
      button.onclick = function () {
        Desktop.copy(code.textContent).then(function () { I18n.write(button, "✓"); }).catch(function () {});
      };
      pre.appendChild(button);
    });
    if (options.toc) {
      var headings = node.querySelectorAll("h1,h2,h3");
      if (headings.length >= 4) {
        var toc = document.createElement("details"), summary = document.createElement("summary");
        toc.className = "reading-toc";
        I18n.bind(summary, function () { return I18n.t("ui.3b2d0794ee6f"); });
        toc.appendChild(summary);
        headings.forEach(function (h) {
          h.id = "reading-heading-" + (++headingId);
          var a = document.createElement("a");
          a.href = "#" + h.id; I18n.write(a, h.textContent);
          a.onclick = function (event) { event.preventDefault(); h.scrollIntoView({ block: "start" }); };
          toc.appendChild(a);
        });
        node.prepend(toc);
      }
    }
  }
  function stream(node, text, options) {
    var pending = timers.get(node);
    if (pending) clearTimeout(pending);
    // 首次立刻显示；连续片段按 80ms 节流，不延后直到流结束。
    var now = Date.now(), last = Number(node.dataset.renderedAt || 0);
    if (now - last >= 80) {
      set(node, text, options); node.dataset.renderedAt = String(now);
    } else {
      timers.set(node, setTimeout(function () { set(node, text, options); node.dataset.renderedAt = String(Date.now()); }, 80 - (now - last)));
    }
  }
  global.ReaderRenderer = { render: render, set: set, stream: stream, safeUrl: safeUrl };
})(window);