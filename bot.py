"""Entrypoint: the hourly tick loop plus the Telegram bot (commands, threshold pushes)."""

import signal

import telegram_bot
from runner import refresh_macro, run_forever


def _handle_shutdown_signal(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    # Translate SIGTERM (docker stop, systemd stop, kill) into the same clean-shutdown path
    # run_forever() already handles for Ctrl+C (SIGINT/KeyboardInterrupt).
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    telegram_bot.start()
    telegram_bot.notify_started()
    try:
        # Seed fed_rate/sectors/margin_debt on startup instead of waiting for the next
        # pre-market/after-hours tick; matters most on a fresh deploy with empty data/, but
        # cheap on every restart since each collector's should_refresh() skips it if already fresh.
        refresh_macro()
    except Exception as exc:
        print(f"startup refresh_macro failed ({type(exc).__name__}: {exc}); continuing.")
    try:
        run_forever(on_tick=telegram_bot.handle_tick)
    finally:
        telegram_bot.notify_stopped()
