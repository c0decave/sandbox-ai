"use strict";
// The page side of the terminal bridge: xterm.js shows the output of the pseudo-terminal and sends keys
// to it. Bytes cross the QWebChannel as base64 text in both directions.
(function () {
  function encodeText(text) {
    const bytes = new TextEncoder().encode(text);
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  }

  function decode(encoded) {
    const binary = atob(encoded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  const themes = {
    dark: { background: "#1e1e1e", foreground: "#d4d4d4", cursor: "#d4d4d4", selectionBackground: "#264f78" },
    light: { background: "#ffffff", foreground: "#1e1e1e", cursor: "#1e1e1e", selectionBackground: "#add6ff" }
  };

  // The app's theme: "dark", "light", or JSON with xterm.js colours, a font and whether it is light.
  function chooseTheme(value) {
    if (themes[value]) {
      return { colors: themes[value], light: value === "light", fontFamily: "monospace" };
    }
    try {
      const parsed = JSON.parse(value);
      const colors = {};
      ["background", "foreground", "cursor", "selectionBackground"].forEach(function (key) {
        if (typeof parsed[key] === "string" && /^#[0-9a-fA-F]{6}$/.test(parsed[key])) {
          colors[key] = parsed[key];
        }
      });
      const font = typeof parsed.fontFamily === "string" && /^[\w ,"-]{1,200}$/.test(parsed.fontFamily)
        ? parsed.fontFamily + ", monospace" : "monospace";
      return { colors: Object.assign({}, themes.dark, colors), light: parsed.light === true, fontFamily: font };
    } catch (error) {
      return { colors: themes.dark, light: false, fontFamily: "monospace" };
    }
  }

  new QWebChannel(qt.webChannelTransport, function (channel) {
    const pty = channel.objects.pty;
    const chosen = chooseTheme(pty.theme);
    const light = chosen.light;
    document.body.style.background = chosen.colors.background;
    if (light) {
      document.body.className = "light";
    }
    const TerminalClass = (window.Terminal && window.Terminal.Terminal) || window.Terminal;
    const FitClass = (window.FitAddon && window.FitAddon.FitAddon) || window.FitAddon;
    const term = new TerminalClass({
      cursorBlink: true, fontFamily: chosen.fontFamily, fontSize: 13, scrollback: 10000,
      theme: chosen.colors
    });
    const fit = new FitClass();
    term.loadAddon(fit);
    term.open(document.getElementById("terminal"));
    term.onData(function (data) { pty.write(encodeText(data)); });
    term.onBinary(function (data) { pty.write(btoa(data)); });
    term.onResize(function (size) { pty.resize(size.cols, size.rows); });
    // Each chunk is acknowledged once xterm.js has processed it: the bridge stops reading the terminal while
    // too much is not shown yet, so a flood of output never piles up here.
    pty.output.connect(function (encoded) {
      term.write(decode(encoded), function () { pty.ack(encoded.length); });
    });
    function refit() {
      try { fit.fit(); } catch (e) { /* not laid out yet */ }
    }
    window.addEventListener("resize", refit);
    refit();
    pty.resize(term.cols, term.rows);
    pty.ready();
    term.focus();
    // For tests and for the app: the visible text of the terminal.
    window.sandboxTerminal = {
      text: function () {
        const buffer = term.buffer.active;
        const lines = [];
        for (let i = 0; i < buffer.length; i++) {
          const line = buffer.getLine(i);
          lines.push(line ? line.translateToString(true) : "");
        }
        return lines.join("\n");
      },
      type: function (text) {
        if (typeof term.input === "function") {
          term.input(text, true);
        } else {
          pty.write(encodeText(text));
        }
      },
      size: function () { return [term.cols, term.rows]; },
      fontFamily: function () { return term.options.fontFamily; }
    };
  });
})();
