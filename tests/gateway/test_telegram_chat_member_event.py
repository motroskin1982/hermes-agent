"""Actual Telegram membership transitions are emitted without profile data."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter():
    adapter = object.__new__(TelegramAdapter)
    adapter.emitted = []
    adapter.emit_plugin_event = lambda name, payload: adapter.emitted.append((name, payload))
    return adapter


def _update(*, update_id=9, chat_id=-1004451453879, user_id=77,
            old_status="left", new_status="member"):
    user = SimpleNamespace(id=user_id, first_name="Катя", username="katya")
    return SimpleNamespace(
        update_id=update_id,
        chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            old_chat_member=SimpleNamespace(status=old_status, user=user),
            new_chat_member=SimpleNamespace(status=new_status, user=user),
        ),
    )


def _run(adapter, update):
    asyncio.run(adapter._handle_chat_member(update, None))
    return adapter.emitted


def test_actual_entry_is_emitted_as_minimal_primitive_transition():
    adapter = _adapter()
    emitted = _run(adapter, _update())
    assert emitted == [("chat_member", {
        "schema_version": 1,
        "update_id": 9,
        "chat_id": "-1004451453879",
        "user_id": "77",
        "old_status": "left",
        "new_status": "member",
        "old_is_member": False,
        "new_is_member": True,
    })]


def test_profile_data_is_never_emitted():
    adapter = _adapter()
    _name, payload = _run(adapter, _update())[0]
    assert "Катя" not in repr(payload)
    assert "katya" not in repr(payload)


def test_malformed_membership_update_emits_nothing():
    adapter = _adapter()
    bad = SimpleNamespace(update_id=1, chat_member=SimpleNamespace(
        chat=SimpleNamespace(id=-100), old_chat_member=None, new_chat_member=None))
    assert _run(adapter, bad) == []


def test_restricted_counts_only_when_telegram_marks_user_as_member():
    adapter = _adapter()
    entered = _update(old_status="left", new_status="restricted")
    entered.chat_member.new_chat_member.is_member = True
    assert _run(adapter, entered)[0][1]["new_is_member"] is True
    adapter = _adapter()
    excluded = _update(old_status="member", new_status="restricted")
    excluded.chat_member.new_chat_member.is_member = False
    assert _run(adapter, excluded)[0][1]["new_is_member"] is False


def test_handler_is_optional_and_registered_without_disabling_adapter():
    from pathlib import Path
    body = (Path(__file__).resolve().parents[2] / "plugins" / "platforms" /
            "telegram" / "adapter.py").read_text()
    assert "ChatMemberHandler(self._handle_chat_member, ChatMemberHandler.CHAT_MEMBER)" in body
    assert "if ChatMemberHandler is not None:" in body
