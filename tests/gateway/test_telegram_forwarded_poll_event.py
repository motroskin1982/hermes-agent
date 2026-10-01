"""Forwarded native polls reach profile hooks without leaking content to an LLM."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.platforms.telegram.adapter import MessageType, TelegramAdapter


def _adapter():
    adapter = object.__new__(TelegramAdapter)
    adapter._is_user_authorized_from_message = lambda _message: True
    adapter._should_process_message = lambda _message: True
    adapter._build_message_event = lambda message, kind, update_id: SimpleNamespace(
        text=message.text or "",
        message_type=kind,
        raw_message=message,
        platform_update_id=update_id,
    )
    adapter.handled = []

    async def handle(event):
        adapter.handled.append(event)

    adapter.handle_message = handle
    return adapter


def _update(*, forwarded=True, poll=True, update_id=41):
    return SimpleNamespace(
        update_id=update_id,
        effective_message=SimpleNamespace(
            text=None,
            poll=(SimpleNamespace(
                question="Who voted?", options=[SimpleNamespace(text="A", voter_count=3)]
            ) if poll else None),
            forward_origin=SimpleNamespace(type="user") if forwarded else None,
        ),
    )


def _run(adapter, update):
    asyncio.run(adapter._handle_forwarded_poll_message(update, None))
    return adapter.handled


def test_forwarded_native_poll_reaches_pre_dispatch_as_a_neutral_event():
    adapter = _adapter()
    handled = _run(adapter, _update())
    assert len(handled) == 1
    event = handled[0]
    assert event.message_type is MessageType.TEXT
    assert event.text == "[Telegram forwarded native poll]"
    assert event.platform_update_id == 41
    assert event.raw_message.poll.question == "Who voted?"


def test_question_options_and_voter_counts_never_enter_event_text():
    event = _run(_adapter(), _update())[0]
    assert "Who voted?" not in event.text
    assert "A" not in event.text
    assert "3" not in event.text


def test_unforwarded_or_malformed_poll_is_ignored():
    for update in (_update(forwarded=False), _update(poll=False), SimpleNamespace(update_id=1, effective_message=None)):
        assert _run(_adapter(), update) == []


def test_authorization_and_group_trigger_still_gate_the_event():
    adapter = _adapter()
    adapter._is_user_authorized_from_message = lambda _message: False
    assert _run(adapter, _update()) == []
    adapter = _adapter()
    adapter._should_process_message = lambda _message: False
    assert _run(adapter, _update()) == []


def test_native_poll_handler_is_registered_separately_from_text_and_media():
    from pathlib import Path

    body = (Path(__file__).resolve().parents[2] / "plugins" / "platforms" /
            "telegram" / "adapter.py").read_text()
    assert "filters.POLL" in body
    assert "self._handle_forwarded_poll_message" in body
