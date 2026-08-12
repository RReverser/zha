"""Event tests for ZHA."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

from zha.event import EventBase, EventListener


class EventGenerator(EventBase):
    """Event generator for testing."""


def test_event_base_unsubs():
    """Test event base class."""
    event = EventGenerator()
    assert not event._listeners
    assert not event._global_listeners

    callback = MagicMock()

    unsub = event.on_event("test", callback)
    assert event._listeners == {
        "test": [EventListener(callback=callback, with_context=False)]
    }
    unsub()
    assert event._listeners == {"test": []}

    unsub = event.on_all_events(callback)
    assert event._global_listeners == [
        EventListener(callback=callback, with_context=False)
    ]
    unsub()
    assert not event._global_listeners

    unsub = event.once("test", callback)
    assert "test" in event._listeners
    assert len(event._listeners["test"]) == 1
    unsub()
    assert event._listeners == {"test": []}


def test_event_base_emit():
    """Test event base class."""
    event = EventGenerator()
    assert not event._listeners
    assert not event._global_listeners

    callback = MagicMock()

    event.once("test", callback)
    event.emit("test")
    assert callback.called

    callback.reset_mock()
    event.emit("test")
    assert not callback.called

    unsub = event.on_event("test", callback)
    event.emit("test")
    assert callback.called
    unsub()

    callback.reset_mock()
    unsub = event.on_all_events(callback)
    event.emit("test")
    assert callback.called
    unsub()

    assert "test" in event._listeners
    assert event._listeners == {"test": []}
    assert not event._global_listeners


def test_event_base_emit_data():
    """Test event base class."""
    event = EventGenerator()
    assert not event._listeners
    assert not event._global_listeners

    callback = MagicMock()

    event.once("test", callback)
    event.emit("test", "data")
    assert callback.called
    assert callback.call_args[0] == ("data",)

    callback.reset_mock()
    event.emit("test", "data")
    assert not callback.called

    unsub = event.on_event("test", callback)
    event.emit("test", "data")
    assert callback.called
    assert callback.call_args[0] == ("data",)
    unsub()

    callback.reset_mock()
    unsub = event.on_all_events(callback)
    event.emit("test", "data")
    assert callback.called
    assert callback.call_args[0] == ("data",)
    unsub()

    assert "test" in event._listeners
    assert event._listeners == {"test": []}
    assert not event._global_listeners


async def test_event_base_emit_coro():
    """Test event base class."""
    event = EventGenerator()
    assert not event._listeners
    assert not event._global_listeners

    callback = AsyncMock()

    event.once("test", callback)
    event.emit("test", "data")

    await asyncio.gather(*event._event_tasks)

    assert callback.await_count == 1
    assert callback.mock_calls == [call("data")]
    assert not event._event_tasks

    callback.reset_mock()

    unsub = event.on_event("test", callback)
    event.emit("test", "data")

    await asyncio.gather(*event._event_tasks)

    assert callback.await_count == 1
    assert callback.mock_calls == [call("data")]
    unsub()
    assert not event._event_tasks

    callback.reset_mock()

    unsub = event.on_all_events(callback)
    event.emit("test", "data")

    await asyncio.gather(*event._event_tasks)

    assert callback.await_count == 1
    assert callback.mock_calls == [call("data")]
    unsub()
    assert not event._event_tasks


async def test_event_emit_with_context():
    """Test event emitting with context."""

    event = EventGenerator()
    async_callback = AsyncMock()
    sync_callback = MagicMock()

    event.once("test", sync_callback, with_context=True)
    event.once("test", async_callback, with_context=True)
    event.emit("test", "data")

    await asyncio.gather(*event._event_tasks)

    sync_callback.assert_called_once_with("test", "data")
    async_callback.assert_awaited_once_with("test", "data")
