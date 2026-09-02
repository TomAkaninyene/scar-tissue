# research/

The empirical probes this project was designed from.

Nothing here is project code. Nothing in `src/` imports it, no test covers
it, and none of it runs as part of the agent. These are one-shot scripts
and their raw output, kept because every rule in
[CLAUDE.md](../CLAUDE.md) claims to be verified, and this is what
"verified" means here: the behaviour was induced and observed against the
packages actually installed and a chain actually forked, rather than read
from documentation and assumed.

Two rules exist only because a probe contradicted the obvious assumption —
`get_entity` raises where you would expect `None`, and one revert string
covers two entirely different causes.

## What is here

| File | What it establishes | Run against |
|---|---|---|
| `FINDINGS.md` | Sibyl Memory SDK behaviour: FTS5 tokenization, `set_entity` UPDATE semantics, the `status` column trap, journal columns, tier limits | the installed `sibyl-memory-client` source on disk — `client.py` and `schema.sql`, not the published docs |
| `probe_section7.py` | The script behind section 7 of `FINDINGS.md` | the installed SDK |
| `probe-output.txt` | Raw output of that run, unedited | — |
| `probe_sig.py` | Whether REAL Base contract identifiers survive unicode61 as single tokens — the probe that froze `sig_v1` before anything was keyed on it | the installed SDK, scratch DB |
| `probe_revert.py` | Four induced failures on Base v3 `SwapRouter02` — slippage, allowance, balance, deadline — with the raw `ContractLogicError`, the receipt, and the unwrapped node response for each | an anvil fork of Base mainnet pinned to block 50786400 |

## Provenance

The SDK probes — `FINDINGS.md`, `probe_section7.py`, `probe-output.txt` —
were run on 2026-08-18 against a scratch install, before this repository
existed. They are environment research rather than project code: read them
as "here is what the SDK does", not "here is something that was built
here".

`probe_sig.py` was run on 2026-09-01 and `probe_revert.py` on 2026-09-02,
both alongside this repository rather than before it.

## Re-running the revert probe

It needs the pinned fork up:

```sh
anvil --fork-url "$BASE_RPC_URL" --fork-block-number 50786400
.venv/bin/python research/probe_revert.py
```

It brackets itself in `evm_snapshot` / `evm_revert` and reverts before
each case, so every failure starts from identical state and the fork is
left as it was found. It uses anvil's own dev account; that key is in the
file, it is the published test key anvil prints on startup, and it must
never be used anywhere but a local fork.

The output is deliberately raw — exception class, full `args`, `.message`,
`.data`, the receipt, and the node's JSON-RPC error. No classification
happens in the probe. Classification is `src/failures.py`, and it was
written after this output existed, not before.
