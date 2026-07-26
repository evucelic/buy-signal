"""Entrypoint: the hourly tick loop plus the Telegram bot (commands, threshold pushes)."""

import signal

import telegram_bot
from runner import run_forever


def _handle_shutdown_signal(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    # Translate SIGTERM (docker stop, systemd stop, kill) into the same clean-shutdown path
    # run_forever() already handles for Ctrl+C (SIGINT/KeyboardInterrupt).
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    telegram_bot.start()
    telegram_bot.notify_started()
    try:
        run_forever(on_tick=telegram_bot.handle_tick)
    finally:
        telegram_bot.notify_stopped()
