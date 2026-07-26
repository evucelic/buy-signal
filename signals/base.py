from dataclasses import dataclass

NONE = "none"
SOFT = "soft"
STRONG = "strong"


@dataclass
class SubSignal:
    name: str             # "vix", "fed_rate", "margin_debt", "sector"
    score: float          # signal-specific numeric diagnostic
    state: str            # free-form label, e.g. none/soft/strong (vix) or cutting/flat/hiking (fed)
    detail: str           # human-readable reason
    passes: bool = False
    table: str | None = None  # optional monospace-ready rendering of detail, for multi-row data


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render headers+rows as a left-aligned, fixed-width table for a monospace context."""
    widths = [max(len(str(cell)) for cell in col) for col in zip(headers, *rows)]

    def fmt(row: list[str]) -> str:
        return "  ".join(str(cell).ljust(w) for cell, w in zip(row, widths))

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(row) for row in rows]
    return "\n".join(lines)
