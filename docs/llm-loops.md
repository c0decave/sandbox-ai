# Local models in loops: what sandbox-ai does about it

Local models sometimes run in circles: they call the same tool with the same arguments again and again, alternate
between two calls, retry a failing command, or repeat one paragraph (or one thought) until the answer is cut off.
This page says why, what the agents themselves do about it, and what sandbox-ai's **loop guard** does for starts with
`--llm` (and only for them: traffic to any other server, the cloud providers' included, is never changed).

The second half is the research report the user supplied on 2026-11-12 (condensed, in English), with what we checked
against the OpenCode in our images.

## Why local models loop

- **Greedy or near-greedy sampling.** Temperature 0 (or very low) picks the most likely token every time; once a
  sentence repeats, repeating it again is the most likely continuation. Qwen3's model card warns against greedy
  decoding for exactly this reason.
- **No repetition penalty.** llama.cpp (`repeat_penalty`, DRY with `dry_multiplier`), LM Studio (`repeat_penalty`),
  vLLM (`repetition_penalty`) and Ollama (only in a Modelfile) have one; most defaults are 1.0 (off) or mild.
- **A context window smaller than the conversation.** The agent is told one window (OpenCode `limit.context`, Codex
  `model_context_window`) while the server loaded the model with a smaller one (LM Studio's JIT default load, Ollama's
  `OLLAMA_CONTEXT_LENGTH` / `num_ctx`). The server then cuts or shifts the conversation: the model forgets what it
  just did and does it again.
- **A wrong chat template / tool-call parser.** Tool calls that are announced but not parsed look like text to the
  agent; the model tries again.
- **No limit on the answer's length.** llama.cpp's `n_predict` is -1 (infinite) by default: a looping answer only ends
  when the context is full.

## What the agents do themselves (checked in their sources)

- **OpenCode 1.x** (our images install `opencode-ai@latest`, 1.19.5 on 2026-11-12): a `doom_loop` check in
  `packages/opencode/src/session/processor.ts` (`DOOM_LOOP_THRESHOLD = 3`): when the last three parts of the current
  assistant message are calls of the same tool with the same (JSON) input, it asks the permission `doom_loop`
  ("Continue after repeated failures"). The default rule is `doom_loop: "ask"` (`agent/agent.ts`); the config key is
  `permission.doom_loop` (`ask`, `allow` or `deny`, an action only). Rules are evaluated last match wins, so
  `{"*": "allow"}` - what sandbox-ai's `--dangerously-skip-permissions` gives OpenCode - also allows `doom_loop`.
  Known gap (issue #51965, closed as not planned): every step is its own assistant message, so a model that makes one
  call per step is never caught; only three identical calls inside one answer are.
- **OpenCode 2** (`@opencode/cli`, 2.2.x, a separate npm package our images do not install): the docs say `doom_loop`
  is no V2 permission action any more, and the maintainers closed #51965 because "the doom_loop check was removed in
  V2". Its config keys are different (`agents`, `permissions` as a rule list, `providers`).
- **Codex and Claude Code** have no such check for repeated tool calls that we know of.
- **None of them** watches the text of an answer for repetition.

## sandbox-ai's loop guard

Each layer is on or off (and tuned) on its own. sandbox-ai's defaults, yours in `~/.config/sandbox-ai/defaults.json`
(`sandbox-ai defaults set loop_guard '{"warn": 4, "steps": 60}'`), a server's (`sandbox-ai llm set NAME --loop-guard
warn=4`), and a start's (`--llm-param loop_guard.warn=4`, `--llm-param loop_guard=off`) go over each other in this
order. `sandbox-ai llm show NAME` shows what applies.

| Layer | Default | What it does |
|---|---|---|
| `steps` | 40 | OpenCode: `agent.build.steps` 40, `plan`/`explore` 18, `general` 26; at the last step OpenCode asks for a text answer (a new message of yours starts the count again). 0: OpenCode's own (no limit). |
| `compaction` | on | OpenCode: `compaction.auto` and `prune` on, `reserved` a fifth of the window; `tool_output` cut at 1000 lines / 50 000 bytes (OpenCode's own: 2000 / 51 200). |
| `instructions` | on | OpenCode: `~/.config/opencode/sandbox-ai-loop-guard.md` in its `instructions` (OpenCode concatenates the instructions of all its configuration files: your `AGENTS.md` and instructions stay). |
| `sampling` | on | The model family's recommended sampling as the lowest default (Qwen3 thinking T 0.6 / top_p 0.95 / top_k 20 / min_p 0; Qwen3 Instruct T 0.7 / top_p 0.8; Qwen3-Coder T 0.7 / top_p 0.8 / top_k 20 / repetition 1.05; QwQ as Qwen3; DeepSeek-R1 T 0.6 / top_p 0.95; gpt-oss T 1.0 / top_p 1.0). For the Qwen and DeepSeek families a temperature below 0.3 is raised to theirs (never greedy). Your own parameters win. |
| `context` | on | A warning at the start when the agent would be told a larger window than the server has the model loaded with (LM Studio's loaded instance, Ollama's `/api/ps`) or reports. |
| `warn` | 3 | The proxy, on each request: the same tool call with the same result (volatile parts - times, durations, ids, PIDs, temporary paths - taken out) this many times since your last message: a note `[sandbox-ai loop guard] ...` is added to that result. The model reads it and goes on. |
| `block` | 6 | ... this many times: the note, and tool use is paused for that one answer (`tool_choice: none`; Ollama, which takes no `tool_choice`, gets no tools for it). The model answers in text (a summary, a different plan); your next message goes on as usual. |
| `text` | 10 | The proxy, on a streamed answer: one block of 8 to 4096 characters repeated this many times in a row, over at least 4 KiB: the answer ends cleanly in its API's own events (`finish_reason: stop`, `end_turn`, `response.completed`) with a note; the server's connection is closed (it stops generating); the next request gets stronger anti-repetition sampling once (presence/frequency penalty, repeat/repetition penalty, DRY where the server takes them). |
| `watchdog_requests`, `watchdog_minutes` | 0 (off) | For unattended runs: after this many model requests (or minutes) in one turn, tool use is paused with a note. |

What counts as a loop, and what does not (tests in `tests/test_llm_loops.py`):

- A loop: the same call with the same result N times (identical), two calls taking turns (each pair repeats), the
  same failing command between other calls, a paragraph or a thought repeated in the answer.
- No loop: read, edit, test, read again with the changed file (every result is new); waiting for CI with
  `sleep 30; gh run view` (a call that waits - `sleep` of 5 s or more, `--watch`, `wait`, `until` - counts against
  three times the thresholds); a separator, an array of zeros, a table, similar lines, real code; a short phrase
  repeated ("the the the": `max_tokens` ends it).
- After your next message the counts start again.

Every intervention is a safeguards record of the kind `loop` ("loop guard intervened": what was found, what was done),
in `sandbox-ai safeguards` and the app.

With `--dangerously-skip-permissions` and the loop guard on, OpenCode's own `doom_loop` question is kept (`ask`),
which `{"*": "allow"}` would otherwise allow.

## Sampling parameters per model

`sandbox-ai llm set NAME [--model MODEL] --param KEY=VALUE --force-param KEY=VALUE --unset KEY --reset` keeps
parameters per server and model (every model without `--model`); `llm add ... --param` sets every model's;
`--llm-param KEY=VALUE` sets one for a start (forced). `sandbox-ai llm params [--kind KIND]` lists them with their
ranges and where each goes. The connection proxy puts them into the requests to that server: a value where the
request has none, a forced one in place of the agent's own, `max_tokens` as a ceiling (the agent's lower value stays).

| Key | Range | Ollama | LM Studio | llama.cpp | vLLM | other |
|---|---|---|---|---|---|---|
| temperature, top_p | 0-2, 0-1 | all APIs | all | all | all | all |
| top_k | 0-1000 | Anthropic API only (Modelfile) | chat, completions, Anthropic | all | chat, completions, Anthropic | Anthropic |
| min_p | 0-1 | Modelfile | - | all | chat, completions | - |
| repeat_penalty, repeat_last_n | 0.5-2, -1-131072 | Modelfile | repeat_penalty: chat, completions | all | - | - |
| repetition_penalty | 0.5-2 | - | - | - | chat, completions | - |
| presence_penalty, frequency_penalty, seed | -2-2, 0-2^31 | chat, completions | chat, completions | all | chat, completions | chat, completions |
| dry_multiplier, cache_prompt | 0-5, bool | - | - | all | - | - |
| max_tokens | 16-2^20 | all (Responses: `max_output_tokens`) | all | all | all | all |
| num_ctx | 512-2^24 | never sent: the agents' configuration; a start loads LM Studio models with it | | | | |

Sources: Ollama's `docs/api/openai-compatibility` and `anthropic-compatibility` (the fields each takes; "The OpenAI
API does not have a way of setting the context size": a Modelfile with `PARAMETER num_ctx`), LM Studio's
OpenAI-compatible chat completions (top_k, repeat_penalty among the payload fields), llama.cpp's `tools/server`
README (its `/completion` options also on `/v1/chat/completions`; on `/v1/messages` and `/v1/responses` those the API
has no field for inside `extra_params`, which is where the proxy puts them),
vLLM's extra sampling parameters. For Ollama, `llm set` and a start print the Modelfile for what its APIs do not
take.

## Two sandboxes, two models, one server

sandbox-ai's own reads (`llm status`, `llm models`, `llm check`, the context window) only list models or send an
empty request that names none: none of them loads a model (a test checks it against an LM Studio-like mock). What
unloads models is the server:

- **LM Studio**: JIT loading (on by default) loads a model at its first request, and **Auto-Evict** (on by default,
  Developer > Server Settings) unloads the models loaded that way before it loads another. Models loaded explicitly
  (`lms load`, `POST /api/v1/models/load`) are not affected (LM Studio's docs, "Idle TTL and Auto-Evict"). So a start
  loads its model explicitly when no instance of it is loaded (with `num_ctx` as its `context_length`), and leaves a
  loaded one alone. Such a model stays loaded until you unload it in LM Studio (or `lms unload`).
- **Ollama** unloads a model idle for `OLLAMA_KEEP_ALIVE` (5 minutes) and when more than `OLLAMA_MAX_LOADED_MODELS`
  (3 per GPU) or more than its memory are asked for. Its API reports neither setting and its OpenAI-compatible API
  has no `keep_alive`: set them on the server (`llm check` says so).
- **llama.cpp** serves one model (router mode: up to `--models-max`); **vLLM** one model: nothing to unload.

`sandbox-ai llm set NAME --evict allow` leaves loading to the server (JIT); `never` is the default.

---

## The user's research report (2026-11-12, condensed)

*Supplied by the user as a German report; condensed into English. Its claims are the report's; what we checked
against our installation follows in "Checked".*

Core thesis: a multi-layer defence is needed - (1) bounded agent steps, (2) real tool-loop detection, (3) correct
backend/tool-call integration, (4) low-loss context management, (5) model-appropriate decoding, (6) verifiable
progress criteria. A prompt alone is no circuit breaker.

Failure classes:

- A identical tool loop (same call, same output repeatedly)
- B alternating loop (grep(A) → read(B) → grep(A) → read(B))
- C error-retry loop (same command, same exit code)
- D text/thinking loop (model repeats sentences/thoughts without tools) - decoding/model/reasoning
- E context loss (re-examines files already understood after a long session) - context/compaction
- F API/parser loop (tool call announced but not executed / malformed) - chat template/backend
- G goal/finish loop (all tests green, keeps exploring) - missing stop criteria
- H subagent loop (a background subagent searches endlessly)

A tool-call detector helps A-C and H; D/E/G need other measures.

OpenCode as the report describes it: v1 had a rudimentary `doom_loop` check (`"permission": {"doom_loop": "deny"}`
stopped after the 3rd identical call; gaps across model steps and cyclic sequences, issue #43800). v2: the runtime
does not trigger doom-loop detection although `doom_loop` is still known (PR #61328 open, #53032 closed). An external
plugin `opencode-doom-loop-detector` 0.1.5 (peer dependencies OpenCode >= 2.2.5) with four detectors (identical calls
within one message: ask after 3; a signature with identical results: warn 3, block 6; little new information;
equivalent error sequences: warn 4, block 6, volatile parts normalised), working through the `doom_loop` permission
prompt. v2 keys: `agents.<name>.steps`, `plugins`, `compaction.auto`, `compaction.keep.tokens`, `compaction.buffer`,
`tool_output.max_lines` / `max_bytes`, `permissions` as a rule list, `providers`; sampling via `agents.*.request` is
stored but not sent in v2. Suggested starting values (experimental): build steps 18, plan 8, explore 8, general 12;
compaction auto, keep.tokens 6000-10000, buffer 20 %; tool_output 1000 lines, 50000 bytes; external_directory ask.
OpenCode v2 reads AGENTS.md (global and project), not CLAUDE.md.

LM Studio: `GET /api/v1/models` gives per model `max_context_length`, `capabilities.trained_for_tool_use` and
`loaded_instances[].config.context_length`: compare the loaded context with what the agent assumes. `lms load KEY
--context-length 32768`. A minimal tool-call test against `/v1/chat/completions` with one declared tool should give
`finish_reason: "tool_calls"` and a structured `tool_calls` array.

Sampling: no universal temperature; Qwen3 (hybrid) thinking T 0.6, top_p 0.95, top_k 20, min_p 0; non-thinking
T 0.7, top_p 0.8, top_k 20, min_p 0; no greedy decoding. Context: three limits - the model's maximum, the window
actually loaded, the limit the agent is told (with room for output and compaction) - must agree.

The anti-loop `AGENTS.md` template of the report is the basis of `sandbox-ai-loop-guard.md` (see `LOOP_TEXT` in
`sandbox/llm.py`). Further points: fewer tools and MCP servers mean fewer wrong paths; separate low step limits for
subagents; commands with side effects (git commit/push, rm, install, migrate, deploy) need a question or idempotency;
legitimate polling must stay possible as a bounded exception (read → edit → test → read with a changed file must not
be blocked); an independent time/step watchdog for unattended runs; metrics (success rate, repetitions, no-progress
steps, retries, tool calls, tokens, time, false-positive blocks).

### Checked (2026-11-12)

- Our images install `opencode-ai@latest` (`Containerfile.opencode`, `OPENCODE_VERSION=latest`): 1.19.5 on npm on
  2026-11-12, an OpenCode **1.x**. OpenCode 2 is the separate package `@opencode/cli` (2.2.7), which the images do not
  install; the plugin's peer dependencies (`@opencode/plugin` ^2.2.5) are OpenCode 2's, so it does not apply to our
  OpenCode. The report's v2 keys (`agents`, `permissions`, `providers`, `compaction.keep.tokens`,
  `compaction.buffer`) are therefore not used; 1.19.5's own are: `agent.<name>.steps` (described as "Maximum number
  of agentic iterations before forcing text-only response"), `compaction.auto` / `prune` / `tail_turns` /
  `preserve_recent_tokens` / `reserved`, `tool_output.max_lines` (default 2000) / `max_bytes` (default 51200),
  `instructions` (concatenated across configuration files), `permission`.
- `doom_loop` in 1.19.5: present and working as described above (three identical calls in one assistant message,
  default `ask`); "deny" is a valid action. With sandbox-ai's `--dangerously-skip-permissions` it was allowed by
  `{"*": "allow"}`; now kept at `ask` while the loop guard is on.
- OpenCode 1.19.5 reads `~/.config/opencode/AGENTS.md`, the project's `AGENTS.md`, and also `CLAUDE.md` (project and
  `~/.claude/CLAUDE.md`) unless its Claude Code prompt is disabled (`session/instruction.ts`).
- Issue numbers #43800, #61328, #53032 and the 18/8/8/12 step values were not checked; the step defaults here are
  higher (40/18/18/26) because a step limit that is too low ends ordinary edit-test cycles.
- LM Studio's JIT, TTL and Auto-Evict behaviour: from its docs ("Idle TTL and Auto-Evict"); `POST
  /api/v1/models/load` with `context_length`: from its REST docs. Not checked against a real LM Studio here.
