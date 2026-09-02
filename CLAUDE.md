# Scar Tissue — agent working rules

An onchain execution agent that learns from its own failed transactions on
Base. When a swap reverts, it writes a post-mortem, stores a lesson in Sibyl
Memory, revises that lesson as evidence accumulates, and retrieves it before
submitting a similar transaction.

These rules were established empirically against the installed SDK, not read
from documentation. Several contradict the docs. Do not "improve" them.

## Environment

- Python. venv at ./.venv. Versions PINNED in requirements.txt.
- sibyl-memory-client 0.8.0, sibyl-memory-cli 0.4.0, web3 8.0.0
- Verified: schema.sql is byte-identical between 0.6.1 and 0.8.0. All
  tokenizer findings below hold. No public method signature changed.
- LOCAL DEV FORK: anvil forking Base mainnet, PINNED to block 50786400.
  RPC URL in .env (gitignored). The pin is MANDATORY — an unpinned fork
  drifts with the chain and induced failures stop reproducing.
- Findings source: ~/sibyl-probe/FINDINGS.md and probe-output.txt (scratch,
  outside this repo). Authority is the SDK source on disk, never the docs.

## sig_v1 — FROZEN. Never change.

    <lowercase_address>/<functionName>/<failureClassCamel>

    0x2626664c2603336e57b271c5c0b26f421741e481/exactInputSingle/slippageRevert

Every lesson is keyed by this. Changing it invalidates every stored lesson.
Every lesson body carries "sig": "v1" so a future change is detectable.

- ADDRESS: lowercased, ALWAYS, through one normalize_address() helper.
  Never str(addr) inline. get_entity is a BINARY SQL comparison —
  case-SENSITIVE — while FTS5 case-folds. Verified: get_entity with a
  lowercased name against a checksummed stored name RAISES NotFoundError,
  and set_entity then creates a SECOND row. web3.py's to_checksum_address
  and a raw eth_getTransactionReceipt disagree on case for the same
  contract. Unnormalized, this is silent memory loss plus split-brain
  lessons. Lowercase is what RPC returns natively.
- FUNCTION: camelCase exactly as in the ABI. camelCase is ATOMIC under
  unicode61 — "swapExactTokensForTokens" matches, "swap" and "tokens"
  return ZERO.
- FAILURE CLASS: camelCase, NO hyphen. slippageRevert, allowanceRevert,
  deadlineRevert. Hyphenated forms shatter into common words that collide
  with body prose. Verified: "slippageRevert" returns only true matches;
  "slippage revert" also matches allowanceRevert rows via body text alone.

## Retrieval — never reverse these

- EXACT: get_entity("lesson", sig). Direct index lookup, no tokenizer.
- NEIGHBOUR: search_entities(token, category="lesson"). Query a SINGLE
  signature token, NEVER prose. Failure class -> same failure across
  contracts. Lowercased address -> all failures on one contract.
  Always pass category="lesson" — 0.8.0 uses it to stop cross-category
  bleed from open_call entities.
- NO substring matching without prefix=True. "router" does not match
  "routerA". Every signature segment must be a COMPLETE token you query
  in full.
- Rank filtering is RELATIVE to the top hit in the result set, never an
  absolute floor (floors drift as the corpus grows). BM25/IDF: a token in
  every row scores ~1e-06, a distinctive token ~-0.79. Six orders apart.
  BUT that six-order spread was measured on a MULTI-token query. On the
  SINGLE-token queries these rules mandate, every matching row contains
  the token, so IDF is UNIFORM across the match set and the only spread
  left is document-length normalisation — measured at ~2.3x at the
  extreme (same token, one body padded 200x longer). Nowhere near a
  one-order window, so the window excludes NOTHING in practice: the FTS
  MATCH is what does the excluding. Keep the filter — it costs nothing
  and still guards the multi-token and pathological-length cases — but
  NEVER rely on it to suppress a bad neighbour. If a bad neighbour must
  be kept out, the token itself has to be wrong for it.

## Hard rules

- LESSON STATE LIVES IN THE BODY. The status column is NEVER used.
  set_entity's UPDATE sets `status = ?` unconditionally and status
  defaults to None — a revision that omits it SILENTLY nulls it. Verified.
- EVERY get_entity call site catches NotFoundError. It RAISES, it does not
  return None. A first-encounter signature is the NORMAL path.
- ONE timestamp helper for every write_event. ts is caller-supplied and
  UNVALIDATED; a malformed string breaks ordering and since/until filters
  silently. Never construct a timestamp inline.
- ONE DB path constant: ./data/reflex.db. Never inlined. Never the default
  ~/.sibyl-memory/memory.db.
- NEVER run `sibyl setup`. It wires Claude Code as a memory client against
  the default DB. Two Sibyl DBs on this machine during the demo recording
  is a question with no good on-camera answer.
- DETERMINISTIC SECONDARY SORT anywhere ordering is visible. Equal BM25
  ranks are common; updated_at has ms resolution and batch revisions land
  ~3ms apart; list_entities orders by updated_at DESC alone. A judge will
  run it twice and the output must be identical.
- NEVER commit .env, keys, or data/. Repo is PUBLIC with a funded wallet
  on this machine. Burner wallet only, demo-sized funds only.

## Memory model

- entity `lesson/<sig>` — the CURRENT standing correction. REVISED via
  set_entity, never appended. Verified: on conflict it is a true UPDATE —
  id and created_at survive, only body/status/updated_at change. So
  created_at = first_seen and updated_at = last_revised, free, as row
  metadata. Do not duplicate them in the body.
- entity `open_call/<tx_ref>` — pre-flight claims, written before submit.
- journal via write_event — append-only post-mortem record. Use the native
  columns, do not blob into extra:
    evaluated -> what the agent believed pre-flight
    acted     -> the transaction submitted, plus tx hash
    forward   -> the correction, and the signature it was filed under
- Free tier cap 5 MB (docs say 2 MB — stale). Empty schema already costs
  ~274 KB. Never journal raw market data or full receipts.

## Build discipline

- COMMIT AT EVERY WORKING INCREMENT. Never squash. Commit history is a
  judged artifact — forty honest commits read better than eight tidy ones.
- NEVER REWRITE HISTORY. No filter-branch, rebase, amend, squash, or
  force-push. History moves forward only: a defect in an existing commit
  — wrong author, typo, stale reference — is corrected by a NEW commit,
  never by rewriting the old one. A config fix (git config user.email)
  corrects future commits and that is the whole remedy; past commits keep
  their metadata. If a rewrite seems warranted, ASK AND STOP.
- Nothing in this project predates Sep 1. No prior work.
- Scope: ONE failure class done completely beats three half-built.
  slippageRevert first.
- The deletion test is a FLAG (--no-memory), not a code edit. It must run
  live, back to back, on camera.
