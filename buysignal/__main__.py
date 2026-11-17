"""Run the hourly tick loop and the Telegram bot."""

import logging
import signal

from buysignal import logs, telegram_bot
from buysignal.schedule.runner import refresh_macro, run_forever

log = logging.getLogger(__name__)


def _handle_shutdown_signal(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    logs.configure()
    # SIGTERM (docker stop) takes the same shutdown path as Ctrl+C.
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    telegram_bot.start()
    telegram_bot.notify_started()
    try:
        # Seed the caches now; the journal skips what already ran today.
        refresh_macro()
    except Exception as exc:
        log.error("startup refresh_macro failed (%s: %s); continuing.", type(exc).__name__, exc)
    try:
        run_forever(on_tick=telegram_bot.handle_tick)
    finally:
        telegram_bot.stop()
        telegram_bot.notify_stopped()
