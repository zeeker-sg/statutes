"""Tests for resources/ids.py — deterministic id minting per PRD pinned inputs."""

from __future__ import annotations

import hashlib

import ids


def sha12(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# =============================================================================
# hash_id
# =============================================================================
def test_hash_id_known_vector():
    assert ids.hash_id("act:IA1965") == sha12("act:IA1965")
    # And against a raw hashlib literal, independent of the helper.
    assert ids.hash_id("act:IA1965") == hashlib.sha256(b"act:IA1965").hexdigest()[:12]


def test_hash_id_is_12_lowercase_hex_chars():
    for raw in ("act:IA1965", "sl_supp:2026:355", "", "unicode-ée"):
        out = ids.hash_id(raw)
        assert len(out) == 12
        assert all(c in "0123456789abcdef" for c in out)


def test_hash_id_deterministic():
    assert ids.hash_id("act:AA2004") == ids.hash_id("act:AA2004")


# =============================================================================
# Namespaced minting — pinned inputs from PRD "Identity and keys"
# =============================================================================
def test_act_id_pins_act_namespace():
    assert ids.act_id("IA1965") == sha12("act:IA1965")
    assert ids.act_id("ACRAA2022") == sha12("act:ACRAA2022")


def test_gazette_id_pins_series_year_seq():
    assert ids.gazette_id("sl_supp", 2026, 355) == sha12("sl_supp:2026:355")
    assert ids.gazette_id("acts_supp", "2025", "19") == sha12("acts_supp:2025:19")


def test_version_id_pins_parent_and_valid_date():
    parent = ids.act_id("IA1965")
    assert ids.version_id(parent, "2025-12-05") == sha12(f"{parent}:2025-12-05")


def test_fragment_id_pins_parent_and_provision():
    parent = ids.act_id("IA1965")
    assert ids.fragment_id(parent, "IA1965/s9A") == sha12(f"{parent}:IA1965/s9A")


# =============================================================================
# Namespace separation
# =============================================================================
def test_act_namespace_does_not_collide_with_raw_hash():
    assert ids.act_id("IA1965") != ids.hash_id("IA1965")


def test_gazette_series_do_not_collide():
    assert ids.gazette_id("sl_supp", 2025, 19) != ids.gazette_id("acts_supp", 2025, 19)


def test_versions_of_different_parents_do_not_collide():
    a = ids.version_id(ids.act_id("IA1965"), "2025-12-05")
    b = ids.version_id(ids.act_id("AA2004"), "2025-12-05")
    assert a != b


def test_fragments_of_different_parents_do_not_collide():
    a = ids.fragment_id(ids.act_id("IA1965"), "s2")
    b = ids.fragment_id(ids.act_id("AA2004"), "s2")
    assert a != b
