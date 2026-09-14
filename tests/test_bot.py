"""Tests for buysignal/__main__.py, the entrypoint."""

import pytest

from buysignal import __main__ as entrypoint


def test_handle_shutdown_signal_raises_keyboard_interrupt():
    """SIGTERM (docker stop) takes the same clean-shutdown path as Ctrl+C."""
    with pytest.raises(KeyboardInterrupt):
        entrypoint._handle_shutdown_signal(None, None)
