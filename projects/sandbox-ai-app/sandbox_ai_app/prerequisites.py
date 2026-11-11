"""What a start needs on its host before it can run - an identity, an image variant (made from its recipe and built),
a VPN profile, MCP servers - found and provided instead of left for the launcher to refuse: for the Board and the
Start page alike (every preset and profile). A page mixes Prerequisites in and gives prerequisite_argv() (its
launcher options), prerequisite_agent(), host() and, optionally, show_notice(text, role)."""
from __future__ import annotations

import json

from . import applog, board as B
from .files import printable
from .i18n import tr
from .widgets import background, confirm, warn

LOCAL = "local"  # this machine, as `sandbox-ai hosts` names it


class Prerequisites:
    """The mixin (see the module's docstring)."""

    PREREQUISITES = ("--identity", "--image-variant", "--vpn", "--mcp")

    def board_needs(self):
        """{option: [names]} of what the start uses that must exist on its host first: --identity, --image-variant,
        --vpn, --mcp (placeholders left out: they are asked for first)."""
        argv = self.prerequisite_argv()
        own = set(self.prerequisite_own_servers())
        needs = {}
        for index, token in enumerate(argv):
            option, _, value = token.partition("=")
            if option in self.PREREQUISITES:
                value = value if "=" in token else (argv[index + 1] if index + 1 < len(argv) else "")
                if value and not B.PLACEHOLDER.search(value):
                    if option == "--mcp" and value in own:
                        continue  # the project's own server: defined by its project file, nothing to add
                    needs.setdefault(option, []).append(value)
        return needs

    def prerequisite_own_servers(self):
        """The MCP servers the start's project defines itself (none known: a page may say)."""
        return set()

    def prerequisite_context(self):
        """What the start is about (a board, a project, a host): a continuation made for another is dropped."""
        return self.host()

    def check_prerequisites(self, then=None, why="start"):
        """Everything the board needs that its host does not have yet - an identity, an image variant (made, or only
        not built), a VPN profile, an MCP server: a preset's "investigation" identity, "tor" variant and Tor profile,
        say - read in the background, then provided (provide()). THEN runs after it all is there (the start goes on);
        nothing is left for the launcher to refuse. True while that is under way."""
        needs = self.board_needs()
        if not needs or not hasattr(self.sai, "host_image_variants"):
            return False
        host = self.host()
        context = self.prerequisite_context()
        busy = self.__dict__.setdefault("preparing_contexts", set())
        if context in busy:  # (this start's needs are read or made already; another board or host is not held up)
            self.prerequisite_notice(tr("Still preparing what the start needs…"), "notice-warn")
            return True
        key = (host, json.dumps(needs, sort_keys=True))
        if getattr(self, "prerequisites_done", None) == key:
            return False
        busy.add(context)  # from the read on: a second Start meanwhile starts nothing twice
        agent = self.prerequisite_agent()  # (the start's, also when the page shows another one by the answer)

        def same():
            return self.prerequisite_context() == context

        def go_on():
            """THEN, unless the page now shows another start (another board, project or host): never one the human
            did not start."""
            if then is not None and same():
                then()

        def read():
            """{option: {names there}} on HOST for the options of NEEDS ("--image-variant": {name: built}; None: not
            answered - the launcher says what is missing at the start)."""
            def variants():
                data = self.sai.host_json(host, "images", "--json", timeout=90)
                return {str(e["target"]): bool(e.get("present")) for e in data if isinstance(e, dict)
                        and e.get("variant") and isinstance(e.get("target"), str)}
            readers = {"--identity": lambda: self.sai.identities() if host == LOCAL else self.sai.host_identities(host),
                       "--image-variant": variants,
                       "--vpn": lambda: [e["profile"] for e in self.sai.host_vpn_profiles(host)],
                       "--mcp": lambda: [row[0] for row in self.sai.host_mcp_servers(host)]}
            have = {}
            for option in needs:
                try:
                    found = readers[option]()
                    have[option] = found if isinstance(found, dict) and option == "--image-variant" else set(found)
                except Exception:  # noqa: BLE001
                    have[option] = None
            return have

        def done(have, error):
            if not same():  # the page shows another start now: nothing is asked or made for this one
                busy.discard(context)
                return
            complete = error is None and isinstance(have, dict) and all(have.get(o) is not None for o in needs)
            if error is not None or not isinstance(have, dict):
                have = {}
            missing, unbuilt = {}, []
            for option, names in needs.items():
                there = have.get(option)
                if there is None:
                    continue
                for name in names:
                    if name not in there:
                        missing.setdefault(option, []).append(name)
                    elif option == "--image-variant" and not there[name]:
                        unbuilt.append(name)
            if not missing and not unbuilt:
                if complete:  # (one that could not be read is read again at the next start)
                    self.prerequisites_done = key
                busy.discard(context)
                go_on()
                return
            self.provide(missing, unbuilt, host, key, go_on if then is not None else None, why, context, agent)
        background(read, owner=self, timeout=180, done=done)
        return True

    def provide(self, missing, unbuilt, host, key, then, why, context=None, agent=None):
        """Make what MISSING ({option: [names]}) lacks on HOST: identities at once (empty: no logins, none of yours);
        image variants of a recipe made, and built with those only defined (UNBUILT) - after one question, in the
        background, the output in the log window; a VPN profile as a Tor profile (nothing to fill in) or on the VPN
        page; MCP servers added from their catalog. Then THEN, once everything is there."""
        agent = agent or self.prerequisite_agent()
        context = context if context is not None else self.prerequisite_context()
        busy = self.__dict__.setdefault("preparing_contexts", set())
        recipes = set()
        try:
            recipes = set(self.sai.module("images").RECIPES)
        except Exception:  # noqa: BLE001 - an older sandbox-ai: no recipes known here
            pass
        identities = list(missing.get("--identity", []))
        variants = [v for v in missing.get("--image-variant", []) if v in recipes]
        no_recipe = [v for v in missing.get("--image-variant", []) if v not in recipes]
        vpns, servers = list(missing.get("--vpn", [])), list(missing.get("--mcp", []))
        problems = []
        if no_recipe:
            problems.append(tr("Image variant {names}: no recipe of that name - make it on the Images page "
                               "(sandbox-ai images new NAME --from {agent}).", names=", ".join(no_recipe), agent=agent))
        tor_profiles, vpn_page = [], []
        for name in vpns:
            choice = self.ask_vpn_kind(name, host)
            (tor_profiles if choice == "tor" else vpn_page).append(name)
        if vpn_page:
            problems.append(tr("VPN profile {names}: make it on the VPN page (it needs its keys or login), then "
                               "start again.", names=", ".join(vpn_page)))
        to_build = list(dict.fromkeys(unbuilt + variants))
        if to_build and not confirm(self, tr("Image variant"), tr(
                "This start needs the image variant {names}{made}. Build it now? It takes some minutes and room on "
                "the disk; the output goes to the log window, and the start goes on when it is built.",
                names=", ".join(to_build), made=tr(" (made from its recipe first)") if variants else ""),
                tr("Build")):
            problems.append(tr("Image variant {names}: not built (sandbox-ai build --variant NAME).",
                               names=", ".join(to_build)))
            to_build = []
        if servers and not confirm(self, tr("MCP servers"), tr(
                "This start uses the MCP servers {names}, which are not added on {host}. Add them from their catalogs "
                "now (their images are pulled)?", names=", ".join(servers), host=host), tr("Add")):
            problems.append(tr("MCP servers {names}: not added (the MCP page adds them from a catalog).",
                               names=", ".join(servers)))
            servers = []
        if problems and why == "start":
            warn(self, tr("Not started"), "\n".join(problems))
            if not (identities or variants or to_build or tor_profiles or servers):
                busy.discard(context)
                return
            then = None  # what can be made is made; the start waits for the rest
        elif problems:
            warn(self, tr("Preparing the start"), "\n".join(problems))
        busy.add(context)
        self.prerequisite_notice(tr("Preparing what the start needs…"), "notice-warn")

        def work():
            def run(*args, timeout=600):
                status, out, err = self.sai.host_run(host, *args, timeout=timeout)
                text = (out + err).strip()
                applog.log("INFO" if status == 0 else "ERROR", "board", f"sandbox-ai {' '.join(args)}: exit {status}\n"
                           + text[-20000:])
                return status, text
            said = []
            for name in identities:
                said.append(("identity", name, *run("identities", "add",
                                                    "--description=made for a start (no logins)", "--",
                                                    name, timeout=60)))
            for name in variants:
                said.append(("variant", name, *run("images", "recipe", name, "--from", agent, timeout=120)))
            for name in to_build:
                if any(kind == "variant" and n == name and status != 0 for kind, n, status, _ in said):
                    continue
                said.append(("build", name, *run("build", "--variant", name, timeout=3600)))
            for name in tor_profiles:
                status, text = run("vpn", "add", name, "--tor", "--socks", timeout=120)
                if status != 0 and "build tor" in text:  # the Tor VPN's image first, then the profile again
                    said.append(("build", "tor (VPN)", *run("build", "tor", timeout=3600)))
                    status, text = run("vpn", "add", name, "--tor", "--socks", timeout=120)
                said.append(("vpn", name, status, text))
            for name in servers:
                said.append(("mcp", name, *run("mcp", "add", name, timeout=900)))
            return said

        def made(said, error):
            busy.discard(context)
            if error is not None:
                warn(self, tr("Preparing the start"), printable(error))
                return
            words = {"identity": tr("identity"), "variant": tr("image variant"), "build": tr("built"),
                     "vpn": tr("Tor profile"), "mcp": tr("MCP server")}
            ok = [f"{words[kind]} {name}" for kind, name, status, _ in said if status == 0]
            failed = [f"{words[kind]} {name}: {printable(text.splitlines()[-1] if text else '')[-300:]}"
                      for kind, name, status, text in said if status != 0]
            if ok:
                self.prerequisite_notice(tr("Made for the start: {list}.", list=printable(", ".join(ok))), "notice-ok")
            if failed:
                warn(self, tr("Preparing the start"), tr("Not everything could be made (the log window has the output):") +
                     "\n" + "\n".join(failed))
                return
            self.prerequisites_done = None  # read again: what was made is there now
            if self.prerequisite_context() != context:
                return  # another start is shown now: nothing more is made or started for this one
            # (a board edited meanwhile to need nothing any more starts at once)
            if then is not None and not self.check_prerequisites(then=then, why=why):
                then()
        background(work, owner=self, work=True, timeout=4 * 3600, done=made)

    def ask_vpn_kind(self, name, host):
        """"tor" when the missing VPN profile NAME is to be made as a Tor profile now (nothing to fill in), else None
        (made on the VPN page: a WireGuard, OpenVPN, Mullvad or Tailscale profile needs its keys or login)."""
        from . import widgets as W
        if W.AUTO_ANSWER is not None:
            return "tor" if W.AUTO_ANSWER else None
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(QMessageBox.Icon.Question, tr("VPN profile {name}", name=printable(name)), tr(
            "This start goes through the VPN profile {name}, which is not on {host}. A Tor profile needs nothing "
            "filled in and is made now; another kind (WireGuard, OpenVPN, Mullvad, Tailscale) needs its keys or login "
            "on the VPN page.", name=printable(name), host=printable(host)), parent=self)
        tor = box.addButton(tr("Make a Tor profile"), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(tr("I make it on the VPN page"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return "tor" if box.clickedButton() is tor else None


    def prerequisite_notice(self, text, role="notice-warn"):
        shown = getattr(self, "show_notice", None)
        if callable(shown):
            shown(text, role)
        else:
            applog.log("WARNING" if role == "notice-warn" else "INFO", "start", text)
