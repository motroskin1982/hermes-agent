"""Every session must carry the profile that produced it.

A profile-scoped plugin decides whether a Telegram actor is verified by
comparing the session's stored profile name against its own. A single-profile
gateway used to persist NULL for the primary adapter — only secondary
multiplexed profiles were stamped — so such a plugin refused every tool call it
was asked to make.

Live consequence in the Nova Teen Club, for weeks: the bot held conversations
with children and could not store a name, admit anyone, hand out the group link
or notify its owner. It looked like a dozen unrelated bugs and was one.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB

RUN_PY = (Path(__file__).resolve().parents[2] / "gateway" / "run.py").read_text()


def test_no_registration_leaves_the_handler_unstamped():
    """Both the initial wiring and the reconnect path. A transient network drop
    that silently reverts to unstamped sessions is the same bug returning."""
    assert "set_message_handler(self._handle_message)" not in RUN_PY
    assert RUN_PY.count("_make_profile_message_handler(self._active_profile_name())") >= 2


def test_the_stamping_handler_sets_the_profile_and_delegates():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    seen = []

    async def _handle(event):
        seen.append(event)
        return "handled"

    runner._handle_message = _handle
    handler = GatewayRunner._make_profile_message_handler(runner, "nova-teen-club")
    event = SimpleNamespace(source=SimpleNamespace(profile=None))
    assert asyncio.run(handler(event)) == "handled"
    assert event.source.profile == "nova-teen-club"
    assert seen == [event]


def test_an_already_stamped_event_is_never_relabelled():
    """A multiplexed secondary profile stamped it first; overwriting would
    hand one profile's traffic to another's plugins."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)

    async def _handle(event):
        return None

    runner._handle_message = _handle
    handler = GatewayRunner._make_profile_message_handler(runner, "nova-teen-club")
    event = SimpleNamespace(source=SimpleNamespace(profile="someone-else"))
    asyncio.run(handler(event))
    assert event.source.profile == "someone-else"


def test_stamping_cannot_break_message_handling():
    """A malformed event must still reach the handler: dropping a child's
    message because its source object was odd is worse than an unstamped row."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    reached = []

    async def _handle(event):
        reached.append(True)
        return "ok"

    runner._handle_message = _handle
    handler = GatewayRunner._make_profile_message_handler(runner, "p")
    assert asyncio.run(handler(SimpleNamespace(source=None))) == "ok"
    assert reached == [True]
    assert inspect.iscoroutinefunction(handler)


def test_stamped_gateway_profile_survives_sqlite_create_and_reset(tmp_path, monkeypatch):
    """An event stamp is not enough: Nova's tools read the persisted row."""
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = store._db
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="control-chat",
        chat_type="group",
        user_id="owner",
        thread_id="control-topic",
        profile="nova-teen-club",
    )

    first = store.get_or_create_session(source)
    assert db.get_session(first.session_id)["profile_name"] == "nova-teen-club"

    reset = store.reset_session(first.session_key)
    assert reset is not None
    assert db.get_session(reset.session_id)["profile_name"] == "nova-teen-club"
    db.close()


def test_primary_handler_stamps_the_row_seen_by_nova_tools(tmp_path, monkeypatch):
    import hermes_state
    from gateway.run import GatewayRunner

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = object.__new__(GatewayRunner)

    async def _handle(event):
        return store.get_or_create_session(event.source)

    runner._handle_message = _handle
    event = SimpleNamespace(source=SessionSource(
        platform=Platform.TELEGRAM, chat_id="control-chat", user_id="owner",
    ))
    handler = GatewayRunner._make_profile_message_handler(runner, "nova-teen-club")
    entry = asyncio.run(handler(event))

    assert event.source.profile == "nova-teen-club"
    assert store._db.get_session(entry.session_id)["profile_name"] == "nova-teen-club"
    store._db.close()


def test_unstamped_or_different_profile_is_not_falsely_stamped(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("untrusted", source="telegram")
    db.create_session("untrusted", source="telegram", profile_name="nova-teen-club")
    db.create_session("foreign", source="telegram", profile_name="other-profile")
    db.create_session("foreign", source="telegram", profile_name="nova-teen-club")

    assert db.get_session("untrusted")["profile_name"] is None
    assert db.get_session("foreign")["profile_name"] == "other-profile"
    db.close()


def test_quarantine_preserves_history_and_cannot_be_resumed(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="control",
                           user_id="owner", profile="nova-teen-club")
    old = store.get_or_create_session(source)
    store._db.append_message(old.session_id, role="user", content="synthetic history")
    store._db.end_session(old.session_id, "nova_profile_quarantined")
    fresh = store.get_or_create_session(source)
    assert fresh.session_id != old.session_id
    assert store._db.get_session(fresh.session_id)["profile_name"] == "nova-teen-club"
    assert store.switch_session(fresh.session_key, old.session_id) is None
    with pytest.raises(ValueError, match="cannot be resumed"):
        store._db.reopen_session(old.session_id)
    assert store._db.get_messages(old.session_id)[0]["content"] == "synthetic history"
    store._db.close()


def test_branch_command_preserves_profile_and_peer(tmp_path, monkeypatch):
    import hermes_state
    from hermes_state import AsyncSessionDB
    from gateway.session import AsyncSessionStore
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from gateway.platforms.base import MessageEvent

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="control",
                           user_id="owner", chat_type="group", thread_id="topic",
                           profile="nova-teen-club")
    old = store.get_or_create_session(source)
    store._db.append_message(old.session_id, role="user", content="synthetic history")
    runner = SimpleNamespace(
        _session_db=AsyncSessionDB(store._db), async_session_store=AsyncSessionStore(store),
        _session_key_for_source=lambda src: old.session_key, config={},
        _clear_session_boundary_security_state=lambda key: None,
        _evict_cached_agent=lambda key: None,
    )
    asyncio.run(GatewaySlashCommandsMixin._handle_branch_command(
        runner, MessageEvent(text="/branch example", source=source)))
    fresh = store.get_or_create_session(source)
    assert fresh.session_id != old.session_id
    row = store._db.get_session(fresh.session_id)
    assert row["profile_name"] == "nova-teen-club"
    assert (row["user_id"], row["chat_id"], row["thread_id"]) == ("owner", "control", "topic")
    store._db.close()
