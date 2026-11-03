# Releasing sandbox-ai

The version lives in one place: `__version__` in `sandbox/agent_sandbox.py`.
Raise it with `scripts/bump_version.py` for every change you install or ship:
the patch counts up to 250, then the minor goes up (`0.1.250` → `0.2.0`).
`--minor`, `--major`, `--set X.Y.Z` and `--show` cover the rest.

A release consists of

| File | Used by |
|---|---|
| `sandbox-ai-bundle-X.Y.Z.tar.gz` | `install.sh` (the whole tree, reproducible) |
| `sandbox_ai-X.Y.Z-py3-none-any.whl`, `sandbox_ai-X.Y.Z.tar.gz` | PyPI (`pip install sandbox-ai`) |
| `install.sh` | first installs (`curl … | bash`) |
| `SHA256SUMS` | `install.sh` verifies the bundle with it |

## Steps

```bash
# 1. Bump the version, commit, tag
v=$(python3 scripts/bump_version.py)
python3 -m pytest -q tests
git commit -am "Release $v"
git tag "v$v"
git push origin main "v$v"

# 2. Build dist/ (needs: python3 -m pip install build)
python3 scripts/make_dist.py

# 3. Publish the release with its files (a token that may write releases);
#    SANDBOX_AI_SOURCE names your own Gitea/Forgejo repository, if you publish there
export SANDBOX_AI_TOKEN=...
python3 scripts/publish_release.py --source "${SANDBOX_AI_SOURCE:-https://github.com/c0decave/sandbox-ai}" dist/*
```

`install.sh` and `sandbox-ai-update` install from `https://github.com/c0decave/sandbox-ai` unless
`--source URL` or `SANDBOX_AI_SOURCE` names another repository; keep
`SANDBOX_AI_SOURCE` set in your shell when your own installation updates from
your own server.

`make_dist.py` refuses uncommitted changes (`--allow-dirty` for a local test)
and builds the bundle from the files git tracks, with fixed owner and
timestamps (`SOURCE_DATE_EPOCH`, default: the last commit), so two builds of
one commit are identical. `publish_release.py` creates the release for the
tag if it does not exist and replaces files of the same name.

Users then update with `sandbox-ai-update`; new installs use
`./install.sh` (or `install.sh --source URL`).

## GitHub and PyPI

The repository runs `.github/workflows/`:

- `ci.yml`: tests on Python 3.9 to 3.13, builds the package and runs every command from the wheel.
- `release.yml`, for a pushed tag `vX.Y.Z`: tests, checks that the tag matches
  `__version__`, builds `dist/`, creates the GitHub release with all files and
  uploads wheel and sdist to PyPI by trusted publishing (register the
  repository, the workflow `release.yml` and the environment `pypi` on PyPI
  first). On Gitea/Forgejo Actions the same workflow publishes with
  `publish_release.py` and the secret `RELEASE_TOKEN`; PyPI is skipped there.

`DEFAULT_SOURCE` in `install.sh` and `[project.urls]` in `pyproject.toml` point
to `https://github.com/c0decave/sandbox-ai`.

Before you push or tag a release, run the privacy check on what would be
published: `sandbox-ai privacy-check` (with `--rules-dir` for your
own rules), see the README section "Privacy check
before publishing".
