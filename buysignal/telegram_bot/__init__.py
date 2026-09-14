"""Long-polling Telegram bot: per-signal + aggregate commands, plus threshold-crossing pushes.

Three modules, in dependency order: `render` turns values into Telegram HTML and does nothing
else; `transport` owns the credentials and the wire; `app` holds the state, decides what to
say and wires the other two together. The entrypoint only needs the four names below.
"""

from buysignal.telegram_bot.app import handle_tick, start
from buysignal.telegram_bot.transport import notify_started, notify_stopped

__all__ = ["handle_tick", "notify_started", "notify_stopped", "start"]
