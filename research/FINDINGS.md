# Sibyl Memory — verified behaviour

Versions here: sibyl-memory-cli 0.3.22, sibyl-memory-client 0.6.1, mcp 0.1.13.
Source of truth: sibyl_memory_client/ in the venv (client.py, schema.sql).
Read directly from source, not inferred from docs.

> The published docs are BEHIND the package. Where docs and source disagree,
> the source on disk wins. Do not design against docs.sibyllabs.org.

## 1. Retrieval and tokenization

All FTS5 tables in schema.sql use: tokenize = 'porter unicode61'

unicode61 splits on ALL non-alphanumeric characters. `:` `/` `-` `_` `.` are
all separators. NO delimiter survives as an atomic token.

Design rules, not preferences:
- Exact lesson retrieval MUST use get_entity(category, name). Direct lookup
  against UNIQUE (tenant_id, category, name). No tokenizer involved.
- search_entities() is for adjacent/neighbour lessons ONLY. A signature
  shatters into component tokens, so "slippage revert" matches that failure
  class across every contract seen. This is the neighbour-retrieval path.
- camelCase does NOT split. swapExactTokensForTokens stays one token.
- Porter stemming applies on both write and query side. Assume stems.
- A v4 folded-trigram search_shadow table exists but is used ONLY as a
  zero-hit fallback inside search(). Never rely on it for primary retrieval.

## 2. Entity write semantics (set_entity)

On conflict it performs a genuine UPDATE, not INSERT OR REPLACE:
  UPDATE entities SET status=?, body=?, updated_at=... WHERE id=?

The row KEEPS its original id and its original created_at. So:
- created_at -> when the lesson was first learned (first_seen)
- updated_at -> when the lesson was last revised (last_revised)
Do not duplicate these in the body.

list_entities() orders by updated_at DESC -> "most recently revised lessons"
is a one-liner.

TRAP: the UPDATE sets `status = ?` UNCONDITIONALLY, and status defaults to
None. Revising without re-passing status silently NULLs it, breaking any
status-filtered retrieval with no error.
DECISION: all lesson state lives in the body JSON. NEVER use the status column.

TRAP: get_entity RAISES NotFoundError when absent — it does not return None.
A first-encounter signature is the NORMAL path here, not an error. Every
pre-execution retrieval must catch NotFoundError and treat it as "no lesson".

Validation: category and name must be non-empty, no control chars, <= 1024
chars. ValidationError on rejection.

## 3. Journal semantics (write_event / read_events)

Four payload columns, all nullable, all JSON-validated.
Post-mortem mapping (use the native shape, do not blob into extra):
- evaluated -> what the agent believed pre-flight (assumed slippage,
  allowance state, expected min-out)
- acted     -> the transaction actually submitted, plus tx hash
- forward   -> the correction, and the signature it was filed under
- extra     -> reserved, leave null

Ordering: ORDER BY ts DESC, id DESC. Millisecond ties break on UUID —
arbitrary but STABLE. Same read returns the same order every time.

ts is CALLER-SUPPLIED and UNVALIDATED (ts or _utc_now_iso()). read_events
since/until filter it as plain text comparison.
- This is the legitimate backfill mechanism: induced-failure post-mortems get
  the TRUE timestamp of the real transaction.
- Never fabricate a ts that has no real transaction behind it.
- A malformed ts breaks ordering and range filters SILENTLY. Write ONE
  timestamp helper and use it everywhere. No inline construction.

## 4. Free-tier cap

set_entity docstring says 5 MB. The docs page says 2 MB — stale. Code wins.
- Enforced by _cap_gate.check() pre-write plus _verify_committed_size()
  inside the same transaction. Raises CapExceededError.
- free_tier_status() returns the authoritative number. Call it, do not
  hardcode either figure.
- Lessons are small text, no risk. Do NOT journal raw market data, full
  receipts, or per-tick anything.

## 5. CLI surface

Twelve subcommands, not the nine the docs list:
init, upgrade, status, whoami, devices, dashboard, logout, health,
memory, update, setup, migrate
Global flags: --credentials, --db, --tier-cache

## 6. Database isolation — HARD RULE

DO NOT run `sibyl setup`. It wires Claude Code / Hermes as memory clients
against the default ~/.sibyl-memory/memory.db.
Reasons: (1) coding-session memory would share a DB and cap with the agent's
lesson store; (2) two Sibyl DBs on the machine during the demo recording
invites "which one does the agent actually read?" — a question with no good
on-camera answer.

The agent talks to ./data/reflex.db via the Python client ONLY.
One DB path constant in code. Never inline it. Never the default path.

## 7. STILL UNVERIFIED — the probe must establish these empirically

Cannot be settled by reading source. Print RAW results, do not summarise.

1. Cold-start persistence. Write in one process, exit, read from a genuinely
   fresh process. Confirm data is on disk, not in-session cache.
2. Tokenizer behaviour in practice. Write entities with signature-shaped
   names (e.g. router/swapExactTokensForTokens/slippage-revert) and query via
   search_entities(). Establish which sub-tokens match, whether porter
   stemming alters matching on either side, and how the camelCase segment
   behaves.
3. The real cap figure from free_tier_status() — 5 MB vs 2 MB, settled from
   the running client.

## 8. Standing rules

- Exact match -> get_entity. Neighbour search -> search_entities. Never reverse.
- Lesson state lives in body. The status column is unused.
- Every get_entity call site catches NotFoundError.
- One timestamp helper for every write_event.
- One DB path constant: ./data/reflex.db
- Docs are not authority. Source on disk is authority.

---

## 9. PROBE RESULTS — verified empirically 2026-08-18

All of section 7 now settled. Raw output in probe-output.txt.

**Cold start:** confirmed. Write pid 923288, read pid 923289, matching marker
UUID and created_at. Persistence is real across process boundaries.
Note: DB main file was 4096 bytes at writer exit; data was in WAL, checkpointed
on next open. Do NOT show raw file size on camera as proof of persistence.

**Cross-contract neighbour retrieval: WORKS.** search_entities("slippage
revert") returned BOTH routerA/swapExactTokensForTokens/slippage-revert and
routerB/exactInputSingle/slippage-revert at identical rank. The adjacent-lessons
retrieval path is confirmed.

**camelCase is atomic.** "swapExactTokensForTokens" matched. "swap" and
"tokens" both returned ZERO. Function names searchable only in full.

**Porter stemming works both directions.** "revert" matched bodies containing
"reverted"/"reverting"; "reverting" matched the same set.

**NO substring matching without prefix=True.** Query "router" returned ZERO —
the tokens are "routerA"/"routerB". unicode61 splits on / and - but a trailing
alphanumeric stays attached to the token.
=> RULE: every signature segment must be a COMPLETE token you will query in
full. Test real contract identifiers against this before freezing sig_v1.
prefix=True works as an escape hatch ("swapExact*" hit).

**BM25/IDF gives a usable relevance filter.** "revert" (present in all 5
entities) matched everything at rank ~-1.3e-06. "slippage" (present in 2)
matched at -0.79. Six orders of magnitude apart.
=> RULE: query on the DISTINCTIVE term. Filter neighbours by rank RELATIVE to
the top hit in the result set, not an absolute floor (which drifts as the
corpus grows). Within ~1 order of magnitude = real neighbour.
=> NAMING: failure classes are distinctive-noun + common-suffix.
slippage-revert / allowance-revert / deadline-revert. The suffix groups them
for humans; the noun does all the retrieval work.

**Status trap: CONFIRMED REAL.** status "active" -> null from a second write
that never mentioned status. Silent, no error. HARD RULE: lesson state lives in
body. The status column is never used.

**Revision identity: CONFIRMED.** id unchanged, created_at unchanged (.151Z),
updated_at advanced (.154Z), body v1 -> v2. This before/after comparison is a
DEMO ASSET — put the same thing on screen for a real lesson on Sep 9.

**Cap: 5 MB confirmed twice.** DEFAULT_SOFT_CAP_BYTES = 5242880 and
free_tier_status() soft_cap_bytes = 5242880. Free is the only capped tier;
all others null/uncapped. Docs page 2 MB figure is stale.
Note: empty-ish schema already costs ~274 KB (10 tables + 4 FTS5 + trigram
shadow + WAL). Usable headroom ~4.97 MB. pct_used is a FRACTION not a percent
(0.052 = 5.2%). Print free_tier_status() at the end of the Sep 8 backfill.

**TIE-BREAKING — recurring issue, applies everywhere.** Identical BM25 ranks
for equally-matching entities; updated_at has ms resolution and batch revisions
land ~3ms apart; list_entities orders by updated_at DESC alone.
=> RULE: anywhere ordering is visible or matters, add a deterministic secondary
sort. Repeat runs must produce identical output — a judge will run it twice.
