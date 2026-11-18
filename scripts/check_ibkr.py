"""Download the IBKR Flex statement once and say whether the bot can read it; stores nothing.

On the server, before a deploy, through the image already running there (the token is locked to
the server's IP and lives in its .env):

    cat buysignal/broker/flex.py scripts/check_ibkr.py | \\
      $EC2 'cd ~/buy-signal && docker compose run --rm --no-deps -T --entrypoint python bot -'

In the repo, against a saved statement: python -m scripts.check_ibkr --file statement.xml
Prints counts, symbols, currencies and dates; no balances, prices or account number.
"""
import os
import sys

try:
    from buysignal.broker.flex import FlexError, fetch, parse
except ImportError:  # piped after flex.py into an image that predates it
    pass


def _summary(statement) -> list[str]:
    lines = [
        f"statement     {statement.from_date} to {statement.to_date}, generated {statement.generated_at:%Y-%m-%d %H:%M}",
        f"base currency {statement.base_currency}",
        f"positions     {len(statement.positions)}: " + ", ".join(f"{p.symbol} ({p.exchange}, {p.currency})" for p in statement.positions),
        f"cash          currencies {', '.join(statement.cash) or 'none'}",
        f"trades        {len(statement.trades)}, buys {sum(t.quantity > 0 for t in statement.trades)}"
        + (f", {statement.trades[0].trade_date} to {statement.trades[-1].trade_date}" if statement.trades else ""),
        f"daily values  {len(statement.nav)}" + (f", last {statement.nav[-1][0]}" if statement.nav else " (Net Asset Value section empty?)"),
        f"deposits      {len(statement.deposits)} days with deposits or withdrawals",
    ]
    if not statement.cash:
        lines.append("warning       no cash rows: tick Currency, Level of Detail and Ending Cash in Cash Report")
    if len(statement.nav) < 2:
        lines.append("warning       fewer than two daily values: the Overview and Returns tabs stay empty")
    return lines


def main(argv) -> int:
    if "--file" in argv:
        payload = open(argv[argv.index("--file") + 1], "rb").read()
    else:
        token, query = os.environ.get("IBKR_FLEX_TOKEN"), os.environ.get("IBKR_FLEX_QUERY_ID")
        if not token or not query:
            print("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID are not set in this environment")
            return 2
        try:
            payload = fetch(token, query)
        except FlexError as exc:
            print(f"download failed: {exc}")
            return 1
    try:
        statement = parse(payload)
    except (ValueError, FlexError) as exc:
        print(f"statement not usable: {exc}")
        return 1
    print("\n".join(_summary(statement)))
    print("ok: the bot can read this statement")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
