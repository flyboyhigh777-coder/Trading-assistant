"""Render signals as Telegram-friendly HTML."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from .models import Direction, Signal

ARROW = {Direction.CALL: "🟢 CALL", Direction.PUT: "🔴 PUT", Direction.NONE: "⚪ NO TRADE"}

RISK_FOOTER = (
    "<i>Signals are indicator output, not advice. "
    "Short-expiry options have negative expectancy at most brokers' payouts — "
    "size accordingly.</i>"
)


def confidence_bar(confidence: float, slots: int = 10) -> str:
    filled = round(min(max(confidence, 0.0), 1.0) * slots)
    return "█" * filled + "░" * (slots - filled)


def format_price(price: float) -> str:
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.4f}"
    return f"{price:.8f}".rstrip("0")


def format_signal(signal: Signal, verbose: bool = True) -> str:
    head = (
        f"<b>{ARROW[signal.direction]}</b>  "
        f"<code>{escape(signal.symbol)}</code> · {escape(signal.timeframe)}"
    )
    lines = [
        head,
        f"price <b>{format_price(signal.price)}</b>",
        (
            f"confidence <code>{confidence_bar(signal.confidence)}</code> "
            f"{signal.confidence:.0%}"
        ),
    ]
    if signal.actionable:
        lines.append(f"suggested expiry <b>{signal.expiry_minutes} min</b>")
    lines.append(
        f"bar closed {signal.bar_time.strftime('%Y-%m-%d %H:%M UTC')}"
    )

    if verbose:
        voted = [v for v in signal.votes if v.direction is not Direction.NONE]
        if voted:
            lines.append("")
            lines.append("<b>Strategies</b>")
            for vote in sorted(voted, key=lambda v: -v.strength):
                mark = "🟢" if vote.direction is Direction.CALL else "🔴"
                lines.append(
                    f"{mark} <b>{escape(vote.strategy)}</b> {vote.strength:.0%} — "
                    f"{escape(vote.reason)}"
                )
        quiet = [v for v in signal.votes if v.direction is Direction.NONE]
        if quiet and not signal.actionable:
            lines.append("")
            lines.append("<b>Why no trade</b>")
            for vote in quiet:
                lines.append(f"• {escape(vote.strategy)}: {escape(vote.reason)}")

    if signal.notes:
        lines.append("")
        for note in signal.notes:
            lines.append(f"⚠️ {escape(note)}")

    return "\n".join(lines)


def format_scan(
    signals: Sequence[Signal],
    errors: dict[str, str] | None = None,
    limit: int = 10,
) -> str:
    actionable = [s for s in signals if s.actionable]
    lines = [f"<b>Scan</b> — {len(actionable)} of {len(signals)} symbols actionable", ""]

    if actionable:
        for signal in actionable[:limit]:
            lines.append(
                f"{ARROW[signal.direction]}  <code>{escape(signal.symbol)}</code> "
                f"{escape(signal.timeframe)} · {signal.confidence:.0%} · "
                f"{signal.expiry_minutes}m · {format_price(signal.price)}"
            )
    else:
        lines.append("No setup cleared the confidence threshold. Nothing to do.")
        best = max(signals, key=lambda s: s.confidence, default=None)
        if best is not None and best.confidence > 0:
            lines.append(
                f"Closest: <code>{escape(best.symbol)}</code> at {best.confidence:.0%}."
            )

    if errors:
        lines.append("")
        for symbol, message in list(errors.items())[:5]:
            lines.append(f"⚠️ {escape(symbol)}: {escape(message)}")

    lines.append("")
    lines.append(RISK_FOOTER)
    return "\n".join(lines)


def format_signal_plain(signal: Signal) -> str:
    """One-line rendering for the CLI."""
    name = {Direction.CALL: "CALL", Direction.PUT: "PUT", Direction.NONE: "----"}
    return (
        f"{signal.symbol:<12} {signal.timeframe:>4}  {name[signal.direction]:<5} "
        f"{signal.confidence:>5.0%}  {format_price(signal.price):>14}  "
        f"{signal.expiry_minutes:>3}m  "
        + ", ".join(
            f"{v.strategy}:{v.direction.value.lower()}"
            for v in signal.votes
            if v.direction is not Direction.NONE
        )
    )
