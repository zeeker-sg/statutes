"""Tests for scripts/capture_block_id.py: one request, window-guarded, one-shot."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "capture_block_id.py"
SGT_IN_WINDOW = datetime(2026, 11, 27, 3, 10)
BLOCK_BODY = (
    "<html><body>Request unsuccessful. Incapsula incident ID: "
    "1234000560012345678-123456789012345</body></html>"
)


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.delenv("STATUTES_IGNORE_WINDOW", raising=False)
    monkeypatch.delenv("STATUTES_NO_DELAY", raising=False)
    spec = importlib.util.spec_from_file_location("capture_block_id", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _counting_transport(calls: list, status=403, body=BLOCK_BODY, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, text=body, headers=headers or {})

    return httpx.MockTransport(handler)


def _open_window(mod, monkeypatch):
    monkeypatch.setattr(mod.sso_window, "require_window", lambda: None)
    sgt = mod.sso_window.SGT
    monkeypatch.setattr(mod.sso_window, "now_sgt", lambda: SGT_IN_WINDOW.replace(tzinfo=sgt))


def test_sends_one_honest_request_and_saves_incident_id(mod, monkeypatch, tmp_path):
    _open_window(mod, monkeypatch)
    calls: list = []
    transport = _counting_transport(calls, headers={"X-Iinfo": "9-123-0 NNNN CT(0 0 0)"})

    assert mod.capture(transport=transport, capture_dir=tmp_path) == 0

    assert len(calls) == 1
    assert str(calls[0].url) == "https://sso.agc.gov.sg/robots.txt"
    assert calls[0].headers["user-agent"] == "ZeekerBot/1.0 (+https://data.zeeker.sg)"
    saved = list(tmp_path.glob("capture_*.txt"))
    assert len(saved) == 1
    text = saved[0].read_text()
    assert "1234000560012345678-123456789012345" in text
    assert "x-iinfo: 9-123-0 NNNN CT(0 0 0)" in text
    assert "status: 403" in text


def test_second_run_sends_nothing(mod, monkeypatch, tmp_path):
    _open_window(mod, monkeypatch)
    calls: list = []
    mod.capture(transport=_counting_transport(calls), capture_dir=tmp_path)
    mod.capture(transport=_counting_transport(calls), capture_dir=tmp_path)
    assert len(calls) == 1


def test_closed_window_sends_nothing(mod, monkeypatch, tmp_path):
    def closed():
        raise mod.sso_window.WindowClosedError("closed")

    monkeypatch.setattr(mod.sso_window, "require_window", closed)
    calls: list = []
    assert mod.capture(transport=_counting_transport(calls), capture_dir=tmp_path) == 0
    assert calls == []
    assert not list(tmp_path.glob("capture_*.txt"))


@pytest.mark.parametrize("var", ["STATUTES_IGNORE_WINDOW", "STATUTES_NO_DELAY"])
def test_refuses_fixture_env(mod, monkeypatch, var):
    monkeypatch.setattr(
        httpx.Client, "send", lambda *a, **k: pytest.fail("request sent despite refusal")
    )
    monkeypatch.setenv(var, "1")
    result = CliRunner().invoke(mod.main, [])
    assert result.exit_code == 2
    assert var in result.output
