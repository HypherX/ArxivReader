"use strict";

const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const context = {};
context.window = context;
context.globalThis = context;
context.setTimeout = function (fn, ms) { return setTimeout(fn, ms); };
vm.createContext(context);
vm.runInContext(fs.readFileSync("static/pdf-find.js", "utf8"), context);
const matchPage = context.PdfFind.matchPage;

function same(actual, expected) {
  assert.strictEqual(JSON.stringify(actual), JSON.stringify(expected));
}

const cjk = matchPage([{ str: "论" }, { str: "文" }, { str: "阅" }, { str: "读" }], "论文");
same(cjk, [
  [{ div: 0, start: 0, end: 1 }, { div: 1, start: 0, end: 1 }],
]);
same(matchPage([{ str: "论" }, { str: "文" }], "论 文"), []);

const phrase = matchPage([{ str: "Hello " }, { str: "World" }], "hello world");
same(phrase, [
  [{ div: 0, start: 0, end: 6 }, { div: 1, start: 0, end: 5 }],
]);
same(matchPage([{ str: "Hello" }, { str: "World" }], "hello world"), []);

const twice = matchPage([{ str: "alpha alpha" }], "Alpha");
assert.strictEqual(twice.length, 2);
same(twice[0], [{ div: 0, start: 0, end: 5 }]);
same(twice[1], [{ div: 0, start: 6, end: 11 }]);

const split = matchPage([{ str: "att" }, { type: "mark" }, { str: "ention" }], "attention");
same(split, [
  [{ div: 0, start: 0, end: 3 }, { div: 1, start: 0, end: 6 }],
]);

const dotted = matchPage([{ str: "İ" }], "İ");
same(dotted, [[{ div: 0, start: 0, end: 1 }]]);

same(matchPage([], "aa"), []);
same(matchPage([{ str: "attention" }], "  "), []);

let sent = 0;
const input = {
  listeners: {},
  addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); },
};
context.bindImeKey(input, function () { sent += 1; });
function fire(type, event) {
  (input.listeners[type] || []).forEach(function (fn) { fn(event); });
}
fire("compositionstart", {});
fire("keydown", { key: "Enter", isComposing: true, keyCode: 229 });
assert.strictEqual(sent, 0);
fire("compositionend", {});
fire("keydown", { key: "Enter", isComposing: false, keyCode: 13 });
assert.strictEqual(sent, 0);
setTimeout(function () {
  fire("keydown", { key: "Enter", isComposing: false, keyCode: 13 });
  assert.strictEqual(sent, 1);
  fire("keydown", { key: "Enter", shiftKey: true, keyCode: 13 });
  assert.strictEqual(sent, 2);
}, 0);
