"""Poll answers reach observer plugins as identifiers, never as content.

Telegram delivers `poll_answer` only because the adapter subscribes to
Update.ALL_TYPES; nothing consumed it before, so a bot could create a poll and
never learn the result. This handler emits the same shape as the join-request
observer: ids and option indexes, no names.

There was no core test for the join-request emitter either, and that is exactly
how a gateway change shipped in September 2026 that stopped a downstream plugin
seeing a verified actor at all. This one is tested.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter():
    adapter = object.__new__(TelegramAdapter)
    adapter.emitted = []
    adapter.emit_plugin_event = lambda name, payload: adapter.emitted.append((name, payload))
    return adapter


def _answer(*, poll_id="p1", user_id=77, option_ids=(0,), update_id=5):
    return SimpleNamespace(
        update_id=update_id,
        poll_answer=SimpleNamespace(
            poll_id=poll_id,
            user=SimpleNamespace(id=user_id, first_name="Катя", username="katya"),
            option_ids=list(option_ids),
        ),
    )


def _run(adapter, update):
    asyncio.run(adapter._handle_poll_answer(update, None))
    return adapter.emitted


def test_a_vote_is_emitted_as_identifiers():
    adapter = _adapter()
    emitted = _run(adapter, _answer(option_ids=(1,)))
    assert len(emitted) == 1
    name, payload = emitted[0]
    assert name == "poll_answer"
    assert payload["poll_id"] == "p1"
    assert payload["user_id"] == "77"
    assert payload["option_ids"] == [1]
    assert payload["retracted"] is False
    assert payload["schema_version"] == 1


def test_the_voters_name_is_never_emitted():
    """An observer that wants a name already has its own member record."""
    adapter = _adapter()
    _name, payload = _run(adapter, _answer())[0]
    flat = repr(payload)
    assert "Катя" not in flat and "katya" not in flat


def test_a_retracted_vote_is_passed_through_as_a_retraction():
    """Telegram signals "I changed my mind" with an empty option list. Dropping
    it would leave a counted vote nobody is standing behind."""
    adapter = _adapter()
    _name, payload = _run(adapter, _answer(option_ids=()))[0]
    assert payload["option_ids"] == []
    assert payload["retracted"] is True


def test_a_malformed_update_emits_nothing():
    adapter = _adapter()
    for update in (
        SimpleNamespace(update_id=1, poll_answer=None),
        SimpleNamespace(update_id=1, poll_answer=SimpleNamespace(
            poll_id="p", user=None, option_ids=[0])),
        SimpleNamespace(update_id=1, poll_answer=SimpleNamespace(
            poll_id="", user=SimpleNamespace(id=1), option_ids=[0])),
        SimpleNamespace(update_id=1, poll_answer=SimpleNamespace(
            poll_id="p", user=SimpleNamespace(id=1), option_ids=["not-an-index"])),
    ):
        assert _run(_adapter(), update) == []


def test_the_handler_is_registered_and_degrades_instead_of_disappearing():
    """Same discipline as the join-request observer: an older
    python-telegram-bot must disable this ONE feature loudly, not the whole
    Telegram adapter silently."""
    from pathlib import Path
    body = (Path(__file__).resolve().parents[2]
            / "plugins" / "platforms" / "telegram" / "adapter.py").read_text()
    assert "PollAnswerHandler(self._handle_poll_answer)" in body
    assert "if PollAnswerHandler is not None:" in body
    assert "poll results will not be counted" in body
