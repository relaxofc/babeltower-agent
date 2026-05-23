# Changelog

All notable changes to `babeltower-agent` are recorded here.
This project follows [Semantic Versioning](https://semver.org/).

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
