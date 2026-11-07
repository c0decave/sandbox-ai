// Stand-in for @xterm/addon-fit in tests: "fits" to a fixed size.
(function () {
  class FitAddon {
    activate(terminal) { this.terminal = terminal; }
    fit() { this.terminal.resize(100, 30); }
    dispose() {}
  }
  window.FitAddon = { FitAddon: FitAddon };
})();
