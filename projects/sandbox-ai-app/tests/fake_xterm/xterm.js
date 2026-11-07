// A stand-in for xterm.js in tests (the real one is vendored by scripts/vendor_xterm.py): the same small
// part of the API that terminal.js uses, keeping the written text as lines.
(function () {
  class Terminal {
    constructor(options) {
      this.options = options || {};
      this.cols = 80;
      this.rows = 24;
      this.lines = [""];
      this.handlers = { data: [], binary: [], resize: [] };
      const self = this;
      this.buffer = { active: {
        get length() { return self.lines.length; },
        getLine: function (i) { return { translateToString: function () { return self.lines[i]; } }; }
      } };
    }
    open(element) { this.element = element; }
    write(data, callback) {
      if (typeof data !== "string") { data = new TextDecoder().decode(data); }
      for (const c of data) {
        if (c === "\n") { this.lines.push(""); }
        else if (c !== "\r") { this.lines[this.lines.length - 1] += c; }
      }
      if (this.element) { this.element.textContent = this.lines.join("\n"); }
      if (callback) { setTimeout(callback, 0); }  // as xterm.js: once the data is processed
    }
    on(kind, f) { this.handlers[kind].push(f); return { dispose: function () {} }; }
    onData(f) { return this.on("data", f); }
    onBinary(f) { return this.on("binary", f); }
    onResize(f) { return this.on("resize", f); }
    resize(cols, rows) {
      this.cols = cols; this.rows = rows;
      this.handlers.resize.forEach(function (f) { f({ cols: cols, rows: rows }); });
    }
    loadAddon(addon) { addon.activate(this); }
    focus() {}
    input(text) { this.handlers.data.forEach(function (f) { f(text); }); }
  }
  window.Terminal = Terminal;
})();
