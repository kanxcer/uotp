"""Concurrency: the bot must serve many simultaneous orders without freezing.

python-telegram-bot isn't installed here (these tests import only the transport
helper), so we exercise the two load-bearing scalability properties directly:

  1. the off-loop worker pool is sized from BOT_WORKERS (default 32), NOT the
     ~6-thread asyncio default that would starve past a few concurrent orders;
  2. many concurrent off-loop tasks complete simultaneously (they don't queue
     behind one another), over a wall-time far below their serial sum.
"""

from __future__ import annotations

import asyncio
import time

from uotpbot.bot.telegram import _get_executor, _run_offloop


def test_offtool_pool_is_sized_beyond_6():
    """Default pool must be well above asyncio's ~6 threads, so >10 orders fit."""
    ex = _get_executor()
    # ThreadPoolExecutor stores _max_workers; default was patched to >= 32.
    workers = getattr(ex, "_max_workers", 0)
    assert workers >= 32
    # Sanity cap so a bad BOT_WORKERS can't spawn thousands of threads.
    assert workers <= 128


def test_offtool_pool_respects_bot_workers_env(monkeypatch):
    import uotpbot.bot.telegram as t
    # Reset the lazily-created singleton to observe env-driven sizing.
    monkeypatch.setenv("BOT_WORKERS", "40")
    saved = t._EXECUTOR
    t._EXECUTOR = None
    try:
        ex = t._get_executor()
        assert getattr(ex, "_max_workers", 0) == 40
    finally:
        t._EXECUTOR = saved


def test_many_concurrent_offloop_tasks_run_in_parallel():
    """12 tasks that each sleep 0.2s finish in ~0.2s, not ~2.4s: proof they run
    concurrently on the pool rather than being forced through ~6 threads."""
    async def _main():
        total_sleep = 0.3
        n = 12
        start = time.monotonic()
        await asyncio.gather(
            *[_run_offloop(time.sleep, total_sleep) for _ in range(n)]
        )
        el = time.monotonic() - start
        return el
    el = asyncio.run(_main())
    # If only 6 threads, 12 x 0.3s would take >= 2 x 0.3 = 0.6s. With a 32-thread
    # pool, all 12 run at once -> ~0.3s. Allow slack for scheduler overhead.
    assert el < 0.6, f"concurrent tasks serialised: {el:.2f}s"
