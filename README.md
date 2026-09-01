# Scar Tissue

> TODO: one-line tagline.

## What it does

TODO: 3-5 sentences. What the agent executes, on what chain, and what it
learns from. Keep it concrete — a reader should know what a run looks like
before reaching the next section.

TODO: demo video link.
TODO: screenshot or terminal capture of a before/after lesson revision.

## Where memory is load-bearing

Every call site where the agent writes to or reads from Sibyl Memory. Line
numbers are exact — click through and read the surrounding function.

| # | What happens | Direction | Call | File:line |
|---|---|---|---|---|
| 1 | TODO: pre-flight lesson lookup before submitting a tx | READ | `get_entity` | TODO: `src/____.py:__` |
| 2 | TODO: neighbour lookup for adjacent failures | READ | `search_entities` | TODO: `src/____.py:__` |
| 3 | TODO: pre-flight claim recorded before submit | WRITE | `set_entity` (`open_call/<tx_ref>`) | TODO: `src/____.py:__` |
| 4 | TODO: lesson stored after a revert | WRITE | `set_entity` (`lesson/<sig>`) | TODO: `src/____.py:__` |
| 5 | TODO: lesson revised as evidence accumulates | WRITE | `set_entity` (revision) | TODO: `src/____.py:__` |
| 6 | TODO: post-mortem appended | WRITE | `write_event` | TODO: `src/____.py:__` |

TODO: confirm every line number above after the code is final — a stale
line number costs more credibility than no line number.

**Signature scheme.** Lessons are keyed by `sig_v1`:
`<lowercase_address>/<functionName>/<failureClassCamel>`. Rationale and the
probe results behind it are in [CLAUDE.md](CLAUDE.md).

TODO: worked example of one real signature and the lesson body it maps to.

## The deletion test

The claim is that memory is doing the work. The test is to remove it and
show the agent fails the same way twice.

TODO: describe what the agent does WITH memory.
TODO: describe what the agent does WITHOUT memory.
TODO: state what exactly is being deleted (which rows / which DB).

### How to run it

```
TODO: setup commands (venv, requirements, .env keys required)
TODO: the with-memory invocation
TODO: the --no-memory invocation
```

TODO: expected output of each run, side by side.
TODO: note the runtime of each so a judge knows what to expect.

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
