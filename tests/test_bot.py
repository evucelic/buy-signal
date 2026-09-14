"""Tests for bot.py, the entrypoint."""

import pytest

import bot


def test_handle_shutdown_signal_raises_keyboard_interrupt():
    with pytest.raises(KeyboardInterrupt):
        bot._handle_shutdown_signal(None, None)
