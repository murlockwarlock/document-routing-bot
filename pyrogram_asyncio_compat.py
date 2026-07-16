"""Compatibility helpers for importing Pyrogram on newer Python versions."""

import asyncio


def ensure_main_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
