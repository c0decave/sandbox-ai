"""The image recipe android-dev and the preset android-development end to end through ./sandbox-ai (user 2026-11-21:
"a full blown android preset for android dev" with one image that has every Android development tool): a user's host
with only Podman faked (tests/live_fakes.py). The recipe makes the variant, `presets` lists the preset, `presets
check` names the variant until it is made, the build builds it, and a dry-run start with the preset is the variant's
image with the Android hosts, the host's adb server and /dev/kvm as asked. The image itself (4 GB of Google's
downloads) is built only by a real build; its pins are checked in tests/test_images.py."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from live_fakes import Host  # noqa: E402

HOSTS = ["dl.google.com", "maven.google.com", "services.gradle.org", "downloads.gradle.org", "plugins.gradle.org",
         "plugins-artifacts.gradle.org", "repo.maven.apache.org", "repo1.maven.org"]


@pytest.fixture
def host():
    directory = tempfile.mkdtemp(prefix="androiddev-", dir=os.environ.get("TMPDIR") or "/tmp")
    found = Host(directory)
    found.add_image("localhost/claude-sandbox:latest", "localhost/claude-sandbox:base",
                    "localhost/codex-sandbox:latest", "localhost/codex-sandbox:base")
    yield found
    subprocess.run(["rm", "-rf", directory], check=False)


def ok(done):
    assert done.returncode == 0, (done.stdout[-1500:], done.stderr[-3000:])
    return done.stdout


def test_the_android_development_preset_from_recipe_to_start(host):
    # The preset is listed and shown with what it needs.
    listed = ok(host.run("presets"))
    assert "android-development" in listed
    shown = ok(host.run("presets", "show", "android-development"))
    assert "images recipe android-dev --from AGENT && sandbox-ai build android-dev" in shown
    assert "value kvm: /dev/kvm for the Android emulator" in shown and "(default /dev/kvm) (optional)" in shown
    assert "android-development.rules.json" in shown
    # Before the variant is made, check names the recipe; the start names it too.
    check = host.run("presets", "check", "android-development", "--agent", "claude", "--value", "kvm=")
    assert check.returncode != 0
    assert "sandbox-ai images recipe android-dev --from claude && sandbox-ai build android-dev" in \
        check.stdout + check.stderr
    refused = host.run("claude", "--preset", "android-development", "--preset-value", "kvm=", "--dry-run")
    assert refused.returncode != 0
    assert "images recipe android-dev --from claude && sandbox-ai build android-dev" in refused.stderr
    # The recipe makes the variant; the build builds it (Podman faked) as the variant's own image.
    made = ok(host.run("images", "recipe", "android-dev", "--from", "claude"))
    assert "build it: sandbox-ai build android-dev" in made
    recipes = ok(host.run("images", "recipe", "--help"))
    assert "android-dev:" in recipes and "android:" in recipes and "developer.android.com/studio/terms" in recipes
    shown = json.loads(ok(host.run("images", "extend", "android-dev", "--show", "--json")))
    assert shown["findings"] == [] and "RUN sh /opt/android-dev/install.sh" in shown["containerfile"]
    assert "apktool --version" in shown["containerfile"], "everything of android in it"
    built = host.run("build", "android-dev", "--build-arg", "CLAUDE_VERSION=1.2.3", timeout=600)
    assert built.returncode == 0, built.stderr[-3000:]
    tags = [call[call.index("--tag") + 1] for call in host.builds() if "--tag" in call]
    assert "localhost/claude-sandbox-android-dev:latest" in tags
    assert ok(host.run("presets", "check", "android-development", "--agent", "claude", "--value", "kvm="))
    # The start: the variant's image, the Android hosts and nothing wider, the host's adb server, no /dev/kvm when
    # it is left out; codex takes claude's variant over.
    plan = json.loads(ok(host.run("claude", "--preset", "android-development", "--preset-value", "kvm=",
                                  "--dry-run")))
    assert plan["image"] == "localhost/claude-sandbox-android-dev:latest"
    assert set(HOSTS) <= set(plan["https_hosts"]) and plan["host_wildcards"] == []
    assert plan["host_ports"] == [5037] and plan["devices"] == [] and plan["mcp"] == []
    assert plan["limits"].get("memory") in ("16g", None)  # (None: a host whose cgroups give no memory limit)
    assert "android-emulator start" in plan["agent_args"][-1]
    assert any(str(ruleset).endswith("android-development.rules.json") for ruleset in
               json.dumps(plan["rulesets"]).split('"')), plan["rulesets"]
    other = host.run("codex", "--preset", "android-development", "--preset-value", "kvm=", "--dry-run")
    assert other.returncode == 0, other.stderr[-2000:]
    assert json.loads(other.stdout)["image"] == "localhost/codex-sandbox-android-dev:latest"
