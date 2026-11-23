"""The web interface's words for an update check (the Doctor page's Updates on HOST, the Hosts page's version
column): the same verdicts as the command line and the app - a newer release offered only when signed by a trusted
key, one that is not says why, and an installation newer than the newest release says "nothing to update" (the
user's system installation of 0.1.193 against 0.1.188, 2026-11-22), in English and German."""
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_web import FAKE_DOM, run_driver  # noqa: E402

CASES = {
    "newer": {"installed": "0.1.180", "available": "0.1.188", "signature": "good", "newer": True},
    "bad": {"installed": "0.1.180", "available": "0.1.188", "signature": "bad", "newer": False},
    "none": {"installed": "0.1.180", "available": "0.1.188", "signature": "none", "newer": False},
    "ahead": {"installed": "0.1.193", "available": "0.1.188", "signature": "good", "newer": False},
    "same": {"installed": "0.1.188", "available": "0.1.188", "signature": "good", "newer": False},
    "failed": {"error": "cannot reach api.github.com"},
}


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.parametrize("language", ["en", "de"])
def test_the_last_check_in_words(tmp_path, language):
    driver = FAKE_DOM + f"""
(async () => {{
  if ("{language}" === "de") vm.runInContext('setLanguage("de")', context);
  context.cases = {json.dumps(CASES)};
  const said = vm.runInContext('Object.fromEntries(Object.entries(cases).map(([k, r]) => [k, updateFound(r, {{}})]))',
                               context);
  said.hostAhead = vm.runInContext('versionText({{version: "0.1.193", available: "0.1.188", update: false, ahead: true}})', context);
  said.hostSame = vm.runInContext('versionText({{version: "0.1.188", available: "0.1.188", update: false}})', context);
  console.log(JSON.stringify(said));
}})().catch(e => {{ console.error(e.stack); process.exit(1); }});
"""
    said = run_driver(tmp_path, driver, f"update-words-{language}")
    if language == "en":
        assert said == {
            "newer": "0.1.188 is available (installed: 0.1.180)",
            "bad": "0.1.188 is there, but its signature is BAD: not offered",
            "none": "0.1.188 is there, but it is not signed (no SHA256SUMS.sig): not offered",
            "ahead": "0.1.193 is newer than the newest release 0.1.188: nothing to update",
            "same": "up to date (0.1.188)",
            "failed": "failed: cannot reach api.github.com",
            "hostAhead": "0.1.193, newer than the newest release 0.1.188",
            "hostSame": "0.1.188, up to date",
        }
    else:
        assert said["ahead"] == "0.1.193 ist neuer als das neueste Release 0.1.188: nichts zu aktualisieren"
        assert said["bad"] == "0.1.188 ist da, aber seine Signatur ist UNGÜLTIG: nicht angeboten"
        assert said["hostAhead"] == "0.1.193, neuer als das neueste Release 0.1.188"
