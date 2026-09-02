"""Telegram transport regression tests.

The poller runs in a background thread (the HTTP server owns the main thread).
python-telegram-bot registers UNIX signal handlers by default, which only work
in the main thread -- the exact failure seen live on Render:

    RuntimeError: set_wakeup_fd only works in main thread of the main interpreter

The poller died at startup while the health-checked HTTP server kept serving
200s. This pins the fix: run_polling must be called with stop_signals=().
"""

from __future__ import annotations

import pytest

pytest.importorskip("telegram", reason="python-telegram-bot not installed")

from uotpbot.bot.telegram import _start_polling  # noqa: E402


class FakeApp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run_polling(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_polling_registers_no_signal_handlers():
    app = FakeApp()
    _start_polling(app)
    assert app.calls, "run_polling was never called"
    assert app.calls[0].get("stop_signals") == (), (
        "run_polling must disable PTB's default signal handlers -- they only "
        "work in the main thread and kill a threaded poller at startup"
    )


def test_polling_accepts_all_update_types():
    app = FakeApp()
    _start_polling(app)
    from telegram import Update

    assert app.calls[0].get("allowed_updates") is Update.ALL_TYPES
