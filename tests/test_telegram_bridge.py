"""Tests for ``kernel.telegram_bridge`` — the message-dispatch layer.

The polling loop itself can only be exercised live (``run_polling`` opens
real network connections). These tests verify the behavior of
``make_message_handler``, which is the entire surface that touches
incoming/outgoing Telegram messages.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kernel.telegram_bridge import make_message_handler


def _fake_update_with_text(text: str | None) -> SimpleNamespace:
    """Mimic the subset of ``telegram.Update`` the handler reads."""
    message = SimpleNamespace(text=text, reply_text=AsyncMock(return_value=None))
    return SimpleNamespace(effective_message=message)


def test_message_handler_sends_reply_from_callback() -> None:
    """The handler routes ``message.text`` through the callback and sends back the result."""

    async def reply_fn(text: str) -> str:
        return f"got: {text}"

    handler = make_message_handler(reply_fn)
    update = _fake_update_with_text("hello")

    asyncio.run(handler(update, None))

    update.effective_message.reply_text.assert_awaited_once_with("got: hello")


def test_message_handler_skips_messages_without_text() -> None:
    """A media-only update (no text) does not invoke the callback or call reply_text."""
    callback = AsyncMock(return_value="should not be called")
    handler = make_message_handler(callback)
    update = _fake_update_with_text(None)

    asyncio.run(handler(update, None))

    callback.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()


def test_message_handler_falls_back_to_friendly_error_on_exception() -> None:
    """If the callback raises, the user still gets a reply (not silence)."""

    async def boom(_text: str) -> str:
        raise RuntimeError("kaboom")

    handler = make_message_handler(boom)
    update = _fake_update_with_text("hi")

    asyncio.run(handler(update, None))

    update.effective_message.reply_text.assert_awaited_once()
    sent = update.effective_message.reply_text.call_args.args[0]
    assert "wrong" in sent.lower()
