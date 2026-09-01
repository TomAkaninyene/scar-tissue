"""Memory primitives for Scar Tissue: paths, identifiers, timestamps.

Deliberately dependency-free and side-effect-light. No Sibyl client is
constructed here and no lesson logic lives here — this module exists so that
the things CLAUDE.md requires to be defined exactly ONCE are defined exactly
once, and every other module imports them rather than reconstructing them.

Rules enforced here (see CLAUDE.md for the probe evidence behind each):
  - one DB path constant, never inlined, never the SDK default
  - one address normalizer, because get_entity is a case-SENSITIVE binary
    comparison while FTS5 case-folds
  - one signature builder for the FROZEN sig_v1 scheme
  - one timestamp helper, because write_event's ts is caller-supplied and
    completely unvalidated
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Database location
# --------------------------------------------------------------------------
# THE one DB path. Never inline this string. Never fall back to the SDK
# default (~/.sibyl-memory/memory.db) — a second Sibyl DB on this machine is
# a question with no good answer on camera.
#
# Anchored to the repo root rather than the process CWD so that the path is
# identical whether the agent is launched from the repo root, from src/, or
# by a test runner.
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "reflex.db"

# data/ is gitignored, so a fresh clone does not contain it. Create it at
# import time: the deletion test must work on a clean checkout.
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# sig_v1 — FROZEN. See CLAUDE.md.
# --------------------------------------------------------------------------
# Carried in every lesson body as {"sig": SIG_VERSION} so that a future
# scheme change is detectable in already-stored rows.
SIG_VERSION = "v1"

SIG_SEPARATOR = "/"

_ADDRESS_RE = re.compile(r"\A0x[0-9a-fA-F]{40}\Z")
# Function names and failure classes must be single unicode61 tokens: letters
# and digits only. A hyphen, underscore, dot or space would split the segment
# into separate tokens that collide with common body prose.
_SEGMENT_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9]*\Z")


def normalize_address(address: str) -> str:
    """Return a 0x-prefixed, all-lowercase contract address.

    THE single point at which an address becomes a memory key. Never call
    str() on an address inline instead of this.

    get_entity resolves names by binary SQL comparison, so a checksummed
    name and a lowercased name are two different rows with two different
    ids — one write lands in each and neither can see the other. web3.py's
    to_checksum_address and a raw eth_getTransactionReceipt disagree on case
    for the same contract, so the collision is reachable in normal use, not
    hypothetical. Lowercase wins because it is what RPC returns natively.
    """
    if not isinstance(address, str):
        raise TypeError(f"address must be str, got {type(address).__name__}")
    candidate = address.strip()
    if not _ADDRESS_RE.match(candidate):
        raise ValueError(
            f"not a 20-byte hex address: {address!r} "
            "(expected 0x followed by 40 hex characters)"
        )
    return candidate.lower()


def _validate_segment(value: str, *, field: str) -> str:
    """Reject anything that would not survive unicode61 as a single token."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be str, got {type(value).__name__}")
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field} must not be empty")
    if not _SEGMENT_RE.match(candidate):
        raise ValueError(
            f"{field} must be a single alphanumeric token, got {value!r}. "
            "Hyphens, underscores, dots and spaces split under unicode61 into "
            "common words that collide with body prose — use camelCase."
        )
    return candidate


def build_signature(address: str, function_name: str, failure_class: str) -> str:
    """Build a sig_v1 memory key.

        <lowercase_address>/<functionName>/<failureClassCamel>

    e.g. 0x2626664c...741e481/exactInputSingle/slippageRevert

    Case is preserved exactly for the function name and failure class:
    camelCase is atomic under unicode61, so these segments are retrievable
    only when queried in full, and lowercasing them here would not change
    what matches but would make the key unreadable.

    Pure and deterministic — the same inputs always produce the same string.
    """
    return SIG_SEPARATOR.join((
        normalize_address(address),
        _validate_segment(function_name, field="function_name"),
        _validate_segment(failure_class, field="failure_class"),
    ))


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------
def utc_now_iso(when: datetime | None = None) -> str:
    """Return a Sibyl-compatible UTC timestamp: 2026-09-01T12:34:56.789Z

    THE single timestamp helper. Never construct one of these inline.

    write_event's ts is caller-supplied and entirely unvalidated; a
    malformed string breaks ORDER BY and the since/until range filters
    silently, with no error. The format here matches the SDK's own
    _utc_now_iso exactly — millisecond precision, 'Z' suffix — because
    ordering is a lexicographic text comparison against rows the SDK
    timestamped itself, and 6-digit microseconds would sort wrongly.

    Pass ``when`` to backfill a post-mortem with the TRUE timestamp of the
    real transaction. Never pass a time that has no real event behind it.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    elif when.tzinfo is None:
        raise ValueError("naive datetime rejected; pass an aware UTC datetime")
    else:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"
