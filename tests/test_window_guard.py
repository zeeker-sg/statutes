"""Tests for the SSO Terms-of-Use extraction-window guard (resources.sso_window).

No network. Every predicate is exercised with explicit tz-aware datetimes so
the boundary semantics (window open/close, hard stop) are pinned exactly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from resources.sso_window import (
    SGT,
    WindowClosedError,
    hard_stop_reached,
    in_window,
    now_sgt,
    require_window,
    window_allows_request,
)


def sgt(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """A tz-aware SGT datetime on an arbitrary fixed date."""
    return datetime(2026, 6, 11, hour, minute, second, tzinfo=SGT)


class TestNowSgt:
    def test_is_tz_aware_sgt(self):
        now = now_sgt()
        assert now.tzinfo is not None
        assert now.utcoffset() == sgt(3).utcoffset()  # SGT is UTC+8, no DST


class TestInWindowBoundaries:
    def test_just_before_open_02_59_59(self):
        assert in_window(sgt(2, 59, 59)) is False

    def test_open_exactly_03_00_00(self):
        assert in_window(sgt(3, 0, 0)) is True

    def test_just_before_hard_stop_06_44_59(self):
        assert in_window(sgt(6, 44, 59)) is True

    def test_hard_stop_06_45_00_still_in_window(self):
        # 06:45 is the new-request hard stop, but the window itself is open
        # until 07:00 (in-flight work may finish).
        assert in_window(sgt(6, 45, 0)) is True

    def test_just_before_close_06_59_59(self):
        assert in_window(sgt(6, 59, 59)) is True

    def test_close_exactly_07_00_00(self):
        assert in_window(sgt(7, 0, 0)) is False


class TestHardStopBoundaries:
    def test_not_reached_at_open_03_00_00(self):
        assert hard_stop_reached(sgt(3, 0, 0)) is False

    def test_not_reached_at_06_44_59(self):
        assert hard_stop_reached(sgt(6, 44, 59)) is False

    def test_reached_exactly_at_06_45_00(self):
        assert hard_stop_reached(sgt(6, 45, 0)) is True

    def test_reached_at_06_59_59(self):
        assert hard_stop_reached(sgt(6, 59, 59)) is True

    def test_reached_outside_window_before_open(self):
        assert hard_stop_reached(sgt(2, 59, 59)) is True

    def test_reached_outside_window_after_close(self):
        assert hard_stop_reached(sgt(7, 0, 0)) is True


class TestWindowAllowsRequest:
    @pytest.mark.parametrize(
        ("when", "allowed"),
        [
            (sgt(2, 59, 59), False),  # before open
            (sgt(3, 0, 0), True),  # window opens
            (sgt(6, 44, 59), True),  # last second before hard stop
            (sgt(6, 45, 0), False),  # hard stop: no NEW requests
            (sgt(6, 59, 59), False),  # in window but past hard stop
            (sgt(7, 0, 0), False),  # window closed
        ],
    )
    def test_boundaries(self, monkeypatch, when, allowed):
        monkeypatch.delenv("STATUTES_IGNORE_WINDOW", raising=False)
        assert window_allows_request(when) is allowed


class TestTimezoneConversion:
    def test_utc_datetime_converted_to_sgt(self):
        # 19:30 UTC == 03:30 SGT (UTC+8): inside the window.
        dt_utc = datetime(2026, 6, 10, 19, 30, 0, tzinfo=timezone.utc)
        assert in_window(dt_utc) is True
        assert hard_stop_reached(dt_utc) is False

    def test_utc_datetime_outside_window(self):
        # 12:00 UTC == 20:00 SGT: outside.
        dt_utc = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
        assert in_window(dt_utc) is False

    def test_utc_datetime_past_hard_stop(self):
        # 22:50 UTC == 06:50 SGT: in window, past the 06:45 hard stop.
        dt_utc = datetime(2026, 6, 10, 22, 50, 0, tzinfo=timezone.utc)
        assert in_window(dt_utc) is True
        assert hard_stop_reached(dt_utc) is True

    def test_naive_datetime_rejected(self):
        # Documented policy: naive datetimes are ambiguous and a wrong guess
        # is a ToU violation, so the guard rejects them outright.
        naive = datetime(2026, 6, 11, 3, 30, 0)
        with pytest.raises(ValueError, match="naive datetime"):
            in_window(naive)
        with pytest.raises(ValueError, match="naive datetime"):
            hard_stop_reached(naive)


class TestRequireWindow:
    def test_raises_outside_window(self, monkeypatch):
        monkeypatch.delenv("STATUTES_IGNORE_WINDOW", raising=False)
        monkeypatch.setattr("resources.sso_window.now_sgt", lambda: sgt(12, 0, 0))
        with pytest.raises(WindowClosedError):
            require_window()

    def test_raises_at_hard_stop(self, monkeypatch):
        monkeypatch.delenv("STATUTES_IGNORE_WINDOW", raising=False)
        monkeypatch.setattr("resources.sso_window.now_sgt", lambda: sgt(6, 45, 0))
        with pytest.raises(WindowClosedError):
            require_window()

    def test_passes_inside_window(self, monkeypatch):
        monkeypatch.delenv("STATUTES_IGNORE_WINDOW", raising=False)
        monkeypatch.setattr("resources.sso_window.now_sgt", lambda: sgt(4, 0, 0))
        require_window()  # must not raise

    def test_error_message_cites_terms_of_use(self, monkeypatch):
        monkeypatch.delenv("STATUTES_IGNORE_WINDOW", raising=False)
        monkeypatch.setattr("resources.sso_window.now_sgt", lambda: sgt(12, 0, 0))
        with pytest.raises(WindowClosedError, match=r"13\(d\)\(i\)"):
            require_window()


class TestIgnoreWindowEscapeHatch:
    def test_window_allows_request_bypassed(self, monkeypatch):
        monkeypatch.setenv("STATUTES_IGNORE_WINDOW", "1")
        assert window_allows_request(sgt(12, 0, 0)) is True
        assert window_allows_request(sgt(6, 45, 0)) is True

    def test_require_window_bypassed(self, monkeypatch):
        monkeypatch.setenv("STATUTES_IGNORE_WINDOW", "1")
        monkeypatch.setattr("resources.sso_window.now_sgt", lambda: sgt(12, 0, 0))
        require_window()  # must not raise

    def test_other_values_do_not_bypass(self, monkeypatch):
        monkeypatch.setenv("STATUTES_IGNORE_WINDOW", "0")
        assert window_allows_request(sgt(12, 0, 0)) is False

    def test_in_window_and_hard_stop_unaffected_by_env(self, monkeypatch):
        # The escape hatch lives in window_allows_request only; the raw
        # predicates always report the truth.
        monkeypatch.setenv("STATUTES_IGNORE_WINDOW", "1")
        assert in_window(sgt(12, 0, 0)) is False
        assert hard_stop_reached(sgt(6, 50, 0)) is True
