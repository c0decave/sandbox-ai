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
      this.selected = "";
      this.modes = { bracketedPasteMode: false, mouseTrackingMode: "none" };
      this.oscHandlers = {};
      this.linkProviders = [];
      const self = this;
      this.buffer = { active: {
        get length() { return self.lines.length; },
        getLine: function (i) { return { translateToString: function () { return self.lines[i]; } }; }
      } };
      this.parser = { registerOscHandler: function (code, f) { self.oscHandlers[code] = f;
                                                               return { dispose: function () {} }; } };
      window.fakeTerminal = this;
    }
    open(element) { this.element = element; }
    write(data, callback) {
      if (typeof data !== "string") { data = new TextDecoder().decode(data); }
      // OSC sequences go to their handlers; ?2004h and ?2004l switch bracketed paste
      const self = this;
      data = data.replace(/\x1b\](\d+);([^\x07\x1b]*)(?:\x07|\x1b\\)/g, function (_all, code, payload) {
        if (self.oscHandlers[code]) { self.oscHandlers[code](payload); }
        return "";
      });
      data = data.replace(/\x1b\[\?2004([hl])/g, function (_all, mode) {
        self.modes.bracketedPasteMode = mode === "h";
        return "";
      });
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
    paste(text) {
      text = text.replace(/\r?\n/g, "\r");
      if (this.modes.bracketedPasteMode) { text = "\x1b[200~" + text + "\x1b[201~"; }
      this.input(text);
    }
    select(column, row, length) { this.selected = (this.lines[row] || "").substr(column, length); }
    selectAll() { this.selected = this.lines.join("\n"); }
    clearSelection() { this.selected = ""; }
    hasSelection() { return this.selected !== ""; }
    getSelection() { return this.selected; }
    attachCustomKeyEventHandler(f) { this.keyHandler = f; }
    registerLinkProvider(provider) { this.linkProviders.push(provider); return { dispose: function () {} }; }
  }
  window.Terminal = Terminal;
})();
