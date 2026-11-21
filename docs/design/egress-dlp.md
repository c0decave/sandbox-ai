# Design: secret-leak detection in outgoing requests (egress DLP)

Status: built in 0.1.99 as the **secret guard** (`runtime.SecretGuard`, `sandbox/secret_guard.py`, README: Secret
guard). What was built differs from this design in these points:

- Two decisions instead of one: `"secrets": {"on_send", "on_shape"}` - a known value (`on_send`) and a token shape
  (`on_shape`). Defaults `report`/`report`; `high`: `block`/`ask`; `paranoid`: `block`/`block`.
- Without a hold (`report`) a body is searched **after** it went out, while the server answers (no added latency);
  with `ask` or `block` the first `RULE_BODY_LIMIT` bytes are read first. Past them: `block` cuts the stream where
  the secret comes, `ask` is recorded and not held (as designed).
- The matcher is not the canaries' one expression: that takes 20 ms per MiB with five values (the design's "below
  1 ms" was wrong). Values are told by four bytes at every 16th place of the data (one set lookup pass), shapes by
  their rarest anchor character, base64 runs by a translation table: 3 to 10 ms per MiB, flat in the number of values.
- Shapes are also found base64-encoded (GitHub's contents API carries files so), and a token in the login of its own
  service (a header or URL to its host) is no finding.
- The allow-list: `--allow-send WHAT@HOST` and `sandbox-ai secret-guard allow` (the design's open question about
  `--secret-name` was not needed: `--env` values with a token's shape are registered too).
- Safeguard kinds `secret-send` and `secret-context`; a context event is said once per secret, the request records
  mark the repeats.
- A Git login given into a running sandbox is registered during the run (control op `secret-values`).
- `--ask allow` leaves the guard's questions to the human.

Not built: a policy that holds model requests (`on_context`), custom shapes from `--privacy-rules`, naming the file
that brought a secret into the context.

## Problem

A secret the run hands over in plain text, or one the agent reads from the workspace (a `.env`, a key file), can
leave unseen in any request to a host the run allows: an upload, a gist, an issue, a paste site, an MCP tool argument.
Today only two things catch it: the push privacy check (Git pushes only) and canary tokens (decoys planted for the
run, `runtime.CanaryMatcher`). Knostic's "From .env to Leakage" (a Cursor agent uploading a file with an API key) and
"When an MCP tool asks for your GitHub token" are the cases. `builtin:mcp-no-credentials` asks about token
shapes in MCP arguments; requests through the proxy are not covered.

## Goals and non-goals

- Notice **known secret values** and **secret shapes** in every request the proxy opens, to every host, before the
  request goes out; report by default, ask or block where the human chose so (or the security mode does).
- Notice the same in requests to the run's **own provider** as a `context` event ("a secret entered the model's
  context"), never blocked by default: the model sees what the agent read; silent redaction is not copied (it breaks
  legitimate debugging and hides what happened).
- Non-goals: no vendor service, no upload of hashes, no change of what the agent sees, nothing for traffic the proxy
  does not open (`--no-inspect` hosts, TCP allowed with `--allow-tcp`): those are named as not covered.

## Design

### 1. What is matched

| Source | How it reaches the proxy |
|---|---|
| `--secret-env NAME`, `--env NAME=VALUE` whose name sounds secret (the activity log's rule), MCP `env_secrets`, `--git-token-env`/`-file` without `--token-proxy` | the launcher already knows the values (`cfg["env"]`, `secret_names`): it writes their **forms** (plain, URL-encoded, JSON-escaped, hex, base64 at the three offsets: `canary.forms`) into `proxy.json` under `"secrets"`, mode 0600 in the proxy's private directory, never the sandbox's |
| values the agent read from the workspace | not known in advance: the shapes below catch them |
| shapes | the privacy check's built-in token shapes (`privacy.BUILTIN`, the same list `builtin:mcp-no-credentials` carries) |

Values shorter than 8 characters are not registered (as canaries: ordinary traffic carries short pieces).

### 2. Where it is matched

The proxy already reads request bodies up to `RULE_BODY_LIMIT` (1 MiB) for body conditions and canary detection
(`CanaryMatcher.find` on URL, headers and body, gzip and deflate undone). The secret matcher is a second
`CanaryMatcher` built from `proxy.json` `"secrets"`, run in the same place, on the same bytes, plus a shapes regex. A
body over 1 MiB is matched in a sliding window over its stream (the matcher's `longest` keeps the overlap), so a
value across two chunks is found; the request is not held for that, the record follows.

### 3. What happens

A new ruleset key beside `"canary"`, `"secrets": {"on_send": "report" | "ask" | "block"}` (the strictest of the
rulesets wins, as for canaries). Defaults: `report` in low and medium, `ask` in high, `block` in paranoid. Per
request:

- **another host**: `report` - a safeguard record of the new kind `secret-send` ("the value of --secret-env
  API_TOKEN went to paste.example in a POST body, base64") and an `http` record field `secret`; `ask` - the request is
  held for a question of kind `http` with the reason (the value is never shown, only its name and encoding); `block` -
  403 with the reason.
- **the value's own host**: a value given for a host (`--api-key HOST=...`, a Git login's host) going to that host is
  expected and not matched.
- **the run's provider**: a `context` event in the activity timeline and the safeguards ("a value of
  .env-shaped secrets entered the model's context at 10:02"), never held.

### 4. Records and privacy

No record ever holds a value: names (`--secret-env API_TOKEN`), shape ids (`github-token`) and encodings only; the
proxy's logs are redacted as before (`redact_text`). `sandbox-ai safeguards --kind secret-send` and the app's
Safeguards list show them; `sandbox-ai report` counts them.

### 5. Performance

One regex alternation over at most 1 MiB per request (the canary matcher's cost today: measured below 1 ms for a
1 MiB body with 50 forms). Bodies are already buffered for rules; no new buffering. Shapes are matched only on
request bytes the proxy has (no streaming body is held back).

### 6. Tests (TDD, when built)

- A proxy test with a fixture upstream: a POST whose body holds a `--secret-env` value base64-encoded gives a
  `secret-send` record and is refused with `"on_send": "block"`; the same value to its own host passes.
- The model request carrying a fixture shape gives a `context` event, and passes.
- A value split across two chunks of a 3 MiB body is found.
- No record, log line or question holds the value (grep the run's whole log directory).

## Open questions

- Should `ask` hold a request whose body is larger than a question can show (`too_large_refusal` today refuses)? Yes:
  the question names the value and encoding, not the body.
- `--env` values that do not sound secret are not registered; a `--secret-name` option could add more.
