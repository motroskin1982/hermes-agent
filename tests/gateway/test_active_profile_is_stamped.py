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
