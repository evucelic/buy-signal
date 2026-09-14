"""Entrypoint: the hourly tick loop plus the Telegram bot (commands, threshold pushes)."""

import logging
import signal

import logs
import telegram_bot
from runner import refresh_macro, run_forever

log = logging.getLogger(__name__)


def _handle_shutdown_signal(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    logs.configure()
    # Translate SIGTERM (docker stop, systemd stop, kill) into the same clean-shutdown path
    # run_forever() already handles for Ctrl+C (SIGINT/KeyboardInterrupt).
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    telegram_bot.start()
    telegram_bot.notify_started()
    try:
        # Seed the macro caches on startup instead of waiting for the next pre-market/
        # after-hours tick; matters most on a fresh deploy with empty data/, but cheap on
        # every restart since the refresh journal (persisted in data/, so it survives the
        # redeploy) skips whatever already ran today.
        refresh_macro()
    except Exception as exc:
        log.error("startup refresh_macro failed (%s: %s); continuing.", type(exc).__name__, exc)
    try:
        run_forever(on_tick=telegram_bot.handle_tick)
    finally:
        telegram_bot.notify_stopped()
