/* PDF 阅读器（本地打包的 PDF.js）。
 *
 * 为什么换掉原来的"服务端逐页出 PNG + 前端自绘"：图片视图没有文字层，读者无法选中
 * 任何内容，划词翻译就无从谈起；缩放还受限于服务端预渲染倍率。PDF.js 在浏览器里直接
 * 解析 PDF，文字层、精确缩放、全文检索都是原生能力，服务端只把文件送出去
 * （/api/papers/{id}/pdf 支持 Range，长文档可以边下边看）。
 *
 * 交互约定（和普通 PDF 阅读器一致）：
 *   滚轮 / 触摸板两指  = 翻页滚动（交给浏览器，最顺滑）
 *   捏合 / Ctrl(⌘)+滚轮 = 以光标为锚点平滑缩放
 *   所以这里绝不拦截普通滚轮。
 *
 * 依赖按项目惯例本地打包（见 tools/vendor_assets.py），运行时不访问任何 CDN。
 * 用 dynamic import 而不是 <script type="module">：整个前端仍是一份经典脚本 IIFE，
 * 只有这里按需把 PDF.js 拉进来，首屏不用为阅读器买单。
 */
(function (global) {
  "use strict";

  var BASE = "/vendor/pdfjs-dist/";
  var MIN_SCALE = 0.3, MAX_SCALE = 8;
  var RENDER_MARGIN = 900;          // 视口上下各预渲染多少像素
  var SIDE_PAD = 8;                 // 适应宽度时左右各留的空白：只留一条缝，页面基本铺满栏宽
  var MAX_CANVAS_PIXELS = 16777216;  // 单页画布像素上限。放大时优先保住屏幕像素，极端倍率才降采样
  var MAX_CANVAS_DIM = 8192;          // 单边像素上限：再大时部分浏览器会画失败，页面下半截留黑
  var MAX_CONCURRENT = 2;             // 同时渲染的页数
  var RENDER_TIMEOUT_MS = 12000;      // 单页渲染看门狗：超时归还额度并重排

  var queue = [];                     // 待渲染页队列
  var rendering = 0;                  // 正在渲染的页数

  var state = {
    paperId: null, token: 0, doc: null, pages: [], scale: 1, fitScale: 1, fitted: true,
    rotation: 0, pdfjs: null, query: "", matches: [], matchIndex: -1,
  };

  function el(id) { return document.getElementById(id); }

  function node(tag, attrs) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (key === "text") n.textContent = value == null ? "" : String(value);
      else n.setAttribute(key, value);
    });
    return n;
  }

  function scrollBox() { return el("pdf-scroll"); }

  // ---------------------------------------------------------------- 打开 / 关闭
  function loadPdfJs() {
    if (state.pdfjs) return Promise.resolve(state.pdfjs);
    return import(BASE + "pdf.min.js").then(function (lib) {
      lib.GlobalWorkerOptions.workerSrc = BASE + "pdf.worker.min.js";
      state.pdfjs = lib;
      return lib;
    });
  }

  function close() {
    flushProgress(true);
    clearInterval(progressTimer); progressTimer = null;
    state.token += 1;
    queue = []; rendering = 0;
    state.paperId = null;
    state.url = null;
    state.matches = []; state.matchIndex = -1; state.query = "";
    state.pages.forEach(function (p) { if (p.task) p.task.cancel(); });
    state.pages = [];
    var doc = state.doc; state.doc = null;
    if (doc) doc.destroy().catch(function () {});
    var box = scrollBox();
    if (box) { box.innerHTML = ""; box.scrollTop = 0; box.scrollLeft = 0; }
  }

  // ---------------------------------------------------------------- 阅读进度上报
  // maxSeen = 真正停留过的最远页（只增不减）：页面要在可视区内连续停够 DWELL_MS 才算"看过"，
  // 快速滚过 / 预渲染边距里的页不计——否则一滚到底就直接 100% "已读完"。
  // readSecs = 本次打开累计的阅读秒数：只在页面可见、预览栏开着、且用户有操作（90s 内）时计时，
  // 应用挂在那不动不会再跑表。每 5s 记一次账，每 20s 或关闭时上报一次，后端取 max 落库。
  var progressTimer = null;
  var DWELL_MS = 1500;               // 一页要在视口内停多久才算"看过"
  var IDLE_MS = 90000;               // 多久没操作算人不在，停止计时
  var VIEW_FRACTION = 0.25;          // 页面至少露出多少比例才算正在看

  function readerOnScreen() {
    var box = scrollBox();
    if (!box || box.offsetParent === null) return false;
    var rect = box.getBoundingClientRect();
    return rect.width > 50 && rect.height > 50;
  }

  function noteActivity() { state.lastActive = Date.now(); }

  function isActive(now) {
    return document.visibilityState === "visible" && readerOnScreen()
      && now - (state.lastActive || 0) < IDLE_MS;
  }

  /** 按真实可视区更新每页停留状态，停够 DWELL_MS 的页标记为 seen，推进 maxSeen。 */
  function noteDwell(now, active) {
    if (!state.pages.length) return;
    var box = scrollBox();
    var host = box.getBoundingClientRect();
    var maxSeen = state.maxSeen || 0;
    state.pages.forEach(function (p) {
      var visible = false;
      if (active && p.box.isConnected) {
        var rect = p.box.getBoundingClientRect();
        var overlap = Math.min(rect.bottom, host.bottom) - Math.max(rect.top, host.top);
        visible = overlap > 0 && overlap >= Math.min(rect.height, host.height) * VIEW_FRACTION;
      }
      if (visible) {
        if (!p.visibleSince) p.visibleSince = now;
        if (!p.seen && now - p.visibleSince >= DWELL_MS) p.seen = true;
      } else {
        p.visibleSince = 0;
      }
      if (p.seen && p.no > maxSeen) maxSeen = p.no;
    });
    state.maxSeen = maxSeen;
  }

  function tickProgress() {
    if (!state.paperId || !state.pages.length) return;
    var now = Date.now();
    var active = isActive(now);
    noteDwell(now, active);
    if (!active) { state.tickAt = now; return; }
    state.readSecs = (state.readSecs || 0) + Math.min(30, Math.round((now - (state.tickAt || now)) / 1000));
    state.tickAt = now;
    // 时长攒够 20s、或最远页推进了 3 页以上，就上报一次
    if (state.readSecs - (state.reportedSecs || 0) >= 20 ||
        (state.maxSeen || 0) - (state.reportedPage || 0) >= 3) flushProgress(false);
  }

  function flushProgress(final) {
    if (!state.paperId) return;
    var secs = (state.readSecs || 0) - (state.reportedSecs || 0);
    var page = state.maxSeen || 0;
    if (!page && secs <= 0) return;
    var payload = { paperId: state.paperId, page: page, pages: state.pages.length,
                    seconds: Math.max(0, secs) };
    state.reportedSecs = state.readSecs;   // 乐观记账；失败也就丢一小段，可接受
    state.reportedPage = state.maxSeen;
    fetch("/api/papers/" + payload.paperId + "/reading-progress", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page: payload.page, pages: payload.pages, seconds: payload.seconds }),
      keepalive: true,
    }).then(function (resp) {
      // 上报成功把最新进度交给外层刷新列表（阅读时长 / 完成度立刻可见，不用等重进）
      if (!resp.ok || typeof state.onProgress !== "function") return null;
      return resp.json().then(function (paper) { state.onProgress(paper); });
    }).catch(function () {});
  }

  function open(paper, opts) {
    var box = scrollBox();
    if (!box) return Promise.resolve();
    close();
    var token = state.token;
    state.paperId = paper.id;
    // 默认打开原始 PDF；传入 opts.url 可打开全文翻译产物（对照版 / 译文版）
    state.url = (opts && opts.url) || ("/api/papers/" + paper.id + "/pdf");
    state.scale = 1; state.rotation = 0; state.fitted = true;
    // 阅读进度从零记起；恢复上次读到的页（仅原始 PDF，翻译产物的分页不同）
    state.maxSeen = 0; state.readSecs = 0; state.reportedSecs = 0; state.reportedPage = 0; state.tickAt = Date.now();
    state.lastActive = Date.now();
    clearInterval(progressTimer);
    progressTimer = setInterval(tickProgress, 5000);
    var restorePage = (!opts || !opts.url) && paper.read_page > 1 ? paper.read_page : 0;
    box.innerHTML = "";
    box.appendChild(node("div", { class: "pv-status", text: global.I18n.t("ui.9aed1d2f4cea") }));
    return loadPdfJs().then(function (lib) {
      return lib.getDocument({
        url: state.url,
        cMapUrl: BASE + "cmaps/", cMapPacked: true,
        standardFontDataUrl: BASE + "standard_fonts/",
        isEvalSupported: false,      // 桌面壳里禁了远程脚本，字体回退走 canvas 路径
        disableAutoFetch: false,
      }).promise;
    }).then(function (doc) {
      if (token !== state.token) { doc.destroy(); return; }   // 加载期间已切走
      state.doc = doc;
      return buildDocument(doc, token).then(function () {
        if (token === state.token && restorePage > 1) gotoPage(restorePage);
      });
    }).catch(function (error) {
      if (token !== state.token) return;
      state.paperId = null;
      box.innerHTML = "";
      box.appendChild(emptyState(error));
    });
  }

  function emptyState(error) {
    var wrap = node("div", { class: "empty rich" });
    wrap.appendChild(node("div", { class: "icon", text: "⚠️" }));
    wrap.appendChild(node("div", { class: "t", text: global.I18n.t("ui.8c43e8c6de43") }));
    wrap.appendChild(node("div", { class: "h", text: String((error && error.message) || error || "") }));
    return wrap;
  }

  /** 先按 PDF 自带尺寸铺好占位页，再按滚动位置懒渲染。 */
  function buildDocument(doc, token) {
    var box = scrollBox();
    box.innerHTML = "";
    var holder = node("div", { id: "pdf-pages" });
    box.appendChild(holder);
    var jobs = [];
    for (var i = 1; i <= doc.numPages; i++) jobs.push(doc.getPage(i));
    return Promise.all(jobs).then(function (pages) {
      if (token !== state.token) return;
      state.pages = pages.map(function (page, index) {
        var record = {
          no: index + 1, page: page, text: null, inView: false,
          viewport: page.getViewport({ scale: 1, rotation: state.rotation }),
          box: node("div", { class: "pv-page", "data-page": String(index + 1) }),
          rendered: false, task: null, spans: null,
        };
        record.box.appendChild(node("span", { class: "pv-page-no", text: String(index + 1) }));
        holder.appendChild(record.box);
        return record;
      });
      state.scale = fitWidth();
      state.fitted = true;
      layoutPages();
      syncVisibility();
      updateLabels();
    });
  }

  // ---------------------------------------------------------------- 尺寸与缩放
  function pageWidth() {
    if (!state.pages.length) return 612;
    // 取"最常见"的页宽而不是最大页宽：夹一页横版大图就把全书缩到半屏是阅读器最常见的难受
    var counts = {}, best = 612, bestHits = -1;
    state.pages.forEach(function (p) {
      var key = Math.round(p.viewport.width);
      counts[key] = (counts[key] || 0) + 1;
      if (counts[key] > bestHits || (counts[key] === bestHits && key > best)) { best = key; bestHits = counts[key]; }
    });
    return best;
  }

  /** 适应栏宽：页面铺满可视宽度，只留极小的边距。 */
  function fitWidth() {
    var box = scrollBox();
    var avail = Math.max(120, (box.clientWidth || 640) - SIDE_PAD * 2);
    state.fitScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, avail / pageWidth()));
    return state.fitScale;
  }

  /** keepFit=true 表示"这是适应宽度的重算"，不能把跟随标记抹掉。 */
  function applyScale(next, anchorY, keepFit) {
    var clamped = Math.max(MIN_SCALE, Math.min(MAX_SCALE, next));
    if (Math.abs(clamped - state.scale) < 1e-4) return;
    var box = scrollBox();
    var ay = (anchorY == null) ? box.clientHeight / 2 : anchorY;
    var before = box.scrollHeight || 1;
    var ratio = (box.scrollTop + ay) / before;
    state.scale = clamped;
    if (!keepFit) state.fitted = false;
    layoutPages();
    box.scrollTop = Math.max(0, ratio * (box.scrollHeight || 1) - ay);
    updateLabels();
  }

  function gotoFit(scrollToTop) {
    state.fitted = true;
    applyScale(fitWidth(), 0, true);
    if (scrollToTop) scrollBox().scrollTop = 0;
    updateLabels();
  }

  /** 只改盒子尺寸并作废已画过的页；真正画哪些页由 syncVisibility 按滚动位置决定。 */
  function layoutPages() {
    state.pages.forEach(function (p) {
      p.box.style.width = (p.viewport.width * state.scale) + "px";
      p.box.style.height = (p.viewport.height * state.scale) + "px";
      p.tries = 0;                       // 尺寸变了，重画预算重新给足
      if (!p.rendered) return;
      if (p.task) { p.task.cancel(); p.task = null; }
      p.rendered = false;
      p.queued = false;
    });
    syncVisibility();
  }

  // ---------------------------------------------------------------- 单页渲染
  /** 哪些页该画：视口上下各 RENDER_MARGIN 像素以内。滚出这个范围的页取消渲染、留白等回头。 */
  function syncVisibility() {
    if (!state.pages.length) return;
    var box = scrollBox();
    var host = box.getBoundingClientRect();
    state.pages.forEach(function (p) {
      var rect = p.box.getBoundingClientRect();
      var near = rect.bottom >= host.top - RENDER_MARGIN && rect.top <= host.bottom + RENDER_MARGIN;
      if (near !== p.inView) {
        p.inView = near;
        if (near) { p.tries = 0; renderPage(p); }   // 滚回来就重新给足尝试次数
        else if (p.task) { p.task.cancel(); p.task = null; p.rendered = false; }
        return;
      }
      // 位置没变也要看一眼"是不是欠着一页没画"。栏宽变化后盒子会立刻变大，
      // 而旧 canvas 还停在原来的尺寸，白底就从右边和下边露成一整片空白。
      if (near && !p.rendered && !p.queued && !p.task) renderPage(p);
    });
    noteDwell(Date.now(), isActive(Date.now()));
    pumpQueue();
  }

  /** 排队而不是来一个渲染一个： tall 窗口里一次可能 intersect 七八页，全开工谁都要等。 */
  function renderPage(record) {
    if (record.rendered || record.queued || !state.doc || !record.inView) return;
    if ((record.tries || 0) >= 4) return;      // 连续失败/超时多次就别再徒劳重试
    record.queued = true;
    queue.push(record);
    pumpQueue();
  }

  function distanceFromView(record) {
    var host = scrollBox().getBoundingClientRect();
    var rect = record.box.getBoundingClientRect();
    if (rect.bottom < host.top) return host.top - rect.bottom;
    if (rect.top > host.bottom) return rect.top - host.bottom;
    return 0;
  }

  function pumpQueue() {
    while (rendering < MAX_CONCURRENT && queue.length) {
      queue.sort(function (a, b) { return distanceFromView(a) - distanceFromView(b); });
      var record = queue.shift();
      record.queued = false;
      if (record.rendered || !record.inView) continue;
      rendering += 1;
      startRender(record, function () { rendering -= 1; pumpQueue(); });
    }
  }

  /** 位图像素 / CSS 像素。放大后仍按 devicePixelRatio 画，字才不会被拉糊。 */
  function outputRatio(viewport) {
    var ratio = Math.max(1, Math.min(global.devicePixelRatio || 1, 2));
    var pixels = viewport.width * viewport.height;
    if (pixels > 0 && pixels * ratio * ratio > MAX_CANVAS_PIXELS) {
      ratio = Math.sqrt(MAX_CANVAS_PIXELS / pixels);
    }
    var longest = Math.max(viewport.width, viewport.height);
    if (longest > 0 && longest * ratio > MAX_CANVAS_DIM) ratio = MAX_CANVAS_DIM / longest;
    return ratio > 0 ? ratio : 1;
  }

  function startRender(record, done) {
    record.rendered = true;
    record.tries = (record.tries || 0) + 1;
    var viewport = record.page.getViewport({ scale: state.scale, rotation: state.rotation });
    var ratio = outputRatio(viewport);
    var canvas = node("canvas", { class: "pv-canvas" });
    canvas.style.width = viewport.width + "px";
    canvas.style.height = viewport.height + "px";
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    var layer = node("div", { class: "pv-textlayer" });
    // 重绘（缩放/旋转）前先清掉上一次留下的 canvas 与文字层，否则会叠出空白页
    Array.prototype.forEach.call(record.box.querySelectorAll("canvas, .pv-textlayer"), function (old) { old.remove(); });
    record.box.insertBefore(canvas, record.box.firstChild);
    record.box.appendChild(layer);
    record.spans = null;
    record.textLayer = null;
    var task;
    try {
      // 画布物理像素是 CSS 尺寸的 ratio 倍，渲染必须按同一倍率放大，否则只画满左上角。
      var transform = ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null;
      task = record.page.render({
        canvasContext: canvas.getContext("2d", { alpha: false }),
        viewport: viewport,
        transform: transform,
      });
    } catch (error) {
      record.rendered = false;
      record.box.appendChild(node("div", { class: "pv-error", text: global.I18n.t("ui.208ec265a2a4", { page: record.no }) }));
      done();                       // 这里还没起看门狗，直接归还额度
      return;
    }
    record.task = task;
    clearTimeout(record.watchdog);
    record.watchdog = setTimeout(function () { finish(true); }, RENDER_TIMEOUT_MS);
    function settle() {                       // 无论成功、失败还是超时，额度都必须还回队列
      clearTimeout(record.watchdog);
      record.watchdog = null;
      done();
    }
    function finish(timedOut) {
      if (record.task === task) {
        record.task = null;
        record.rendered = false;
        if (timedOut) { try { task.cancel(); } catch (e) { /* 已结束 */ } }
        if (record.inView && (record.tries || 0) < 4) renderPage(record);
      }
      settle();
    }
    task.promise.then(function () {
      if (record.task !== task) { settle(); return; }   // 已被更新的一次渲染取代
      record.task = null;
      clearTimeout(record.watchdog);
      record.watchdog = setTimeout(function () { settle(); }, RENDER_TIMEOUT_MS);
      buildTextLayer(record, viewport).then(settle, settle);
    }).catch(function (error) {
      if (record.task === task) {
        record.task = null;
        record.rendered = false;
        if (/cancel|abort/i.test(String((error && (error.name || error.message)) || ""))) {
          if (record.inView) renderPage(record);       // 视口内被打断：立刻补一次
        } else {
          record.box.appendChild(node("div", { class: "pv-error", text: global.I18n.t("ui.208ec265a2a4", { page: record.no }) }));
        }
      }
      settle();                     // 必须走 settle：否则看门狗还会再还一次额度
    });
  }

  /** 用 PDF.js 官方文字层：每个 span 的位置、字距、旋转、缩放全部由库计算。
   *  之前手绘 span（手动乘矩阵 + scaleX 拉伸到实测宽度）会跟画布上的字对不齐，
   *  选中时高亮框和可见文字错开，看上去就是"鬼影"，划词也很难划准。 */
  function buildTextLayer(record, viewport) {
    if (record.spans) return Promise.resolve();
    var layer = record.box.querySelector(".pv-textlayer");
    var lib = state.pdfjs;
    if (!layer || !lib || typeof lib.TextLayer !== "function") { record.spans = []; return Promise.resolve(); }
    // --scale-factor 必须在构造前就位：TextLayer 用 calc(var(--scale-factor) * Npx) 定位并定尺寸。
    layer.style.setProperty("--scale-factor", String(viewport.scale));
    // 容器尺寸默认被 round 到整像素（比画布窄/矮不足 1px），而 span 的 left/top 是容器宽高
    // 的百分比，这点误差会让选区高亮和画布字形错开，看上去就是重影；把 round 步长调细即可。
    layer.style.setProperty("--scale-round-x", "0.01px");
    layer.style.setProperty("--scale-round-y", "0.01px");
    return record.page.getTextContent({ includeMarkings: false }).then(function (content) {
      if (!content) { record.spans = []; return; }
      record.textLayer = new lib.TextLayer({ textContentSource: content, container: layer, viewport: viewport });
      return record.textLayer.render().then(function () {
        record.spans = record.textLayer.textDivs.slice();
        if (state.query) highlight();
      });
    }).catch(function () { record.spans = []; });
  }

  // ---------------------------------------------------------------- 全文检索
  function pageText(record) {
    if (!record.text) {
      record.text = record.page.getTextContent({ includeMarkedContent: false }).then(function (content) {
        record.items = (content && content.items) || [];
        return record.items;
      }).catch(function () { record.items = []; return []; });
    }
    return record.text;
  }

  function find(query, direction) {
    if (!state.doc) return Promise.resolve({ count: 0 });
    var term = (query || "").trim();
    var fresh = term !== state.query;
    state.query = term;
    if (term.length < 2) { state.matches = []; state.matchIndex = -1; highlight(); updateLabels(); return Promise.resolve({ count: 0 }); }
    var token = state.token;
    var seq = (state.findSeq = (state.findSeq || 0) + 1);
    return Promise.all(state.pages.map(pageText)).then(function (itemLists) {
      if (token !== state.token || seq !== state.findSeq) return { count: 0 };
      var matches = [];
      itemLists.forEach(function (items, position) {
        global.PdfFind.matchPage(items, term).forEach(function (ranges) {
          if (matches.length < 3000) matches.push({ page: position + 1, ranges: ranges });
        });
      });
      state.matches = matches;
      if (!matches.length) { state.matchIndex = -1; highlight(); updateLabels(); return { count: 0 }; }
      if (fresh || state.matchIndex < 0 || state.matchIndex >= matches.length) {
        state.matchIndex = direction === "prev" ? matches.length - 1 : 0;
      } else {
        state.matchIndex = direction === "prev"
          ? (state.matchIndex - 1 + matches.length) % matches.length
          : (state.matchIndex + 1) % matches.length;
      }
      var record = state.pages[matches[state.matchIndex].page - 1];
      if (record && !record.textLayer) gotoPage(record.no);
      highlight();
      updateLabels();
      return { count: matches.length };
    });
  }

  /** 把命中的字包进 mark。标记画在文字层内部，才会跟着字形缩放并对齐。 */
  function clearHits() {
    state.pages.forEach(function (page) {
      var divs = page.textLayer && page.textLayer.textDivs;
      if (!divs) return;
      divs.forEach(function (span) {
        if (!span || span.dataset.hitSource == null) return;
        span.textContent = span.dataset.hitSource;
        delete span.dataset.hitSource;
      });
    });
  }

  function paintSpan(span, pieces) {
    if (!span.isConnected) return;
    if (span.dataset.hitSource == null) span.dataset.hitSource = span.textContent;
    var text = span.dataset.hitSource;
    var ordered = pieces.slice().sort(function (a, b) { return a.start - b.start || a.end - b.end; });
    while (span.firstChild) span.removeChild(span.firstChild);
    var cursor = 0;
    ordered.forEach(function (piece) {
      var start = Math.max(cursor, Math.min(piece.start, text.length));
      var end = Math.max(start, Math.min(piece.end, text.length));
      if (start > cursor) span.appendChild(document.createTextNode(text.slice(cursor, start)));
      if (end > start) {
        var mark = document.createElement("mark");
        mark.className = piece.current ? "pv-hit current" : "pv-hit";
        mark.textContent = text.slice(start, end);
        span.appendChild(mark);
      }
      cursor = end;
    });
    if (cursor < text.length) span.appendChild(document.createTextNode(text.slice(cursor)));
  }

  function highlight() {
    clearHits();
    var buckets = [];
    state.matches.forEach(function (hit, index) {
      var record = state.pages[hit.page - 1];
      var divs = record && record.textLayer && record.textLayer.textDivs;
      if (!divs) return;
      var current = index === state.matchIndex;
      hit.ranges.forEach(function (range) {
        var span = divs[range.div];
        if (!span) return;
        var bucket = null;
        for (var i = 0; i < buckets.length; i++) if (buckets[i].span === span) bucket = buckets[i];
        if (!bucket) { bucket = { span: span, pieces: [] }; buckets.push(bucket); }
        bucket.pieces.push({ start: range.start, end: range.end, current: current });
      });
    });
    buckets.forEach(function (bucket) { paintSpan(bucket.span, bucket.pieces); });
    revealCurrent();
  }

  function revealCurrent() {
    var mark = document.querySelector("#pdf-pages mark.pv-hit.current");
    var box = scrollBox();
    if (!mark || !box) return;
    var host = box.getBoundingClientRect();
    var rect = mark.getBoundingClientRect();
    if (rect.width < 0.5 && rect.height < 0.5) return;
    if (rect.top < host.top + 48) box.scrollTop += rect.top - host.top - 48;
    else if (rect.bottom > host.bottom - 36) box.scrollTop += rect.bottom - host.bottom + 36;
    if (rect.left < host.left + 16) box.scrollLeft += rect.left - host.left - 16;
    else if (rect.right > host.right - 16) box.scrollLeft += rect.right - host.right + 16;
  }

  // ---------------------------------------------------------------- 导航与标签
  function gotoPage(number) {
    var record = state.pages[Math.max(1, Math.min(state.pages.length, number)) - 1];
    if (!record) return;
    var box = scrollBox();
    box.scrollTop += record.box.getBoundingClientRect().top - box.getBoundingClientRect().top - 10;
  }

  function currentPage() {
    var box = scrollBox();
    if (!box || !state.pages.length) return 1;
    var line = box.getBoundingClientRect().top + box.clientHeight * 0.3;
    var current = 1;
    for (var i = 0; i < state.pages.length; i++) {
      if (state.pages[i].box.getBoundingClientRect().top <= line) current = i + 1;
      else break;
    }
    return current;
  }

  function updateLabels() {
    var percent = el("pdfv-label");
    if (percent) percent.textContent = Math.round(state.scale * 100) + "%";
    var page = el("pdfv-pageno");
    if (page) {
      page.max = String(state.pages.length || 1);
      if (document.activeElement !== page) page.value = String(currentPage());
    }
    var total = el("pdfv-total");
    if (total) total.textContent = "/ " + (state.pages.length || 0);
    var count = el("pdfv-find-count");
    if (count) count.textContent = state.matches.length ? (state.matchIndex + 1) + " / " + state.matches.length : "0";
    var fit = el("pdfv-fit");
    if (fit) fit.classList.toggle("on", state.fitted);
  }

  function rotate() {
    state.rotation = (state.rotation + 90) % 360;
    state.pages.forEach(function (p) {
      p.viewport = p.page.getViewport({ scale: 1, rotation: state.rotation });
      p.spans = null; p.text = null; p.rendered = false;   // 旧 canvas 由 startRender 负责清理
    });
    gotoFit();
  }

  // ---------------------------------------------------------------- 事件绑定
  var wheelFrame = null, wheelFactor = 1, wheelAnchor = null;

  // 只拦"捏合"和"按住 Ctrl/⌘ 滚轮"；普通滚轮一律交给浏览器滚动
  function onWheel(event) {
    if (!(event.ctrlKey || event.metaKey)) return;
    event.preventDefault();
    var box = scrollBox();
    wheelAnchor = event.clientY - box.getBoundingClientRect().top;
    wheelFactor *= Math.pow(1.0035, -event.deltaY);
    if (wheelFrame) return;
    wheelFrame = requestAnimationFrame(function () {
      wheelFrame = null;
      var factor = wheelFactor; wheelFactor = 1;
      applyScale(state.scale * factor, wheelAnchor, false);
    });
  }

  var scrollFrame = null;
  function onScroll() {
    if (scrollFrame) return;
    scrollFrame = requestAnimationFrame(function () { scrollFrame = null; syncVisibility(); updateLabels(); });
  }

  var resizeTimer = null;
  // 栏宽变化（拖分隔条、开合译文栏、折叠面板、缩放窗口）都要重新铺满。
  // 之前这里调 applyScale 会把 fitted 一起清掉，于是"第一次之后再也不跟宽度"，
  // 页面就比栏窄一截，右边留出一条深色空白。
  function onResize() {
    if (!state.fitted || !state.pages.length) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (state.fitted && state.pages.length) { state.scale = fitWidth(); layoutPages(); updateLabels(); }
    }, 80);
  }

  function bind() {
    var box = scrollBox();
    if (!box || box.dataset.bound) return;
    box.dataset.bound = "1";
    box.addEventListener("wheel", onWheel, { passive: false });
    box.addEventListener("scroll", onScroll);
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(onResize).observe(box);
    else global.addEventListener("resize", onResize);

    // 阅读计时的"人在场"信号：滚动 / 点按 / 键盘 / 移动鼠标都算活动；90s 无操作就停表
    state.lastActive = Date.now();
    var moveTick = 0;
    global.addEventListener("pointermove", function () {
      var now = Date.now();
      if (now - moveTick > 500) { moveTick = now; noteActivity(); }
    }, { passive: true });
    ["pointerdown", "keydown", "wheel"].forEach(function (name) {
      global.addEventListener(name, noteActivity, { passive: true });
    });

    el("pdfv-in").addEventListener("click", function () { applyScale(state.scale * 1.2, null, false); });
    el("pdfv-out").addEventListener("click", function () { applyScale(state.scale / 1.2, null, false); });
    el("pdfv-label").addEventListener("click", function () { gotoFit(true); });
    el("pdfv-fit").addEventListener("click", function () { gotoFit(true); });
    el("pdfv-rotate").addEventListener("click", rotate);

    var pageInput = el("pdfv-pageno");
    pageInput.addEventListener("change", function () {
      var value = parseInt(pageInput.value, 10);
      if (value) gotoPage(value);
    });
    pageInput.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      pageInput.blur();
      pageInput.dispatchEvent(new Event("change"));
    });

    var findInput = el("pdfv-find");
    var runFind = function (direction) { find(findInput.value, direction); };
    // 中文输入法用回车上屏。这个回车不能拿去查找，否则拼音还没变成汉字就被清掉或搜错。
    bindImeKey(findInput, function (event) {
      if (event.key === "Escape") { findInput.value = ""; runFind("next"); findInput.blur(); return; }
      if (event.key !== "Enter") return;
      event.preventDefault();
      runFind(event.shiftKey ? "prev" : "next");
    });
    el("pdfv-find-next").addEventListener("click", function () { runFind("next"); });
    el("pdfv-find-prev").addEventListener("click", function () { runFind("prev"); });
  }

  global.PdfView = { open: open, close: close, gotoPage: gotoPage, init: bind,
                     isVisible: function () { return !!state.paperId; },
                     current: function () { return { paperId: state.paperId, url: state.url }; },
                     set onProgress(fn) { state.onProgress = fn; } };
})(window);
