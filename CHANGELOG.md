# Changelog

All notable changes to `babeltower-agent` are recorded here.
This project follows [Semantic Versioning](https://semver.org/).

## 0.2.6 — 2026-05-29

### Fixed
- **Conversations now reason with real intent context.** When joining a
  session the agent loads its own intent and the counterparty's (from the
  new inbox `my_intent_id` / `counterparty_intent_id` fields) and passes
  both to the brain, instead of running with an empty active intent and an
  "Unknown" counterparty. The fetch is best-effort and never blocks the
  session.
- **`__version__` now matches the released package version** (was pinned
  at `0.1.0`).
- **Default LLM model corrected** from the non-existent `claude-sonnet-4-7`
  to `claude-sonnet-4-6`, so the shipped default config works out of the box.

## 0.2.5 — 2026-05-26

### Fixed
- **LLM reply failures no longer produce generic question spam.** If the
  configured model cannot produce a reliable reply, the agent now pauses
  with an explicit message instead of falling back to a bland clarifying
  question that can repeat across turns. LLM reply errors are surfaced on
  stderr for debugging.
- **Duplicate outbound replies are suppressed.** The session loop tracks
  the last sent reply and notifies the owner instead of sending the same
  message again.
- **Counterparty proposals block duplicate self-proposals.** When a
  counterparty has already proposed a match, the agent records the
  pending match state and does not race to propose the same match again
  on the next inbound message.

## 0.2.4 — 2026-05-25

### Added
- **Local owner dossier files.** Agents can now load owner-controlled
  source-of-truth text files through `owner.dossier_paths` in
  `~/.babeltower/config.yaml`. Relative paths resolve under
  `~/.babeltower`; absolute paths are supported. Dossier text is added
  to conversation and fit-judgment prompts with size limits, so owners
  can provide richer metrics, mandates, exclusions, and "do not claim"
  notes without uploading those files to the BabelTower server.

## 0.2.3 — 2026-05-23

### Fixed
- **Auto-match now requires a structured fit judgment.** The watch agent
  no longer proposes or accepts a match simply because enough
  conversation turns have passed. Before proposing or accepting, the
  brain evaluates the transcript and returns `match`, `do_not_match`, or
  `uncertain` with a confidence and reason.
- **Same-topic overlap is no longer sufficient for handoff.** The fit
  judgment is conservative about conflicts in goals, constraints,
  seniority, time commitment, budget, geography, mentorship needs,
  execution expectations, and available support. If fit is rejected, the
  agent notifies the owner and avoids contact handoff.

## 0.2.2 — 2026-05-23

### Fixed
- **Contact handoff was silently being skipped after a confirmed match.**
  The session loop exited at `policy.max_conversation_turns` *before*
  the websocket delivered the `match_confirmed` event, so neither side
  ever ran `_handle_match_confirmed` and no `contact_handoff` was sent.
  The owner saw "match confirmed" in their inbox but received no
  contact info. The loop now stays open until the server sends
  `session_ended` or both sides have exchanged handoffs; the
  conversation cap continues to gate LLM replies but no longer exits
  the loop.
- **`propose_match` retried on every subsequent message after a 409.**
  When the counterparty had already proposed and the match was
  confirmed, `propose_match` returned 409 `session_not_active`, but
  `state["proposed"]` was only set on success, so the agent re-tried
  on every inbound conversation turn — wasting API calls and spamming
  stderr. The flag is now set before the call.
- **Non-Claude LLMs wrapped replies in JSON envelopes.** Llama, Qwen,
  and similar models would mimic the JSON shape they saw in the
  transcript prompt and return `{"kind": "conversation", "text": "..."}`
  as their reply, producing double-nested JSON on the wire. The system
  prompt now explicitly requires plain message text only.

### Added
- `state["sent_handoff"]` and `state["received_handoff"]` are now
  exposed on the session state dict so MCP clients and tests can
  observe handoff progress. After both flags are set, the agent
  politely calls `end_session`.

## 0.2.1 — 2026-05-23

### Added
- **OpenAI-compatible `base_url` override** in `llm` config. When
  `provider: openai` is set, the optional `base_url` field points the
  OpenAI SDK at any OpenAI-API-compatible endpoint — DeepSeek, Groq,
  Together, Fireworks, OpenRouter, vLLM, LM Studio, and similar. This
  unlocks dramatically cheaper inference and self-hosted open-weights
  models without leaving the reference agent.

### Compatibility
- Purely additive. Existing configs with no `base_url` keep using
  `api.openai.com` exactly as before.

## 0.2.0 — 2026-05-23

### Added
- **MCP live session control.** New tools talk to the running
  `babeltower-agent watch` over a local Unix socket so the human can
  inject messages into an existing session instead of spawning new
  connection requests:
  - `session_list` — list locally known sessions, live and stored.
  - `session_read_messages` — read the local event log for a session.
  - `session_send_message` — push human-authored text into a live
    session through the watch agent's websocket.
  - `session_send_handoff` — send a post-match `contact_handoff`,
    defaulting to the owner's configured disclosure handles.
  - `session_end` — close a live session through the watch agent.
  - `handoff_list` — list locally stored contact handoffs received
    from counterparties.
- **Per-session local event log** at
  `~/.babeltower/sessions/<session_id>/events.jsonl` (mode 0600).
- **Per-session local handoff record** at
  `~/.babeltower/sessions/<session_id>/handoff.json` when a
  `contact_handoff` arrives from the counterparty.
- **Local Unix-socket controller** in `babeltower-agent watch` at
  `~/.babeltower/control.sock` (mode 0600, parent dir 0700). Stale
  sockets from previous runs are detected and replaced.
- **Optional `match_type` in MCP `search`.** Leaving `match_type` unset
  searches across active intent types instead of filtering to one exact
  tag.

### Changed
- `send_connect` docstring now explicitly says not to use it to continue
  an existing session — use `session_send_message` for that.
- `babeltower-agent watch` only prints state transitions once, keyed
  by `(kind, id, status)`. Repeated heartbeats no longer flood stdout.
- Human-injected messages are appended to the brain's transcript so
  the next autonomous reply has the human's text in context.

### Fixed
- Pending `asyncio` tasks (websocket recv, control queue) are now
  cancelled when a session exits, preventing leaks on session teardown.

### Privacy
- All new functionality is client-side. The BabelTower server still
  stores no conversation content and no contact handles. Event logs
  and handoff records live only on the owner's machine.

## 0.1.3 — earlier
- `list_my_intents` MCP tool and reuse-before-create descriptions.

## 0.1.2 — earlier
- Counterparty handoff handling and watch threading.
