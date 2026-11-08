"""Deterministic id minting for the statutes tables (PRD "Identity and keys").

Primary keys are opaque deterministic hashes — SHA-256 hexdigest, first 12
chars — of pinned, namespaced inputs. Because the PK is derived from a
stable source attribute, re-running discovery mints the same id and
upsert-on-pk collapses duplicates structurally; nothing ever scans for
duplicates. Citation/natural identifiers (shorthand, gazette numbers,
provision ids) are content fields, never keys.

Pinned hash inputs (changing any of these is a migration — do not improvise):

==================  =============================  ==========================
table               input                          helper
==================  =============================  ==========================
acts                ``act:{shorthand}``            :func:`act_id`
gazette_stream      ``{series}:{year}:{number}``   :func:`gazette_id`
act_versions / sl   ``{parent_id}:{valid_date}``   :func:`version_id`
*_fragments         ``{parent_id}:{provision_id}`` :func:`fragment_id`
==================  =============================  ==========================

``parent_id`` is always the parent row's opaque 12-hex id, so the version
and fragment namespaces can never collide with the catalog namespaces.
"""

from __future__ import annotations

import hashlib


def hash_id(raw: str) -> str:
    """SHA-256 hexdigest of ``raw``, truncated to the first 12 hex chars."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def act_id(shorthand: str) -> str:
    """Acts catalog PK — survives any URL reshuffling by AGC."""
    return hash_id(f"act:{shorthand}")


def gazette_id(series: str, year: int | str, number_seq: int | str) -> str:
    """Gazette stream PK — the gazette number IS the item's published identity."""
    return hash_id(f"{series}:{year}:{number_seq}")


def version_id(parent_id: str, valid_date: str) -> str:
    """Version PK — one expression per work per validity date."""
    return hash_id(f"{parent_id}:{valid_date}")


def fragment_id(parent_id: str, provision_id: str) -> str:
    """Fragment PK — unique within a work; re-extraction replaces wholesale."""
    return hash_id(f"{parent_id}:{provision_id}")
