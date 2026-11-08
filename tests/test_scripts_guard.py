"""The live-fetching scripts hard-refuse fixture-only env vars (ToU guard).

``scripts/fetch_fixtures.py`` and ``scripts/phase0_spike.py`` exist solely to
fetch from the live site, so an inherited ``STATUTES_IGNORE_WINDOW`` /
``STATUTES_NO_DELAY`` would silently defeat the extraction-window guard and
the robots.txt crawl-delay pacing. Both must exit 2 before building any
client. These tests additionally disable httpx's transport entirely, so even
a regression here can never let a request leave the process.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from click.testing import CliRunner

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Belt and braces: any HTTP send in these tests is a hard failure."""

    def refuse(*args, **kwargs):
        raise AssertionError("network disabled: live-fetch script issued a request in tests")

    monkeypatch.setattr(httpx.Client, "send", refuse)


@pytest.mark.parametrize("script_name", ["fetch_fixtures", "phase0_spike"])
@pytest.mark.parametrize(
    "env_vars",
    [
        ("STATUTES_IGNORE_WINDOW",),
        ("STATUTES_NO_DELAY",),
        ("STATUTES_IGNORE_WINDOW", "STATUTES_NO_DELAY"),
    ],
)
def test_live_scripts_refuse_fixture_env(script_name, env_vars, monkeypatch):
    monkeypatch.delenv("STATUTES_IGNORE_WINDOW", raising=False)
    monkeypatch.delenv("STATUTES_NO_DELAY", raising=False)
    for name in env_vars:
        monkeypatch.setenv(name, "1")

    module = _load_script(script_name)
    result = CliRunner().invoke(module.main, [])

    assert result.exit_code == 2
    assert "ERROR" in result.output  # click>=8.2: output combines stdout+stderr
    for name in env_vars:
        assert name in result.output


@pytest.mark.parametrize("script_name", ["fetch_fixtures", "phase0_spike"])
def test_refusal_fires_even_when_var_is_set_to_zero(script_name, monkeypatch):
    """Presence is the trigger: the scripts have no fixture mode at all, so
    any value (even '0') means the caller's shell is configured for fixture
    work and must not run a live fetcher."""
    monkeypatch.delenv("STATUTES_NO_DELAY", raising=False)
    monkeypatch.setenv("STATUTES_IGNORE_WINDOW", "0")

    module = _load_script(script_name)
    result = CliRunner().invoke(module.main, [])

    assert result.exit_code == 2
