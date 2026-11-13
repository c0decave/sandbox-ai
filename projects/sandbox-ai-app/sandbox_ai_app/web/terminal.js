"use strict";
// The page side of the terminal bridge: xterm.js shows the output of the pseudo-terminal and sends keys
// to it. Bytes cross the QWebChannel as base64 text in both directions.
//
// Copy, paste and links go through the app's "helper" object (terminal_tools.py): the page never reads or
// writes the clipboard itself. Copy hands the selection (row by row, so the app can join what tmux or an
// agent's interface drew as rows of their own) to helper.copy; every paste - Ctrl+Shift+V, Cmd+V,
// Shift+Insert, a middle click, the browser's own paste - asks helper.paste, which checks the text and sends
// it back through helper.pasteText for xterm.js's paste() (bracketed paste when the program asked for it).
// Links: the rows around the mouse go to helper.links (URLs over several rows joined); Ctrl+click (Cmd+click)
// opens one through helper.openLink, and so does an OSC 8 hyperlink. OSC 52 (a program sets the clipboard)
// goes to helper.osc52, which never answers a request to read it.
(function () {
  const MAC = /Mac|iPhone|iPad/.test(navigator.platform || "");
  const LINK_ROWS = 12;  // rows above and below the mouse handed over to find a link in
  const OSC52_LIMIT = 1500000;  // characters of an OSC 52 request handed on (the app takes 1 MiB of text)

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

  // One cell, one character: a wide character's second cell is "\0", a cell of several code points one
  // replacement character - so the app counts columns as characters.
  function rowText(line, cols) {
    if (!line) {
      return "";
    }
    if (typeof line.getCell !== "function") {
      return line.translateToString(false);
    }
    let text = "";
    for (let x = 0; x < cols; x++) {
      const cell = line.getCell(x);
      if (!cell) {
        text += " ";
        continue;
      }
      const chars = cell.getChars();
      if (cell.getWidth() === 0) {
        text += "\0";
      } else if (!chars) {
        text += " ";
      } else {
        text += Array.from(chars).length === 1 ? chars : "�";
      }
    }
    return text;
  }

  new QWebChannel(qt.webChannelTransport, function (channel) {
    const pty = channel.objects.pty;
    const helper = channel.objects.helper || null;
    const chosen = chooseTheme(pty.theme);
    const light = chosen.light;
    document.body.style.background = chosen.colors.background;
    if (light) {
      document.body.className = "light";
    }
    const TerminalClass = (window.Terminal && window.Terminal.Terminal) || window.Terminal;
    const FitClass = (window.FitAddon && window.FitAddon.FitAddon) || window.FitAddon;
    const hoverBox = document.getElementById("hover");
    let hovered = null;  // the link under the mouse, for the context menu's Copy link

    function modifierHeld(event) {
      return !!event && (MAC ? event.metaKey : event.ctrlKey);
    }

    function showHover(uri) {
      hovered = uri;
      if (hoverBox) {
        hoverBox.textContent = uri + (MAC ? "  —  ⌘+click" : "  —  Ctrl+click");
        hoverBox.hidden = false;
      }
    }

    function hideHover(uri) {
      if (uri === undefined || uri === hovered) {
        hovered = null;
        if (hoverBox) {
          hoverBox.hidden = true;
        }
      }
    }

    function openLink(event, uri) {
      if (!helper || !/^https?:\/\//i.test(uri)) {
        return;
      }
      if (modifierHeld(event)) {
        helper.openLink(uri);
      } else {
        showHover(uri);
      }
    }

    const term = new TerminalClass({
      cursorBlink: true, fontFamily: chosen.fontFamily, fontSize: 13, scrollback: 10000,
      theme: chosen.colors, macOptionClickForcesSelection: true, rightClickSelectsWord: false,
      // OSC 8 hyperlinks (Claude Code's login prints its link so): opened by the app, never by the page.
      linkHandler: {
        activate: function (event, uri) { openLink(event, uri); },
        hover: function (event, uri) { showHover(uri); },
        leave: function (event, uri) { hideHover(uri); },
        allowNonHttpProtocols: false
      }
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

    // -- the selection, row by row --

    function selection() {
      const position = typeof term.getSelectionPosition === "function" ? term.getSelectionPosition() : undefined;
      if (!position) {
        const text = typeof term.getSelection === "function" ? term.getSelection() : "";
        return text ? { text: text } : null;
      }
      const buffer = term.buffer.active;
      const cols = term.cols;
      const segments = [];
      for (let y = position.start.y; y <= position.end.y; y++) {
        const line = buffer.getLine(y);
        if (!line) {
          continue;
        }
        const start = y === position.start.y ? position.start.x : 0;
        const end = y === position.end.y ? position.end.x : cols;
        const next = buffer.getLine(y + 1);
        const last = typeof line.getCell === "function" ? line.getCell(cols - 1) : null;
        segments.push({
          text: line.translateToString(false, start, end),
          wrapped: y < position.end.y && !!next && next.isWrapped === true,
          full: end >= cols && !!last && !!last.getChars() && last.getChars() !== " "
        });
      }
      return { segments: segments, cols: cols };
    }

    function copy() {
      const chosenText = selection();
      if (helper && chosenText) {
        helper.copy(JSON.stringify(chosenText));
      }
    }

    let selectTimer = null;
    if (typeof term.onSelectionChange === "function") {
      term.onSelectionChange(function () {
        clearTimeout(selectTimer);
        selectTimer = setTimeout(function () {
          const chosenText = term.hasSelection() ? selection() : null;
          if (helper && chosenText) {
            helper.selected(JSON.stringify(chosenText));
          }
        }, 200);
      });
    }

    // -- pasting: always through the app --

    function bracketed() {
      return !!(term.modes && term.modes.bracketedPasteMode);
    }

    function requestPaste(source) {
      if (helper) {
        helper.paste(source, bracketed());
      }
    }

    if (helper) {
      helper.pasteText.connect(function (text) {
        term.paste(text);
        term.focus();
      });
    }

    const host = document.getElementById("terminal");
    let middleAt = 0;
    host.addEventListener("mousedown", function (event) {
      if (event.button === 1) {
        middleAt = Date.now();
      }
    }, true);
    // The browser's own paste (a middle click on Linux, its menu): never handed to xterm.js directly.
    host.addEventListener("paste", function (event) {
      event.preventDefault();
      event.stopImmediatePropagation();
      const fromMiddle = Date.now() - middleAt < 1000;
      if (fromMiddle && term.modes && term.modes.mouseTrackingMode !== "none") {
        return;  // the program takes the mouse: the middle click is its
      }
      requestPaste(fromMiddle ? "selection" : "clipboard");
    }, true);
    host.addEventListener("drop", function (event) { event.preventDefault(); }, true);

    // -- keys: Ctrl+Shift+C/V (Cmd+C/V on macOS), Ctrl+Insert, Shift+Insert --

    function action(event) {
      const key = (event.key || "").toLowerCase();
      if (MAC && event.metaKey && !event.ctrlKey && !event.altKey) {
        if (key === "c") { return term.hasSelection() ? "copy" : "skip"; }
        if (key === "v") { return "paste"; }
        if (key === "a") { return "all"; }
      }
      if (!MAC && event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey) {
        if (key === "c") { return "copy"; }
        if (key === "v") { return "paste"; }
      }
      if (event.ctrlKey && !event.shiftKey && !event.altKey && key === "insert") { return "copy"; }
      if (event.shiftKey && !event.ctrlKey && !event.altKey && key === "insert") { return "paste"; }
      return null;
    }

    if (typeof term.attachCustomKeyEventHandler === "function") {
      term.attachCustomKeyEventHandler(function (event) {
        const chosenAction = action(event);
        if (chosenAction === null) {
          return true;
        }
        if (event.type === "keydown") {
          event.preventDefault();
          if (chosenAction === "copy") { copy(); }
          if (chosenAction === "paste") { requestPaste("clipboard"); }
          if (chosenAction === "all") { term.selectAll(); }
        }
        return false;  // (keydown, keypress and keyup of it: none goes to the program)
      });
    }

    // -- links: also a URL drawn over rows of their own (tmux, Claude Code's interface) --

    const linkProvider = {
      provideLinks: function (y, callback) {
          if (!helper) {
            callback(undefined);
            return;
          }
          const buffer = term.buffer.active;
          const target = y - 1;
          const first = Math.max(0, target - LINK_ROWS);
          const last = Math.min(buffer.length - 1, target + LINK_ROWS);
          const rows = [];
          const wrapped = [];
          for (let i = first; i <= last; i++) {
            const line = buffer.getLine(i);
            rows.push(rowText(line, term.cols));
            wrapped.push(!!line && line.isWrapped === true);
          }
          const request = JSON.stringify({ first: first, cols: term.cols, rows: rows, wrapped: wrapped,
                                           target: target });
          helper.links(request, function (answer) {
            let found = [];
            try { found = JSON.parse(answer); } catch (error) { found = []; }
            callback(found.map(function (link) {
              return {
                text: link.url,
                range: { start: { x: link.start[1] + 1, y: link.start[0] + 1 },
                         end: { x: link.end[1] + 1, y: link.end[0] + 1 } },
                decorations: { underline: true, pointerCursor: true },
                activate: function (event) { openLink(event, link.url); },
                hover: function () { showHover(link.url); },
                leave: function () { hideHover(link.url); }
              };
            }));
          });
      }
    };
    if (helper && typeof term.registerLinkProvider === "function") {
      term.registerLinkProvider(linkProvider);
    }

    // -- OSC 52: a program sets the clipboard (the app asks first; a read is never answered) --

    if (helper && term.parser && typeof term.parser.registerOscHandler === "function") {
      term.parser.registerOscHandler(52, function (data) {
        if (data.length <= OSC52_LIMIT) {
          helper.osc52(data);
        }
        return true;
      });
    }

    // For tests and for the app: the visible text of the terminal, and what the menu and a sign-in do.
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
      fontFamily: function () { return term.options.fontFamily; },
      menuState: function () {
        return JSON.stringify({ selection: typeof term.hasSelection === "function" && term.hasSelection(),
                                link: hovered });
      },
      copy: copy,
      paste: function () { requestPaste("clipboard"); },
      selectAll: function () { if (typeof term.selectAll === "function") { term.selectAll(); } },
      select: function (column, row, length) { term.select(column, row, length); },
      // (tests: an OSC 8 hyperlink clicked, with Ctrl/Cmd held or not - what xterm.js calls on a click)
      activateLink: function (uri, held) {
        term.options.linkHandler.activate({ ctrlKey: held, metaKey: held }, uri);
      },
      links: function (y, callback) {  // (tests: what the link provider finds in buffer row Y, 1-based)
        linkProvider.provideLinks(y, function (found) {
          window.sandboxLinks = (found || []).map(function (link) { return [link.text, link.range]; });
        });
      },
      clearSelection: function () { if (typeof term.clearSelection === "function") { term.clearSelection(); } },
      // A sign-in's result: pasted as the program asked (bracketed or not), then Enter.
      pasteLine: function (text) {
        term.paste(String(text));
        pty.write(encodeText("\r"));
        term.focus();
      },
      bracketed: bracketed
    };
  });
})();
