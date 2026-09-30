import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"
    telegram_mod.error.NetworkError = type("NetworkError", (OSError,), {})
    telegram_mod.error.TimedOut = type("TimedOut", (OSError,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)
    sys.modules.setdefault("telegram.error", telegram_mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram import adapter as tg_adapter  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


@pytest.mark.asyncio
async def test_connect_retries_when_initialize_wall_deadline_expires(monkeypatch, caplog):
    """A wedged initialize() attempt must not trap startup on attempt 1/8."""
    fake_app = MagicMock()
    fake_app.bot = MagicMock()
    fake_app.initialize = AsyncMock(return_value=None)
    fake_app.start = AsyncMock()
    fake_app.add_handler = MagicMock()

    chainable = MagicMock()
    chainable.token.return_value = chainable
    chainable.request.return_value = chainable
    chainable.get_updates_request.return_value = chainable
    chainable.build.return_value = fake_app

    builder_root = MagicMock()
    builder_root.builder.return_value = chainable
    monkeypatch.setattr(tg_adapter, "Application", builder_root)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", MagicMock)
    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", AsyncMock(return_value=[]))
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *a, **k: None)
    monkeypatch.setattr(tg_adapter.asyncio, "sleep", AsyncMock())

    deadline_calls = 0
    deadline_timeouts = []

    async def _fake_deadline(awaitable, timeout, *, on_abandon=None):
        nonlocal deadline_calls
        deadline_calls += 1
        deadline_timeouts.append(timeout)
        # DoH discovery is the first bounded phase. The first application
        # initialization is the second.
        if deadline_calls == 2:
            awaitable.close()
            raise tg_adapter.asyncio.TimeoutError()
        return await awaitable

    monkeypatch.setattr(tg_adapter, "_await_with_thread_deadline", _fake_deadline)

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])
    monkeypatch.setattr(adapter, "_delete_webhook_best_effort", AsyncMock())
    monkeypatch.setattr(adapter, "_start_polling_resilient", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_polling_heartbeat_loop", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_start_post_connect_housekeeping", MagicMock())

    assert await adapter.connect() is True

    assert fake_app.initialize.call_count == 2
    assert fake_app.initialize.await_count == 1
    assert deadline_calls == 5
    # The adapter leaves five seconds for GatewayRunner to classify and tear
    # down a stalled connection instead of racing its outer 30-second limit.
    assert all(timeout < 30.0 for timeout in deadline_timeouts)
    tg_adapter.asyncio.sleep.assert_awaited_once_with(1)
    fake_app.start.assert_awaited_once()
    phases = [record.getMessage() for record in caplog.records if "bootstrap phase=" in record.getMessage()]
    assert phases == [
        "[Telegram] bootstrap phase=app_initialize_begin",
        "[Telegram] bootstrap phase=app_initialize_begin",
        "[Telegram] bootstrap phase=app_initialized",
        "[Telegram] bootstrap phase=app_start_begin",
        "[Telegram] bootstrap phase=app_started",
        "[Telegram] bootstrap phase=webhook_clear_begin",
        "[Telegram] bootstrap phase=webhook_clear_done result=ok",
        "[Telegram] bootstrap phase=polling_start_begin",
        "[Telegram] bootstrap phase=polling_start_done result=ok",
    ]
    assert "test-token" not in "\n".join(phases)


@pytest.mark.asyncio
async def test_connect_returns_before_outer_deadline_when_initialize_ignores_cancel(monkeypatch):
    """The adapter owns a shorter deadline than GatewayRunner's outer bound.

    PTB/httpcore can enter cancellation-shielded scopes.  If a hanging
    initialize() only sees cancellation after the outer gateway timeout, the
    runner never reaches its retry/error path.  A very short synthetic outer
    budget makes this race deterministic without a 30-second test.
    """
    import asyncio as _asyncio
    import time as _time

    fake_app = MagicMock()
    release = _asyncio.Event()

    async def _cancel_resistant_initialize():
        try:
            await _asyncio.Future()
        except _asyncio.CancelledError:
            await release.wait()

    fake_app.initialize = _cancel_resistant_initialize
    fake_app.start = AsyncMock()
    fake_app.add_handler = MagicMock()
    fake_app.bot = MagicMock()

    chainable = MagicMock()
    chainable.token.return_value = chainable
    chainable.request.return_value = chainable
    chainable.get_updates_request.return_value = chainable
    chainable.build.return_value = fake_app
    builder_root = MagicMock()
    builder_root.builder.return_value = chainable

    monkeypatch.setattr(tg_adapter, "Application", builder_root)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", MagicMock)
    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", AsyncMock(return_value=[]))
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *a, **k: None)
    monkeypatch.setenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "0.3")

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])

    started = _time.monotonic()
    try:
        assert await _asyncio.wait_for(adapter.connect(), timeout=0.3) is False
    finally:
        release.set()
        await _asyncio.sleep(0)
    assert _time.monotonic() - started < 0.3
    fake_app.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_returns_before_outer_deadline_when_polling_ignores_cancel(monkeypatch):
    """A wedged first getUpdates start must fail before GatewayRunner does."""
    import asyncio as _asyncio
    import time as _time

    release = _asyncio.Event()

    async def _cancel_resistant_polling(**_kwargs):
        try:
            await _asyncio.Future()
        except _asyncio.CancelledError:
            await release.wait()

    fake_bot = MagicMock()
    fake_bot.delete_webhook = AsyncMock(return_value=True)
    fake_app = MagicMock()
    fake_app.bot = fake_bot
    fake_app.initialize = AsyncMock()
    fake_app.start = AsyncMock()
    fake_app.shutdown = AsyncMock()
    fake_app.add_handler = MagicMock()
    fake_app.updater = MagicMock()
    fake_app.updater.start_polling = _cancel_resistant_polling

    chainable = MagicMock()
    chainable.token.return_value = chainable
    chainable.request.return_value = chainable
    chainable.get_updates_request.return_value = chainable
    chainable.build.return_value = fake_app
    builder_root = MagicMock()
    builder_root.builder.return_value = chainable

    monkeypatch.setattr(tg_adapter, "Application", builder_root)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", MagicMock)
    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", AsyncMock(return_value=[]))
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *a, **k: None)
    monkeypatch.setenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "0.3")

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])

    started = _time.monotonic()
    try:
        assert await _asyncio.wait_for(adapter.connect(), timeout=0.3) is False
    finally:
        release.set()
        await _asyncio.sleep(0)
    assert _time.monotonic() - started < 0.3
    assert adapter.is_connected is False
    assert adapter._polling_error_task is None


@pytest.mark.asyncio
async def test_connect_budget_starts_before_cancellation_resistant_fallback_discovery(monkeypatch):
    """DoH is part of the same whole-connect deadline, not preamble time."""
    import asyncio as _asyncio
    import time as _time

    release = _asyncio.Event()

    async def _cancel_resistant_discovery():
        try:
            await _asyncio.Future()
        except _asyncio.CancelledError:
            await release.wait()

    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", _cancel_resistant_discovery)
    monkeypatch.setenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "0.3")

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])

    started = _time.monotonic()
    try:
        assert await _asyncio.wait_for(adapter.connect(), timeout=0.3) is False
    finally:
        release.set()
        await _asyncio.sleep(0)
    assert _time.monotonic() - started < 0.3


@pytest.mark.asyncio
async def test_connect_returns_before_outer_deadline_when_webhook_ignores_cancel(monkeypatch):
    """Webhook startup gets the same detached whole-connect deadline."""
    import asyncio as _asyncio
    import agent.secret_scope as secret_scope
    import time as _time

    release = _asyncio.Event()

    async def _cancel_resistant_webhook(**_kwargs):
        try:
            await _asyncio.Future()
        except _asyncio.CancelledError:
            await release.wait()

    fake_app = MagicMock()
    fake_app.bot = MagicMock()
    fake_app.initialize = AsyncMock()
    fake_app.start = AsyncMock()
    fake_app.shutdown = AsyncMock()
    fake_app.add_handler = MagicMock()
    fake_app.updater = MagicMock()
    fake_app.updater.start_webhook = _cancel_resistant_webhook

    chainable = MagicMock()
    chainable.token.return_value = chainable
    chainable.request.return_value = chainable
    chainable.get_updates_request.return_value = chainable
    chainable.build.return_value = fake_app
    builder_root = MagicMock()
    builder_root.builder.return_value = chainable

    monkeypatch.setattr(tg_adapter, "Application", builder_root)
    monkeypatch.setattr(tg_adapter, "HTTPXRequest", MagicMock)
    monkeypatch.setattr(tg_adapter, "discover_fallback_ips", AsyncMock(return_value=[]))
    monkeypatch.setattr(tg_adapter, "resolve_proxy_url", lambda *a, **k: None)
    monkeypatch.setattr(
        secret_scope,
        "get_secret",
        lambda name, default="": {
            "TELEGRAM_WEBHOOK_URL": "https://example.invalid/telegram",
            "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
        }.get(name, default),
    )
    monkeypatch.setenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "0.3")

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_fallback_ips", lambda: [])

    started = _time.monotonic()
    try:
        assert await _asyncio.wait_for(adapter.connect(), timeout=0.3) is False
    finally:
        release.set()
        await _asyncio.sleep(0)
    assert _time.monotonic() - started < 0.3
    assert adapter.is_connected is False


@pytest.mark.asyncio
async def test_await_with_thread_deadline_returns_value_on_happy_path():
    """The real helper returns the awaited result and raises no timeout."""
    async def _ok():
        return 42

    result = await tg_adapter._await_with_thread_deadline(_ok(), timeout=5.0)
    assert result == 42


@pytest.mark.asyncio
async def test_await_with_thread_deadline_abandons_and_runs_cleanup_on_timeout():
    """A wedged awaitable must raise TimeoutError promptly AND trigger the
    best-effort on_abandon cleanup (the httpx-pool-leak guard).

    This exercises the REAL _await_with_thread_deadline (not a monkeypatched
    stub), covering the abandonment + cleanup mechanism directly.
    """
    import asyncio as _asyncio
    import time as _time

    cleanup_ran = _asyncio.Event()

    async def _wedged():
        # Swallows cancellation for a bounded window — long enough that the
        # helper must return control BEFORE this finishes (proving it doesn't
        # await cancellation, the #58236 shielded-scope behavior), but bounded
        # so the abandoned task can't outlive the test and wedge teardown.
        for _ in range(20):
            try:
                await _asyncio.sleep(0.05)
            except _asyncio.CancelledError:
                # Keep going despite cancellation, like the shielded scope.
                pass

    async def _cleanup():
        cleanup_ran.set()

    started = _time.monotonic()
    with pytest.raises(_asyncio.TimeoutError):
        await tg_adapter._await_with_thread_deadline(
            _wedged(), timeout=0.2, on_abandon=_cleanup
        )
    elapsed = _time.monotonic() - started

    # Returned control promptly — well before the wedged coroutine's ~1s span.
    assert elapsed < 0.8
    # The detached cleanup was scheduled; give the loop a tick to run it.
    await _asyncio.wait_for(cleanup_ran.wait(), timeout=2.0)
    assert cleanup_ran.is_set()


@pytest.mark.asyncio
async def test_await_with_thread_deadline_cleanup_error_is_swallowed():
    """A cleanup that raises must not surface as an unhandled task error."""
    import asyncio as _asyncio

    async def _wedged():
        for _ in range(20):
            try:
                await _asyncio.sleep(0.05)
            except _asyncio.CancelledError:
                pass

    def _boom():
        raise RuntimeError("cleanup blew up")

    # Must still raise TimeoutError (not the cleanup error) and not crash.
    with pytest.raises(_asyncio.TimeoutError):
        await tg_adapter._await_with_thread_deadline(
            _wedged(), timeout=0.2, on_abandon=_boom
        )
    # Let the detached cleanup task run and be observed (no unraised error).
    await _asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_shutdown_abandoned_app_closes_request_transports_when_uninitialized():
    """The leak fix must release the httpx transports even when PTB's own
    Application.shutdown()/Bot.shutdown() no-op because the wedged initialize()
    never flipped _initialized. _shutdown_abandoned_app falls back to closing
    each bot._request transport directly (HTTPXRequest.shutdown gates only on
    client.is_closed, not on an init flag)."""
    from unittest.mock import AsyncMock, MagicMock

    # A half-built app: shutdown() is a no-op (uninitialized), but the request
    # transports still hold open httpx clients that must be closed.
    req0 = MagicMock()
    req0.shutdown = AsyncMock()
    req1 = MagicMock()
    req1.shutdown = AsyncMock()
    bot = MagicMock()
    bot._request = (req0, req1)
    app = MagicMock()
    app.bot = bot
    app.shutdown = AsyncMock(return_value=None)  # PTB no-op on uninitialized app

    await tg_adapter._shutdown_abandoned_app(app)

    app.shutdown.assert_awaited_once()
    # Fell back to closing the transports directly — the actual leak fix.
    req0.shutdown.assert_awaited_once()
    req1.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_abandoned_app_handles_none_and_missing_requests():
    """Robust against app=None and an app whose bot/_request aren't present."""
    from unittest.mock import AsyncMock, MagicMock

    # None app -> no-op, no crash.
    await tg_adapter._shutdown_abandoned_app(None)

    # app.shutdown() raising must be swallowed, and missing _request tolerated.
    app = MagicMock()
    app.shutdown = AsyncMock(side_effect=RuntimeError("still running"))
    app.bot = None
    await tg_adapter._shutdown_abandoned_app(app)  # must not raise
