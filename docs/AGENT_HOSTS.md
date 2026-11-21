# Hosts the agent needs for work on sandbox-ai

What this sandbox reached on 2026-11-07 (its `/etc/hosts`), and what a release needs on
top. Everything is TCP/443 (HTTPS), through the connection proxy.

## Always: the agent itself (Claude, given by default)

| Host | For |
|---|---|
| `api.anthropic.com` | the model API |
| `console.anthropic.com`, `platform.claude.com` | login and token refresh |
| `claude.ai`, `claude.com` | login, account |
| `mcp-proxy.anthropic.com` | the claude.ai connectors (Gmail, Calendar, Drive, Docs) |
| `downloads.claude.ai` | Claude Code updates and plugins |

## Development: tests, dependencies, the repository

| Host | For | Option |
|---|---|---|
| `pypi.org`, `files.pythonhosted.org` | `pip`/`uv` installs: pytest and the CI requirements (`packaging/requirements-ci.txt`) | `--allow-pypi` |
| `github.com`, `api.github.com`, `codeload.github.com` | clone, fetch, push, `gh` | `--allow-github` |
| `raw.githubusercontent.com` | MCP catalogs (`axross-mcp-hub`, `sandbox-ai-mcp`) | `--allow-github` |
| `objects.githubusercontent.com`, `release-assets.githubusercontent.com` | release downloads (install.sh, AppImage) | `--allow-github` |

## A release (only when one is published)

| Host | For | Option |
|---|---|---|
| `uploads.github.com` | release assets (`scripts/publish_release.py`) | `--allow-host uploads.github.com` |
| `upload.pypi.org` | `twine`/`uv publish` | `--publish pypi` (the token stays with the proxy) |
| `test.pypi.org`, `test-files.pythonhosted.org` | a trial upload first, if wanted | `--publish testpypi` |

## As one start command

```bash
sandbox-ai claude --workspace ~/src/sandbox-ai \
    --allow-pypi --allow-github \
    --allow-host uploads.github.com --publish pypi
```

During a run, the same without a restart:

```bash
sandbox-ai net allow $SANDBOX_AI_RUN --allow-pypi --allow-github --allow-host uploads.github.com
```

Since 0.1.77, `--allow-host '*.DOMAIN'` covers every name below a domain (each opened as it is looked
up). Not for shared hosting: `*.githubusercontent.com`, `*.github.io`, `*.amazonaws.com` and public
suffixes are refused (anyone has names there); `--allow-github` names the GitHub hosts one by one.

## Not needed

No DNS to outside resolvers (`--dns`), no SSH (pushes go over HTTPS), no
`--egress-all-allow`.
