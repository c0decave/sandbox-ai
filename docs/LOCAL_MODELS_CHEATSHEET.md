# Local models x sandbox-ai harnesses: cheat sheet

LM Studio on Apple silicon, measured 2026-11-15, round 1 on 0.1.98 on 2026-11-16 (details: [LOCAL_MODELS.md](LOCAL_MODELS.md)).

## Matrix

`✓` works: the smoke test passes (start, own tool, MCP tool, edit) and, where harder tasks were run, at least one
was solved · `~` limited: no harder task solved within its time limit, or the smoke test not always passed · `✗` neither ·
empty: not run. The rows of Qwen3-Coder-Next, Nemotron-3-Super, Laguna-S and Qwen3.6 are round 1's (0.1.98, sandbox-ai's
own proxy and MCP relay, two MCP servers in the smoke test): `smoke k/n` and `debug k/n` = runs passed (seconds or
minutes, and model requests, of the passed runs), `0.1.97: k/n` = the same on main before round 1's fixes, `phase 2`
= the research before; the other rows: `smoke 4/4` = the points of one phase-2 run. `debug k/n` = runs of the
debugging task solved;
`MCP task`, `web task`, `10-turn task`: see [LOCAL_MODELS.md](LOCAL_MODELS.md). Configuration: "tuned" (section 3 and 4).

<!-- MATRIX:BEGIN -->
| model | Claude Code | Codex | OpenCode | Qwen Code | Goose | Hermes | DSH | Pi | OpenCode 2 |
|---|---|---|---|---|---|---|---|---|---|
| Qwen3-Coder-Next 80B | ✓ smoke 2/2 (35 s); debug 1/1 (16.3 min, 142 req); phase 2 (0.1.97): debug 0/1; MCP task 5/7 tests | ✓ smoke 2/2 (23-27 s); phase 2: debug 0/2; MCP task ok | ✓ smoke 2/2 (23-27 s); debug 0/1 (time limit, 168 req); phase 2: debug 1/2 (12 min, 102 req); MCP task ok; 10-turn task 8/10 | ✓ smoke 2/2 (29-32 s) | ✓ smoke 2/2 (20-21 s); debug 1/1 (17.5 min, 129 req) | ✓ smoke 2/2 (43-45 s) | ✓ smoke 2/2 (28-30 s) | ✓ smoke 2/2 (16-19 s) | ✓ smoke 2/2 (27-31 s) |
| Nemotron-3-Super 120B (GGUF) | ✓ smoke 1/1 (102 s); phase 2: debug 1/2 (10 min, 21 req) | ~ smoke 1/1 (91 s); debug 0/1 (time limit, 63 req) | ✓ smoke 1/1 (99 s); phase 2: debug 1/1 (15 min, 38 req) | ✓ smoke 1/1 (338 s) | ~ smoke 1/1 (90 s); debug 0/1 (time limit, 69 req) | ✓ smoke 1/1 (134 s) | ✓ smoke 1/1 (89 s) | ✓ smoke 1/1 (160 s) | ✓ smoke 1/1 (132 s) |
| Laguna-S-2.1 | ✓ smoke 2/2 (54-103 s); debug 1/1 (12.8 min, 36 req) (0.1.97: 0/1); phase 2: debug 1/1 (5 min); MCP task 7/7 tests; web task ok | ~ smoke 2/2 (44-135 s); debug 0/1 (time limit, 46 req) | ✓ smoke 1/1 (53 s) (0.1.97: 0/1); debug 1/1 (11.8 min, 62 req) (0.1.97: 1/1); phase 2: debug 1/1 (5 min, 23 req) | ✓ smoke 2/2 (60-86 s) (0.1.97: 1/2); debug 1/1 (18.2 min, 33 req) (0.1.97: 0/2) | ✓ smoke 2/2 (39-104 s); debug 1/1 (5.9 min, 52 req) (0.1.97: 1/1) | ✓ smoke 1/1 (95 s) (0.1.97: 0/1); debug 1/1 (12.0 min, 33 req) | ✓ smoke 2/2 (42-56 s); debug 1/1 (12.6 min, 36 req) | ✓ smoke 2/2 (32-65 s); debug 1/1 (12.1 min, 34 req) | ✗ smoke 0/1 (0.1.97: 0/1); debug 0/1 (time limit, 36 req) |
| gpt-oss-120b-heretic | ✓ smoke 4/4 88 s | ✓ smoke 4/4 48 s | ~ smoke 4/4; debug 0/1 |  |  |  |  |  |  |
| GLM-4.7-Flash | ✓ smoke 4/4 87 s | ✓ smoke 4/4 45 s | ~ smoke 4/4; debug 0/1 |  |  |  |  |  |  |
| Qwen3.6-35B-A3B | ~ smoke 2/2 (25-28 s); debug 0/1 (time limit, 99 req) (0.1.97: 0/1) | ✓ smoke 2/2 (18-20 s); debug 1/1 (11.9 min, 69 req) | ✓ smoke 2/2 (34-35 s); debug 1/1 (3.0 min, 23 req); phase 2: debug 1/1 (5 min, 30 req) | ✓ smoke 2/2 (24-25 s); debug 1/1 (4.2 min, 25 req) (0.1.97: 0/1) | ✓ smoke 2/2 (17-18 s); debug 1/1 (3.9 min, 37 req) | ~ smoke 2/2 (34-35 s); debug 0/1 (time limit, 76 req) | ✓ smoke 2/2 (34-45 s); debug 1/1 (9.4 min, 68 req) | ~ smoke 0/2 (0.1.97: 1/1); debug 1/1 (3.4 min, 27 req) | ✓ smoke 2/2 (31-42 s); debug 1/1 (2.6 min, 23 req) |
| Gemma-4-31B | ✓ smoke 4/4 78 s | ✓ smoke 4/4 72 s | ✓ smoke 4/4 79 s |  |  |  |  |  |  |
| Qwen3.8-Flash-Next (round 4) | ✓ smoke 1/1 (45 s); debug variant 2/2 (6.2-7.3 min, 20-22 req); MCP task 7/7 tests (12.7 min) | ✓ smoke 1/1 (70 s); debug variant 2/2 (6.6-10.4 min, 18-31 req) | ✓ smoke 1/1 (50 s); debug variant 2/2 (9.9-10.6 min, 23 req); MCP task 7/7 tests (13.8 min); 10-turn task 10/10 (15.6 min) | ✓ smoke 1/1 (66 s); debug variant 2/2 (7.7-9.6 min, 17-23 req) | ~ debug variant 1/1 (7.1 min, 17 req) | ~ debug variant 1/1 (11.1 min, 23 req) | ~ debug variant 1/1 (7.6 min, 20 req) | ~ debug variant 1/1 (5.2 min, 15 req) |  |
| Ornith-1.5-122B (round 4) | ✓ smoke 1/1 (53 s); debug variant 2/2 (10.1-10.4 min, 24-28 req); MCP task 7/7 tests (10.4 min) | ~ smoke 2/2 with 0.1.99's resource-tool fix (0/1 before); debug variant 0/3 (time limit) | ✓ smoke 1/1 (79 s); debug variant 1/2 (14.7 min, 16 req); MCP task 7/7 tests (12 min); 10-turn task 10/10 (20 min) | ✓ smoke 1/1 (148 s); debug variant 2/2 (11.1-15 min, 22-40 req) | ~ debug variant 1/1 (10.1 min, 20 req) | ~ debug variant 1/1 (12.9 min, 21 req) | ~ debug variant 1/1 (15.0 min, 40 req) | ~ debug variant 1/1 (8.4 min, 29 req) |  |
| Ornith-1.5-35B-A3B (round 4) | ✓ smoke 1/1 (24 s); debug variant 2/2 (6.8-8.3 min, 30-47 req) | ✓ smoke 1/1 (22 s); debug variant 1/2 (13.6 min, 35 req) | ~ smoke 0/1 (its own read for the MCP point); debug variant 2/2 (7.7-10.0 min, 30 req); MCP task 7/7 tests (own bash for the tests); 10-turn task 9/10 | ✓ smoke 1/1 (31 s); debug variant 1/2 (6.4 min, 19 req; once solved with the tests changed) | ~ debug variant 1/1 (6.9 min, 34 req) | ~ debug variant 1/1 (13.4 min, 33 req) | ~ debug variant 1/1 (8.1 min, 37 req) | ~ debug variant 1/1 (9.0 min, 31 req) |  |
| KAT-Coder-V2.5-Dev (round 4) | ✓ smoke 1/1 (24 s) with 0.1.99's system-message fix (HTTP 500 before); debug variant 1/2 (12.0 min, 33 req) | ✓ smoke 1/1 (29 s) with the fix (HTTP 500 before); debug variant 1/2 (5.3 min, 19 req) | ✓ smoke 1/1 (20 s); debug variant 2/2 (7.0-8.0 min, 14-22 req) | ~ smoke 0/1 (its own read); debug variant 1/2 (3.4 min, 10 req) |  |  |  |  |  |
| Recursive Flint (round 4) | ✓ smoke 1/1 (48 s); debug variant 1/2 (9.5 min, 28 req; one 32k-token runaway) | ✓ smoke 1/1 (59 s); debug variant 2/2 (6.0-8.8 min, 30-40 req) | ~ smoke 0/1 (its own read); debug variant 1/2 (11.2 min, 22 req) | ~ smoke 1/1 (34 s); debug variant 0/2 (time limit) |  |  |  |  |  |
| Muse Glimmer 30B (round 4) | ✗ debug variant 0/4 (15-25 min, no edit) | ✗ debug variant 0/2 (no edit) | ~ smoke 1/1 (233 s); debug variant 0/2 (15-25 min, no edit) | ✗ debug variant 0/2 (no edit) |  |  |  |  |  |
| Nemotron 3.5 Super VL (round 4) | ~ smoke 1/1 at T 0.6 (72 s); debug variant 0/2 | ~ smoke 1/1 at T 0.6 (86 s); debug variant 0/2 | ~ smoke 1/1 at T 0.6 (62 s; 0/2 at T 1.0); debug variant 0/3 | ~ smoke 1/1 at T 0.6 (98 s); debug variant 0/2 |  |  |  |  |  |
<!-- MATRIX:END -->

## Which model for which task (rounds 1-5)

Long unattended work and hard debugging: **Qwen3.8-Flash-Next** (GGUF; 12 of 12 debugging runs in all eight harnesses)
or Ornith-1.5-122B; a fast helper and little memory: **Ornith-1.5-35B-A3B** (37 GB) or Qwen3.6-35B-A3B (20 GB); Codex:
Flash-Next or Recursive Flint. Round 5 per use case: **untrusted input** (web pages, tickets, other tools' results)
only with Ornith-35B/122B, Flash-Next or Qwen3-Coder-Next - never Gemma 4, Flint, KAT, Nex-N2.5, Laguna-S, Qwen3.6 or Muse (an MCP ticket's
planted comment made them run its command and send `.env`); **building and migrating code** fastest with KAT-Coder in
OpenCode or Ornith-35B in Claude Code; **ops** Ornith-122B or Flash-Next; **vision** Ornith-122B; **team member /
helper** Ornith-35B or -122B; **small jobs / strict JSON** KAT or Qwen3-Coder-Next straight to the API; **long
sessions with compaction** in OpenCode (Claude Code at 64k: "Autocompact is thrashing"; the PROGRESS.md line
`loop_guard.progress_notes`, on in the Ornith-35B and Flash-Next/Claude Code profiles, helps). The full tables:
[LOCAL_MODELS.md](LOCAL_MODELS.md#which-model-for-which-task), per use case `sandbox-ai llm recommend`.

## 0. sandbox-ai 0.1.98 does sections 2-4 itself

A start with `--llm studio:MODEL` applies the **model × harness profile** of the model in that agent (sampling,
context window, turn limit 150, the agent's own switches such as `CLAUDE_CODE_DISABLE_THINKING=1` for Nemotron in
Claude Code) under your own values, and says it in one line; a pair known not to work is a WARNING, never a refusal.
`sandbox-ai llm profiles [MODEL] [--agent AGENT]` lists them, `--dry-run` shows `llm.profile`, `--no-llm-profile`
switches it off. The commands below are what it sets for you (`HOST`: the LM Studio server's address).

## 1. Load the model (LM Studio server)

Always: **one model, context 131072, parallel 1.**

```
sandbox-ai llm add studio --kind lmstudio --url http://HOST:1234
sandbox-ai llm load studio qwen/qwen3-coder-next --context 131072          # 0.1.96 sends parallel 1 itself
# 0.1.95, or by hand:
curl -s http://HOST:1234/api/v1/models/load \
  -d '{"model": "qwen/qwen3-coder-next", "context_length": 131072, "parallel": 1}'
```

## 2. Per model (once per endpoint)

| model key | use | settings to store |
|---|---|---|
| `qwen/qwen3-coder-next` | fast interactive coding (many small steps); round 1: debugging task in Claude Code 16 min, Goose 18 min (130-140 requests) | nothing: sandbox-ai's Qwen3-Coder preset (T 0.7, top_p 0.8, top_k 20, repeat 1.05) |
| `nvidia/nemotron-3-super` (GGUF) | hard and long tasks, steady (15 min, 38 requests); slow writer: not finished in 20 min in Codex or Goose (round 1) | `sandbox-ai llm set studio --model nvidia/nemotron-3-super --param temperature=1.0 --param top_p=0.95` |
| `poolside/laguna-s-2.1` | hard and long tasks: debugging task solved in 7 of 9 harnesses (6-18 min; not Codex, OpenCode 2); at most 2 MCP servers | nothing (server defaults) |
| `gpt-oss-120b-heretic-v2-hi-mlx` | second choice | nothing: gpt-oss preset (T 1.0, top_p 1.0) |
| `zai-org/glm-4.7-flash` | second choice | `sandbox-ai llm set studio --model zai-org/glm-4.7-flash --param temperature=0.7 --param top_p=1.0 --param max_tokens=16384` |
| `qwen3.6-35b-a3b-mlx` | small and strong: debugging task solved in 7 of 9 harnesses in 2.6-12 min (round 1; not Claude Code, Hermes); helper, vision | nothing: Qwen3 preset (T 0.6, top_p 0.95, top_k 20) |
| `google/gemma-4-31b-qat` | small, careful, vision | `sandbox-ai llm set studio --model google/gemma-4-31b-qat --param temperature=1.0 --param top_p=0.95 --param top_k=64` |

Not for agents: `vcoder-120b`, the Nemotron **MLX** build with Claude Code, `qwen3.8-27b` above ~50k tokens.

## 3. Start (every harness)

```
sandbox-ai AGENT --llm studio:MODEL --llm-param loop_guard.steps=150 [--mcp files --mcp exec ...]
```

- `loop_guard.steps=150`: the default 40 ends real work early (OpenCode, OpenCode 2, Qwen Code, Goose, Hermes, Pi).
- Unattended: add `--llm-param loop_guard.watchdog_minutes=90`.
- On sandbox-ai without the fixed loop-guard note (0.1.95, 0.1.96 before fix G5): add `--llm-param loop_guard.warn=0`,
  or every step after the third repeated command costs a full prefill.
- Slow model (Nemotron, GLM, Gemma): 0.1.96 `--llm-param stream_idle_timeout=3600`; tell less context with
  `--llm-param num_ctx=65536`.

## 4. Per harness

| harness | start | extra for local models | do not expect |
|---|---|---|---|
| OpenCode | `sandbox-ai opencode --llm studio:MODEL --llm-param loop_guard.steps=150` | slow models: 0.1.96 `--llm-param stream_idle_timeout=3600` (sets the provider's `headerTimeout`/`chunkTimeout`; both are 300 s in 0.1.95) | a hard step limit (the proxy guard is the stop) |
| Claude Code | `sandbox-ai claude --llm studio:MODEL` | 0.1.95: `ENABLE_TOOL_SEARCH=false CLAUDE_CODE_MAX_CONTEXT_TOKENS=131072 CLAUDE_CODE_AUTO_COMPACT_WINDOW=131072 sandbox-ai claude --env ENABLE_TOOL_SEARCH --env CLAUDE_CODE_MAX_CONTEXT_TOKENS --env CLAUDE_CODE_AUTO_COMPACT_WINDOW --llm studio:MODEL` | models whose Messages API fails with tools (Nemotron MLX); a prefill over 6 minutes. **Nemotron: add `CLAUDE_CODE_DISABLE_THINKING=1` (`--env CLAUDE_CODE_DISABLE_THINKING`)**: debugging task solved in 10 min / 21 requests instead of unsolved after 25 min |
| Codex | `sandbox-ai codex --llm studio:MODEL` | needs the proxy fixes (namespace tools, `justification`, `Chunk ID`); slow models: `-- -c model_providers.llm-studio.stream_idle_timeout_ms=3600000` | MCP tools or a working shell tool on 0.1.95 with LM Studio |
| Qwen Code | `sandbox-ai qwen --llm studio:MODEL --llm-param loop_guard.steps=150` | 0.1.96 turns the memory extraction off and raises the timeouts; 0.1.95: `QWEN_STREAM_IDLE_TIMEOUT_MS=3600000 sandbox-ai qwen --env QWEN_STREAM_IDLE_TIMEOUT_MS ...` | prefills over 120 s on 0.1.95 |
| Goose | `sandbox-ai goose --llm studio:MODEL --llm-param loop_guard.steps=150` | slow models: `OPENAI_TIMEOUT=3600 sandbox-ai goose --env OPENAI_TIMEOUT ...` | bounded tool results (it passes all of them on) |
| Hermes | `sandbox-ai hermes --llm studio:MODEL --llm-param loop_guard.steps=150` | window of 64,000 or more | an answer after its own guardrail stops a loop |
| DSH | `sandbox-ai dsh --llm studio:MODEL -- headless "task"` | - | compaction before the window (it compacts after the server refuses) |
| Pi (0.1.96) | `sandbox-ai pi --llm studio:MODEL --llm-param loop_guard.steps=150` | slow models: `~/.pi/agent/settings.json` `{"httpIdleTimeoutMs": 0}` | an answer when the turn limit ends the run |
| OpenCode 2 (0.1.96) | `sandbox-ai opencode2 --llm studio:MODEL --llm-param loop_guard.steps=150` | as OpenCode | MCP tools in the first request of `run` (2.2.5) |

## 5. Symptom -> cause -> fix

| symptom | cause | fix |
|---|---|---|
| every step as slow as the first | loop-guard note moves (after 3 identical calls) | fixed note places (G5); meanwhile `loop_guard.warn=0` |
| slow from the first step | parallel 4 | load with parallel 1 |
| hangs for minutes, then starts over | harness idle limit < prefill; retries start from zero | section 3/4 timeouts, smaller `num_ctx`, faster model |
| stops unsolved after 40 steps | default turn limit | `loop_guard.steps=150` |
| never stops | no hard limit in Claude Code, Codex, OpenCode 1, DSH; guard blind for Codex | 0.1.96 guard fixes; watchdog |
| Codex cannot see MCP tools / every command "fails" | `namespace` tools ignored by LM Studio; `justification` refused by Codex | proxy fixes G6, G7 |
| read returns a preview and a file path | `files` server part 60 KB > harness limit | server parts of 24 KB |
| `fetch_page`: "not reachable: " | no reason given; no IPv4 fallback | web-fetch fix W1-W3 |
| Claude Code: "Prompt is too long", or the same request again and again | window unknown to it | `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |
| Hermes: "Something went wrong" at the start | window below 64,000 | load with 131072 |
| "reads a resource" of an MCP server again and again; "the server isn't exposing any resources" | resource tools offered for servers that publish none | 0.1.98: MCP relay drops empty capabilities; the proxy's sentence in Codex, DSH, Goose |
| Claude Code compacts at ~50k of 131k | LM Studio counts cached tokens twice for it | 0.1.98: proxy sets the usage right (compaction at ~95k) |
| Claude Code repeats a request after 5 min of silence | no wait setting | 0.1.98: the proxy's keep-alive every 20 s |
| DSH: "stream idle timeout after 300000" | fixed 300 s limit | prefill under 5 min: faster model, smaller `num_ctx` |
| Qwen Code: "Loop detection halted the run (repetitive_thoughts)" | its detector on a thinking model's reasoning | 0.1.98: `model.skipLoopDetection: true` for `--llm` |
| Qwen Code: `tool_call` of `read_mcp_resource` with guessed URIs | its deferred resource tool, offered whatever a server advertises | 0.1.98: `tools.exclude` for `--llm` |

## 6. Numbers to remember

- Prompt before the first word (two MCP servers): Claude Code 24-28k, Qwen Code 22k, Hermes 21k, Codex 14k,
  OpenCode 14k, DSH 12k, Goose 10k, Pi 7k, OpenCode 2 6k tokens.
- First request = prefill of that prompt: 11-33 s on Qwen3-Coder-Next; later steps 0.3-1 s while the cache holds.
- Prefill speed (tokens/s): Qwen3.6-35B 1,000-1,800 · Qwen3-Coder-Next ~1,000 · Laguna ~800 · gpt-oss-120b 500-800 ·
  Nemotron GGUF ~520 · Gemma-4-31B ~430 · GLM-4.7-Flash 280 at 46k · Qwen3.8-27B ~200.
- Harness idle limits: Qwen Code 240 s (request 120 s) · Claude Code 300/360 s · OpenCode, Codex, DSH, Pi,
  OpenCode 2 300 s · Goose 600 s · Hermes none.
