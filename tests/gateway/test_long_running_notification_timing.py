"""Regression tests for Telegram long-running receipt/reminder timing."""

import asyncio
from types import SimpleNamespace

import gateway.run as gateway_run


def _timing():
    assert hasattr(gateway_run, "_long_running_notification_timing"), (
        "gateway needs separate first-receipt and repeating reminder delays"
    )
    return gateway_run._long_running_notification_timing()


def test_first_receipt_after_one_minute_then_reminders_every_five_minutes(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INITIAL_DELAY", "60")
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "300")

    assert _timing() == (60.0, 300.0)


def test_notification_timing_uses_owner_friendly_defaults(monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_NOTIFY_INITIAL_DELAY", raising=False)
    monkeypatch.delenv("HERMES_AGENT_NOTIFY_INTERVAL", raising=False)

    assert _timing() == (60.0, 300.0)


def test_zero_repeat_interval_disables_receipts_and_reminders(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INITIAL_DELAY", "60")
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "0")

    assert _timing() == (None, None)


def _format(*, first: bool, elapsed: int, detail: str = "") -> str:
    assert hasattr(gateway_run, "_format_long_running_notification"), (
        "gateway needs a directly tested owner-facing progress formatter"
    )
    return gateway_run._format_long_running_notification(
        first=first,
        elapsed_minutes=elapsed,
        status_detail=detail,
        repeat_interval=300,
    )


def test_first_notification_confirms_receipt_and_current_activity():
    message = _format(
        first=True,
        elapsed=1,
        detail=" — running terminal checks",
    )

    assert "Message received" in message
    assert "not forgotten" in message
    assert "running terminal checks" in message
    assert "every 5 minutes" in message


def test_later_notification_remains_concise_and_activity_aware():
    message = _format(
        first=False,
        elapsed=6,
        detail=" — waiting for browser result",
    )

    assert "Not forgotten" in message
    assert "6 min" in message
    assert "waiting for browser result" in message
    assert "every 5 minutes" not in message


class _NotificationAdapter:
    def __init__(self, *, send_success=True, edit_success=True):
        self.send_success = send_success
        self.edit_success = edit_success
        self.sent = []
        self.edited = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SimpleNamespace(
            success=self.send_success,
            message_id="receipt-1" if self.send_success else None,
        )

    async def edit_message(self, chat_id, message_id, content):
        self.edited.append((chat_id, message_id, content))
        return SimpleNamespace(success=self.edit_success, message_id=message_id)


def _deliver(adapter, *, message_id=None, first=True, should_continue=None):
    assert hasattr(gateway_run, "_deliver_long_running_notification"), (
        "gateway needs a directly tested async receipt delivery helper"
    )
    return asyncio.run(
        gateway_run._deliver_long_running_notification(
            adapter=adapter,
            chat_id="chat-1",
            text="status",
            metadata={"thread_id": "topic-1"},
            heartbeat_message_id=message_id,
            first_notification=first,
            should_continue=should_continue,
        )
    )


def test_failed_first_delivery_retries_the_receipt_state():
    adapter = _NotificationAdapter(send_success=False)

    message_id, first = _deliver(adapter)

    assert message_id is None
    assert first is True


def test_successful_first_delivery_records_message_for_later_edits():
    adapter = _NotificationAdapter()

    message_id, first = _deliver(adapter)

    assert message_id == "receipt-1"
    assert first is False


def test_later_notification_edits_the_same_message():
    adapter = _NotificationAdapter()

    message_id, first = _deliver(
        adapter,
        message_id="receipt-1",
        first=False,
    )

    assert message_id == "receipt-1"
    assert first is False
    assert adapter.sent == []
    assert adapter.edited == [("chat-1", "receipt-1", "status")]


def test_edit_failure_does_not_fallback_send_after_ownership_loss():
    adapter = _NotificationAdapter(edit_success=False)
    lifecycle = iter((True, False))

    message_id, first = _deliver(
        adapter,
        message_id="receipt-1",
        first=False,
        should_continue=lambda: next(lifecycle),
    )

    assert message_id == "receipt-1"
    assert first is False
    assert adapter.edited == [("chat-1", "receipt-1", "status")]
    assert adapter.sent == []


def test_stream_delivery_holder_reports_final_reply_safely():
    assert gateway_run._stream_final_delivery_confirmed(
        [SimpleNamespace(final_response_sent=True)]
    ) is True
    assert gateway_run._stream_final_delivery_confirmed([None]) is False


def test_notification_stops_after_final_stream_delivery():
    runner = object.__new__(gateway_run.GatewayRunner)
    agent = object()
    runner._running_agents = {"session": agent}
    live_executor = SimpleNamespace(done=lambda: False)

    assert runner._should_emit_long_running_notification(
        "session",
        agent,
        live_executor,
        final_delivery_confirmed=True,
    ) is False
