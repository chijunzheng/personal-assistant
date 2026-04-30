"""Telegram polling bridge.

A thin wrapper over ``python-telegram-bot ~= 21.x`` that:

  1. Builds a polling Application bound to ``TELEGRAM_BOT_TOKEN``
  2. Registers a single ``MessageHandler`` for text messages
  3. Hands each message off to an injected callback (the orchestrator)
  4. Sends the callback's reply back over the same chat

The handler is exposed as a free function so it can be unit-tested
without spinning up a real Telegram Application; the polling loop itself
is verified via the live smoke test described in issue #1.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from kernel.orchestrator import Orchestrator

__all__ = [
    "MessageReplyFn",
    "build_application",
    "make_message_handler",
    "run_polling_loop",
]


logger = logging.getLogger(__name__)


# Async callback signature: takes incoming text, returns reply text.
MessageReplyFn = Callable[[str], Awaitable[str]]


def make_message_handler(reply_fn: MessageReplyFn):
    """Build a python-telegram-bot ``handler`` that delegates to ``reply_fn``.

    The returned coroutine has the signature required by
    ``MessageHandler``: ``(update, context) -> None``.
    """

    async def _handler(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        try:
            reply_text = await reply_fn(message.text)
        except Exception:  # noqa: BLE001
            logger.exception("orchestrator failed handling Telegram message")
            reply_text = "Sorry — something went wrong handling that message."
        await message.reply_text(reply_text)

    return _handler


def _orchestrator_to_async(orchestrator: Orchestrator) -> MessageReplyFn:
    """Adapt the synchronous Orchestrator API to an async callback for PTB."""

    async def _reply(text: str) -> str:
        return orchestrator.handle_message(text).text

    return _reply


def build_application(
    *,
    orchestrator: Orchestrator,
    token: Optional[str] = None,
) -> Application:
    """Construct a ``python-telegram-bot`` Application wired to the orchestrator."""
    bot_token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment")

    application = ApplicationBuilder().token(bot_token).build()
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            make_message_handler(_orchestrator_to_async(orchestrator)),
        )
    )
    return application


def run_polling_loop(
    *,
    orchestrator: Orchestrator,
    token: Optional[str] = None,
) -> None:
    """Acquire the single-instance lock and run the polling loop forever."""
    orchestrator.start()
    try:
        application = build_application(orchestrator=orchestrator, token=token)
        application.run_polling()
    finally:
        orchestrator.stop()
