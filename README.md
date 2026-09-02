# Scar Tissue

> TODO: one-line tagline.

## What it does

TODO: 3-5 sentences. What the agent executes, on what chain, and what it
learns from. Keep it concrete — a reader should know what a run looks like
before reaching the next section.

TODO: demo video link.
TODO: screenshot or terminal capture of a before/after lesson revision.

## Where memory is load-bearing

Every call site where the agent reads from or writes to Sibyl Memory.
Cited by symbol, not by line number: a line number is stale the moment
anything above it moves, and a stale citation costs more credibility than
no citation. `A ← B` means the memory call lives in A and the agent
reaches it from B.

| # | What happens | Direction | Call | Where |
|---|---|---|---|---|
| 1 | Standing lesson looked up for every in-scope failure class, before anything is submitted | READ | `get_entity` | `lessons.py::LessonStore.get_lesson` ← `agent.py::Agent._retrieve` |
| 2 | Adjacent failures on the same contract, queried with one token | READ | `search_entities` | `lessons.py::LessonStore.find_neighbours` ← `agent.py::Agent._retrieve` |
| 3 | Pre-flight claim written before the transaction goes out, and resolved with its outcome after | WRITE | `set_entity` (`open_call/<txRef>`) | `lessons.py::LessonStore.write_open_call` ← `agent.py::Agent.run` |
| 4 | Lesson filed the first time a signature reverts | WRITE | `set_entity` (`lesson/<sig>`) | `lessons.py::LessonStore.write_lesson` ← `agent.py::Agent._learn` |
| 5 | The same lesson revised in place as evidence accumulates | WRITE | `set_entity` (UPDATE, `evidence_count`) | `lessons.py::LessonStore.write_lesson` ← `agent.py::Agent._learn` |
| 6 | Post-mortem appended to the journal | WRITE | `write_event` | `lessons.py::LessonStore.record_postmortem` ← `agent.py::Agent._learn` |

Rows 4 and 5 are one function on purpose. `set_entity` performs a true
UPDATE on conflict, so the first revert and the fifth are the same call —
the row keeps its `id` and `created_at` while `evidence_count` climbs.
Row 3 is likewise called twice per run, with the same `txRef`: once
before submitting, once with the outcome.

Two disclosures rather than a tidier table:

- `find_neighbours` also reads the FTS index directly through the
  client's own connection (`lessons.py::LessonStore._bm25_ranks`).
  `search_entities` orders by BM25 but does not expose the value, and the
  relative rank filter needs the numbers. It uses the SDK's own query
  sanitizer, so the match set is identical.
- `scripts/show_lesson.py::main` reads a lesson too, but it is a
  read-only inspection tool for a human, not part of the agent's path.

**Signature scheme.** Lessons are keyed by `sig_v1`:
`<lowercase_address>/<functionName>/<failureClassCamel>`. Rationale and the
probe results behind it are in [CLAUDE.md](CLAUDE.md).

TODO: worked example of one real signature and the lesson body it maps to.

## The deletion test

The claim is that memory is doing the work. The test is to take the
retrieval away and watch the agent walk into a wall it has already
written down.

The scenario asks for more output than the pool can give. It quotes the
pool with a static `exactInputSingle` call — which returns `amountOut`
without submitting anything — and then sets `amountOutMinimum` 2000 bps
above that real number, so the intent is unsatisfiable as written and
satisfiable once a lesson has widened the tolerance enough.

**With memory.** Run 1 finds no standing lesson, submits the intent as
given, and reverts. The revert is classified as `slippageRevert`, filed
under `<router>/exactInputSingle/slippageRevert`, and journalled. Run 2
retrieves that lesson and reduces `amountOutMinimum` by one step; it
reverts again, and the lesson is revised rather than duplicated —
`evidence_count` becomes 2. By run 5 the accumulated correction is 2000
bps and the identical intent goes through. Four reverts, one success, no
code changed between them.

**Without memory.** The same intent, against the same database, with
`--no-memory`. No `get_entity`, no `search_entities`, and no
`write_lesson` either — `write_lesson` reads the current row to increment
`evidence_count`, and that read is a retrieval. The agent submits the
original `amountOutMinimum` and reverts with the same `slippageRevert`
that run 1 hit. The post-mortem is still written, so the blind run is
still fully evidenced; it simply cannot benefit from anything it
evidenced before.

**What is actually removed.** Not the rows. The lesson is still sitting
in the same database file the blind run is pointed at, with its
`evidence_count` and its correction intact — `scripts/show_lesson.py`
will print it. What the flag removes is the agent's reads. That is a
stronger demonstration than deleting the data would be: nothing is
missing, and the swap still fails.

### How to run it

```sh
# 1. the fork the failures are reproducible against. The block pin is
#    mandatory: an unpinned fork drifts and the failures stop reproducing.
anvil --fork-url "$BASE_RPC_URL" --fork-block-number 50786400

# 2. the environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. .env, which is gitignored and holds two keys:
#      BASE_RPC_URL=<Base mainnet RPC — used ONLY as the fork source>
#      PRIVATE_KEY=<a BURNER key; against the fork, anvil's own dev key>

# 4. the test. Both halves, back to back, in one command.
.venv/bin/python scripts/deletion_test.py
```

Both halves run inside `evm_snapshot` / `evm_revert`, so the fork is left
exactly as it was found, and the lesson store defaults to a throwaway
database so the run starts having learned nothing. Pass `--db` to point
it at a store that persists.

A single run, either way, is also available directly — though it has no
snapshot bracket of its own and will leave the fork moved:

```sh
.venv/bin/python src/agent.py --min-out 28603076
.venv/bin/python src/agent.py --min-out 28603076 --no-memory
```

**Expected output.** Observed against the pinned fork, in about five
seconds:

```
pool returns: 23835897 USDC base units for 10000000000000000 WETH
intent asks : 28603076 (+2000 bps — unreachable as submitted)

--- memory ON, same intent, until the lesson is enough -----
  run 1: min-out 28603076  status 0  slippageRevert  [no standing lesson]
  run 2: min-out 27172922  status 0  slippageRevert  [-500 bps]
  run 3: min-out 25742768  status 0  slippageRevert  [-1000 bps]
  run 4: min-out 24312614  status 0  slippageRevert  [-1500 bps]
  run 5: min-out 22882460  status 1  SUCCESS         [-2000 bps]

--- the SAME intent, --no-memory, SAME store ---------------
  run  : min-out 28603076  status 0  slippageRevert  [retrieval skipped]

======================================================================
                    memory ON                 --no-memory
  --------------------------------------------------------------------
  tx hash           0x645025d7…a81d2dc0       0x8618ad8b…def77815
  status            1  (success)              0  (reverted)
  min-out submitted 22882460                  28603076
  failure class     —                          slippageRevert
  lesson applied    yes, -2000 bps            no, retrieval skipped
======================================================================
```

The full transaction hashes print underneath that table; the short form
is only there to keep it inside eighty columns.

**It exits 0 only when the contrast holds** — that is, when the
memory-on run reached status 1 *and* the `--no-memory` run reverted. It
exits 1 if either half fails to behave that way, with every run printed
above the verdict, and 2 when the environment is not ready (no node, no
`PRIVATE_KEY`, or a node that does not support `evm_snapshot`). So it can
be run as a check rather than read as a claim.

Because the snapshot restores nonces along with state, the run is
deterministic: the two transaction hashes above are the same ones a
second run produces.

## Partner stacks

| Stack | Where it is used | File:line |
|---|---|---|
| Sibyl Memory | TODO: which components — client / CLI / Hermes / MCP | TODO |
| TODO | TODO | TODO |
| TODO | TODO | TODO |

TODO: confirm this list against `requirements.txt` and the submission's
partner criteria — list only what is genuinely load-bearing, not everything
that appears in the dependency tree.

## How memory made this possible

TODO: the honest version. What could not have been built without a
persistent lesson store, and what specifically it changed about the
agent's behaviour between the first and the Nth encounter with the same
failure. Not a feature list.

## Prior work

Nothing in this project predates September 1, 2026. No code, no design, and
no written material was carried over from earlier work. The full commit
history in this repository is the complete record of its construction.

TODO: first commit hash and timestamp, as verifiable evidence.

## License

MIT — see [LICENSE](LICENSE).
