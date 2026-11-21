# Design: a package-registry guard in the proxy

Status: **built** in 0.1.99 (the Knostic research's R12, item 8 of its follow-up); the user's documentation is
the README's section "Package guard". Where the build differs from this design, and why:

- The proxy's part lives in `sandbox/runtime.py` (`PackageGuard`, `package_request`, `package_times`, `PackageList`),
  not in `sandbox/packages.py`: only `runtime.py` is mounted into a sandbox. `sandbox/packages.py` is the host side
  (the copy's sync and checks, the allow-list, the command), and uses the runtime's parsing so both sides tell
  packages alike.
- Nothing in the proxy parsed registry answers before (the `--allow-pypi`/`--allow-npm` options only open hosts), so
  the reading of metadata is new, not reused.
- The copy is `osv-malicious.json` (signed) plus one sorted text list per ecosystem (`npm.list` ...: NAME, advisory,
  versions), not `ECOSYSTEM.json`: the proxy maps the file and searches it in place, with no load time and no memory
  of its own per sandbox (npm alone has a six-digit number of entries).
- npm clients ask for the abbreviated package document, which has no `time`: the proxy sends the same request on
  with `Accept: application/json` (the full document). Nothing more is requested, but the answer is larger.
- A known malicious version is "deny" as designed, and its question (the one way to allow that one download) is
  asked through the approvals; `--ask allow` never answers it, the broker caps every answer to "once". `"block"`
  (never asked) is there for a policy that wants no choice.
- The age and new-name questions can be allowed for the run in Approvals; "always" is the per-project allow-list
  (`sandbox-ai packages allow`), not an approval answer - requests are never remembered beyond the run.
- `package_min_age_days` is a key of `defaults.json` (the human's own base); rulesets, the policy and the strict
  modes can only lengthen it.
- The signed copy's source is the release source under `releases/download/osv-malicious`; publishing it needs the
  release key and is the maintainer's step (`sandbox-ai packages build ... --sign KEY`). `sandbox-ai packages sync
  --osv` builds an unsigned copy from OSV's own dumps meanwhile.
- Go: 404s of the module proxy are not recorded as unknown names (clients probe every prefix of an import path).
- An advisory with a bounded version range but no list of versions counts for every version (the lists compare no
  version order).

## Problem

Agents install packages: `npm install`, `pip install`, `cargo add`, `go get`. A malicious package (Knostic's
SaassyCode, the MCP packages that sweep the environment or decode Python from image pixels), a typosquat or a
hallucinated name an attacker registered ("slopsquatting") runs its install scripts inside the sandbox. The sandbox
contains it, but what it can reach (the workspace, an allowed host, a GitHub token in the proxy) is enough for harm.
sandbox-ai pins its own builds to aged releases; packages the agent installs are not checked at all.

## Goals and non-goals

- Before a package version is downloaded, check it locally: known malicious, too young, or a name that does not exist
  or is brand new. The human decides (ask), with the reason; deny only for a known malicious version.
- No package name leaves the machine for a lookup: the malicious list is an **offline copy** on the host, synced by
  `sandbox-ai update`; release dates come from the registry metadata the agent's own request fetches anyway.
- Non-goals: vulnerability (CVE) scanning, licence checks, private registries' semantics, anything the proxy does not
  open (`--no-inspect`).

## Design

### 1. Where

The proxy's HTTP path already parses requests to registry hosts for `--allow-pypi` and `--allow-npm` rules. A new
module `sandbox/packages.py` (standard library) gives the proxy, per registry:

| Registry | Metadata request (read for dates and existence) | Download request (checked) |
|---|---|---|
| npm (`registry.npmjs.org`) | `GET /NAME`, `GET /@SCOPE%2fNAME` (packument: `time` per version) | `GET /NAME/-/NAME-VERSION.tgz` |
| PyPI (`pypi.org`, `files.pythonhosted.org`) | `GET /simple/NAME/` (PEP 691 JSON: `upload-time`) or `/pypi/NAME/json` | `GET /packages/.../NAME-VERSION-*.whl|.tar.gz` |
| crates.io (`index.crates.io`, `static.crates.io`) | sparse index `GET /3/n/NAME` | `GET /crates/NAME/NAME-VERSION.crate` |
| Go (`proxy.golang.org`) | `GET /MODULE/@v/VERSION.info` (`Time`) | `GET /MODULE/@v/VERSION.zip` |

The proxy keeps the metadata answers it passes through (name -> {version: release time}) for the run, bounded, so
the download that follows is judged without a request of its own. A download whose metadata it has not seen is judged
by the malicious list alone (and named "release age unknown").

### 2. Checks

1. **Malicious**: the OSV `MAL-` entries for npm, PyPI, crates.io and Go, as an offline snapshot
   (`~/.local/share/sandbox-ai/osv-malicious/ECOSYSTEM.json`: name -> affected versions), synced by `sandbox-ai update`
   (signed like releases) and by `sandbox-ai packages sync`. A match is **denied** with the advisory id; the human can
   allow it once by an approval answer only (never always).
2. **Minimum release age**: a version younger than `package_min_age_days` (default **14**, the project's own rule for
   pinned releases) is **asked** about ("left npm 3 days ago"); `0` turns it off.
3. **New or missing names**: a registry 404 for a name, or a package whose first release is younger than 30 days, is
   recorded as "possibly a hallucinated or slopsquatted name" (safeguard kind `package`), and asked about in high and
   paranoid.

The decisions are a ruleset section of their own (`"packages": {"malicious": "deny", "min_age_days": 14,
"min_age": "ask", "new_names": "report"}`), so the policy and the modes can tighten them and an audit ruleset can try
them.

### 3. Records

`http` records gain `package: {"ecosystem", "name", "version", "age_days", "malicious": "MAL-..."}`;
`sandbox-ai safeguards --kind package`; `sandbox-ai report` counts them. Questions show the name, version, age and the
advisory (escaped).

### 4. Limits to state

- Git dependencies, tarball URLs and vendored packages are not registry downloads: not checked.
- A registry mirror the proxy does not know is not checked (`--allow-host` of a mirror: named in the plan).
- The snapshot is as current as its last sync; the age check covers the gap for new malware.

### 5. Tests (TDD, when built)

- A fixture registry upstream: `npm install` of a version listed in a fixture OSV snapshot is refused with the
  advisory; a version released 2 days ago (fixture packument) asks; a 404 name gives a `package` record.
- The snapshot sync verifies its signature and refuses an unsigned or older file.
- No request leaves for a lookup (the proxy's log shows only the agent's own requests).
