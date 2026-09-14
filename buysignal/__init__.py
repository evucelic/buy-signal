"""The buy-signal bot: collect the macro inputs, score them, report the verdict to Telegram.

Layered bottom-up, and imports only ever point that way:

    config, logs        shared settings and logging setup
    collectors/         one cached CSV per external source
    signals/            one score() per checklist condition, read off those caches
    analysis            the "what to buy" valuation view
    schedule/           when anything runs: market hours, the refresh journal, the tick loop
    telegram_bot/       what reaches the phone

`python -m buysignal` runs the whole thing (see __main__.py); the container does exactly that.
"""
