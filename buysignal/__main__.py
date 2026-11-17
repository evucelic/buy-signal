"""Entrypoint: the hourly tick loop plus the Telegram bot (commands, threshold pushes)."""

import logging
import signal

from buysignal import logs, telegram_bot
from buysignal.schedule.runner import refresh_macro, run_forever

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
        # Seed the caches now rather than at the next pre-market tick; the journal skips what
        # already ran today.
        refresh_macro()
    except Exception as exc:
        log.error("startup refresh_macro failed (%s: %s); continuing.", type(exc).__name__, exc)
    try:
        run_forever(on_tick=telegram_bot.handle_tick)
    finally:
        telegram_bot.stop()
        telegram_bot.notify_stopped()
