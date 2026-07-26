"""Entrypoint: the hourly tick loop plus the Telegram bot (/signal, /status, threshold pushes)."""

import telegram_bot
from runner import run_forever

if __name__ == "__main__":
    telegram_bot.start()
    run_forever(on_tick=telegram_bot.handle_tick)
