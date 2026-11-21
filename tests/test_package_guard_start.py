"""The package guard at a start, for every agent on both backends (the feature matrix's fixture `m`: a whole start
through main(), the backend a recorder): what the proxy is given (proxy.json "packages"), the mount of the offline
copy, the options, the modes, your defaults, rulesets and the project's allow-list."""
import json
import os
from pathlib import Path

import test_feature_matrix as fm
from test_feature_matrix import m  # noqa: F401  (fixture)

s = fm.s
rt = s.RUNTIME
packages = s.load_packages_module()

ADVISORY = {"id": "MAL-2025-1", "affected": [{"package": {"ecosystem": "npm", "name": "evil-pkg"},
                                              "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}]}]}]}


def copy_here(m):
    """A copy of the known malicious packages on this (fake) host, built from one advisory."""
    dump = m.tmp / "osv"
    dump.mkdir(exist_ok=True)
    (dump / "MAL-2025-1.json").write_text(json.dumps(ADVISORY))
    packages.sync_osv([dump], out=lambda line: None)
    return packages.data_dir()


def test_every_start_with_the_proxy_has_the_guard(m):
    run = m.run()
    assert run.proxy["packages"] == {"malicious": "deny", "min_age": "ask", "min_age_days": 14, "new_names": "report",
                                     "by": {}, "allow": []}
    assert str(rt.PACKAGES_DIR) not in run.joined  # no copy on this machine: nothing to mount
    assert m.plan()["package_guard"]["min_age_days"] == 14
    # the proxy takes the launcher's own settings as they are
    guard = rt.PackageGuard(run.proxy["packages"], {})
    assert guard.settings == rt.PACKAGE_DEFAULTS and guard.allowed == set()


def test_the_copy_is_mounted_for_the_proxy_alone(m):
    copy = copy_here(m)
    run = m.run()
    if m.backend == "podman":
        assert f"src={copy},dst={rt.PACKAGES_DIR},ro" in run.joined
    else:
        assert f"--ro-bind {copy} {rt.PACKAGES_DIR}" in run.joined
    # what was mounted is what the proxy's lists read
    assert rt.open_package_lists(copy)["npm"].find("evil-pkg") == [("MAL-2025-1", None)]
    # hidden from the agent (Firejail's blacklist of the sandbox's private folders)
    source = (Path(s.__file__).parent / "runtime.py").read_text()
    assert "MCP_UPSTREAM_DIR, VM_CONTROL_DIR,\n                    PACKAGES_DIR)" in source


def test_no_package_guard_switches_it_off_and_the_strict_modes_refuse_that(m):
    copy_here(m)
    run = m.run("--no-package-guard")
    assert "packages" not in run.proxy and str(rt.PACKAGES_DIR) not in run.joined
    assert m.plan("--no-package-guard")["package_guard"] is None
    assert m.plan("-ng")["package_guard"] is None
    for mode, laxer in (("high", "medium"), ("paranoid", "medium")):
        m.refused("--mode", mode, "--no-package-guard",
                  match=rf"--no-package-guard widens the security mode {mode} \(--mode\): without the package guard "
                        rf".*choose a laxer mode: --mode {laxer}")
    # a project file's option is refused the same way
    m.project_file({"mode": "high", "options": ["--no-package-guard"]})
    m.refused(match=r"--no-package-guard widens the security mode high \(the project file .*\)")


def test_the_strict_modes_ask_about_new_names_and_keep_the_minimum_age(m):
    fm_defaults = Path(os.environ["HOME"]) / ".config/sandbox-ai/defaults.json"
    fm_defaults.parent.mkdir(parents=True, exist_ok=True)
    assert m.run("--mode", "low").proxy["packages"]["min_age"] == "ask"
    fm_defaults.write_text(json.dumps({"version": 1, "package_min_age_days": 0}))
    assert m.run().proxy["packages"]["min_age_days"] == 0  # your own default: no age check
    for mode in ("high", "paranoid"):
        settings = m.run("--mode", mode).proxy["packages"]
        assert settings["new_names"] == "ask" and settings["min_age_days"] == 14
        assert settings["by"] == {"new_names": f"mode {mode}", "min_age_days": f"mode {mode}"}
    assert m.run("--mode", "low").proxy["packages"]["new_names"] == "report"
    # lax asks nothing: a young version is recorded there, not held - unless a ruleset says otherwise
    lax = m.run("--mode", "lax").proxy["packages"]
    assert (lax["min_age"], lax["malicious"], lax["min_age_days"]) == ("report", "deny", 0) and lax["by"] == {}
    strict = m.private_file("ask.json", json.dumps({"version": 1, "packages": {"min_age": "ask"}}))
    assert m.run("--mode", "lax", "--rules", str(strict)).proxy["packages"]["min_age"] == "ask"
    fm_defaults.write_text(json.dumps({"version": 1, "package_min_age_days": -1}))
    m.refused(match="package_min_age_days is a number of days, 0-3650")


def test_rulesets_tighten_the_guard_and_never_loosen_it(m):
    strict = m.private_file("strict.json", json.dumps({"version": 1, "packages": {
        "min_age_days": 30, "new_names": "deny"}}))
    lax = m.private_file("lax.json", json.dumps({"version": 1, "packages": {
        "min_age_days": 1, "min_age": "report", "new_names": "report"}}))
    trial = m.private_file("trial.json", json.dumps({"version": 1, "mode": "audit", "packages": {"min_age": "deny"}}))
    settings = m.run("--rules", str(strict), "--rules", str(lax), "--rules", str(trial)).proxy["packages"]
    assert (settings["min_age_days"], settings["min_age"], settings["new_names"]) == (30, "ask", "deny")
    assert settings["by"] == {"min_age_days": "--rules strict.json", "new_names": "--rules strict.json"}
    assert settings["audit"] == [{"name": "--rules trial.json", "min_age": "deny"}]
    broken = m.private_file("broken.json", json.dumps({"version": 1, "packages": {"malicious": "allow"}}))
    m.refused("--rules", str(broken), match=r'"packages" takes "malicious" \(deny, block\)')


def test_allowed_packages_of_the_start_and_of_the_project(m):
    copy_here(m)
    assert packages.allow(m.work, ["pypi:requests@2.32.0", "npm:evil-pkg"]) == (
        ["pypi:requests@2.32.0"], [("npm:evil-pkg", "MAL-2025-1")])
    run = m.run("--allow-package", "npm:Left-Pad@1.3.2", "--allow-package", "crates:serde", "-ap",
                "go:github.com/x/y@v1.0.0")
    assert run.proxy["packages"]["allow"] == ["npm:left-pad@1.3.2", "crates:serde", "go:github.com/x/y@1.0.0",
                                              "pypi:requests@2.32.0"]
    guard = rt.PackageGuard(run.proxy["packages"], rt.open_package_lists(packages.data_dir()))
    assert ("npm", "left-pad", "1.3.2") in guard.allowed and ("crates", "serde", None) in guard.allowed
    # --allow-package of a version on the list is taken as written, and the guard refuses the download all the same
    listed = m.run("--allow-package", "npm:evil-pkg")
    guard = rt.PackageGuard(listed.proxy["packages"], rt.open_package_lists(packages.data_dir()))
    verdict = guard.verdict(rt.package_request("npm", "/evil-pkg/-/evil-pkg-1.0.0.tgz"))
    assert verdict["decision"] == "deny" and verdict["package"]["malicious"] == "MAL-2025-1"
    m.refused("--allow-package", "left-pad", match=r"--allow-package .*ECOSYSTEM:NAME")
    # another project's list is not this one's
    other = m.tmp / "elsewhere"
    other.mkdir()
    packages.allow(other, ["npm:only-there"])
    assert "npm:only-there" not in m.run().proxy["packages"]["allow"]


def test_a_start_that_opens_a_registry_says_when_there_is_no_copy(m):
    assert "package guard" not in m.run().err  # the provider only: no registry is open
    err = m.run("--allow-npm").err
    assert "note: the package guard has no copy of the known malicious packages" in err
    assert "sandbox-ai packages sync" in err
    copy_here(m)
    assert "package guard" not in m.run("--allow-npm").err
    err = m.run("--allow-pypi", "--no-inspect", "files.pythonhosted.org").err
    assert "the package guard does not see downloads from files.pythonhosted.org" in err
    assert "package guard" not in m.run("--allow-npm", "--no-package-guard").err
