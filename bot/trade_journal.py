"""
Trade Journal / Session Tracker

Automatically logs every analysis, signal, and trade decision into a
structured JSON journal. After market close, you can review exactly
what happened, why signals were generated, and whether decisions were correct.

Chat commands:
    journal         - Show today's journal summary
    journal review  - Detailed review with lessons
    journal export  - Export to CSV
"""

import json
import os
from datetime import datetime, date
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any
from loguru import logger


@dataclass
class JournalEntry:
    timestamp: str
    event_type: str  # ANALYSIS, SIGNAL, TRADE, REGIME, USER_CMD, ALERT
    direction: str   # BULLISH, BEARISH, NEUTRAL, N/A
    details: Dict[str, Any] = field(default_factory=dict)
    outcome: str = ""  # filled in later for trades


class TradeJournal:
    """
    Persistent trade journal that records everything the bot does during a session.
    Saved as JSON per day for post-market review.
    """

    def __init__(self, journal_dir: str = None):
        if journal_dir is None:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            journal_dir = os.path.join(base, "journals")

        self._dir = journal_dir
        os.makedirs(self._dir, exist_ok=True)
        self._entries: List[JournalEntry] = []
        self._today = date.today().isoformat()
        self._load_today()

    def _filepath(self, day: str = None) -> str:
        day = day or self._today
        return os.path.join(self._dir, f"journal_{day}.json")

    def _load_today(self):
        path = self._filepath()
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                self._entries = [JournalEntry(**e) for e in data]
            except Exception:
                self._entries = []

    def _save(self):
        path = self._filepath()
        with open(path, "w") as f:
            json.dump([asdict(e) for e in self._entries], f, indent=2, default=str)

    def _ensure_today(self):
        today = date.today().isoformat()
        if today != self._today:
            self._today = today
            self._entries = []

    def log_analysis(self, trend: str, strength: float, price: float, rsi: float, **extra):
        self._ensure_today()
        self._entries.append(JournalEntry(
            timestamp=datetime.now().isoformat(),
            event_type="ANALYSIS",
            direction=trend,
            details={"strength": strength, "price": price, "rsi": rsi, **extra},
        ))
        self._save()

    def log_signal(self, direction: str, strike: int, option_type: str, confidence: float, reason: str):
        self._ensure_today()
        self._entries.append(JournalEntry(
            timestamp=datetime.now().isoformat(),
            event_type="SIGNAL",
            direction=direction,
            details={"strike": strike, "option_type": option_type, "confidence": confidence, "reason": reason},
        ))
        self._save()

    def log_trade(self, action: str, strike: int, option_type: str, premium: float, quantity: int = 25):
        self._ensure_today()
        self._entries.append(JournalEntry(
            timestamp=datetime.now().isoformat(),
            event_type="TRADE",
            direction=option_type,
            details={"action": action, "strike": strike, "premium": premium, "quantity": quantity},
        ))
        self._save()

    def log_regime(self, regime: str, adx: float, should_trade: bool):
        self._ensure_today()
        self._entries.append(JournalEntry(
            timestamp=datetime.now().isoformat(),
            event_type="REGIME",
            direction=regime,
            details={"adx": adx, "should_trade": should_trade},
        ))
        self._save()

    def log_user_command(self, command: str, response_summary: str = ""):
        self._ensure_today()
        self._entries.append(JournalEntry(
            timestamp=datetime.now().isoformat(),
            event_type="USER_CMD",
            direction="N/A",
            details={"command": command, "response": response_summary[:200]},
        ))
        self._save()

    def log_alert(self, message: str):
        self._ensure_today()
        self._entries.append(JournalEntry(
            timestamp=datetime.now().isoformat(),
            event_type="ALERT",
            direction="N/A",
            details={"message": message},
        ))
        self._save()

    def get_summary(self) -> str:
        self._ensure_today()
        if not self._entries:
            return "No journal entries for today. Start the bot to begin logging."

        counts = {}
        for e in self._entries:
            counts[e.event_type] = counts.get(e.event_type, 0) + 1

        analyses = [e for e in self._entries if e.event_type == "ANALYSIS"]
        signals = [e for e in self._entries if e.event_type == "SIGNAL"]
        trades = [e for e in self._entries if e.event_type == "TRADE"]
        cmds = [e for e in self._entries if e.event_type == "USER_CMD"]

        # Direction distribution
        bull = sum(1 for e in analyses if e.direction == "BULLISH")
        bear = sum(1 for e in analyses if e.direction == "BEARISH")
        neut = sum(1 for e in analyses if e.direction == "NEUTRAL")

        # Time range
        first = self._entries[0].timestamp[:19]
        last = self._entries[-1].timestamp[:19]

        lines = [
            f"================================================================",
            f"  TRADE JOURNAL - {self._today}",
            f"================================================================",
            f"",
            f"  Session: {first} to {last}",
            f"  Total Events: {len(self._entries)}",
            f"",
            f"  EVENT COUNTS:",
        ]

        for event_type, count in sorted(counts.items()):
            lines.append(f"    {event_type:<15} {count:>4}")

        lines.extend([
            f"",
            f"  DIRECTION BIAS (from analyses):",
            f"    Bullish: {bull}  |  Bearish: {bear}  |  Neutral: {neut}",
        ])

        if signals:
            lines.append(f"")
            lines.append(f"  SIGNALS GENERATED:")
            for s in signals[-5:]:
                lines.append(f"    {s.timestamp[11:19]}  {s.details.get('option_type', '?')} "
                           f"{s.details.get('strike', '?')}  Conf: {s.details.get('confidence', 0)}%")

        if trades:
            lines.append(f"")
            lines.append(f"  TRADES EXECUTED:")
            for t in trades:
                lines.append(f"    {t.timestamp[11:19]}  {t.details.get('action', '?')} "
                           f"{t.details.get('option_type', '?')} {t.details.get('strike', '?')} "
                           f"@ Rs.{t.details.get('premium', 0):.2f}")

        if cmds:
            lines.append(f"")
            lines.append(f"  USER COMMANDS: {len(cmds)}")
            for c in cmds[-5:]:
                lines.append(f"    {c.timestamp[11:19]}  >> {c.details.get('command', '?')[:50]}")

        lines.extend([
            f"",
            f"================================================================",
            f"  Journal saved to: {self._filepath()}",
            f"================================================================",
        ])

        return "\n".join(lines)

    def get_review(self) -> str:
        """Detailed post-market review with lessons."""
        self._ensure_today()
        if not self._entries:
            return "No entries to review."

        analyses = [e for e in self._entries if e.event_type == "ANALYSIS"]
        signals = [e for e in self._entries if e.event_type == "SIGNAL"]

        # Trend changes
        trend_changes = 0
        prev_dir = None
        for e in analyses:
            if prev_dir and e.direction != prev_dir:
                trend_changes += 1
            prev_dir = e.direction

        # Signal quality
        high_conf = sum(1 for s in signals if s.details.get("confidence", 0) >= 65)
        low_conf = sum(1 for s in signals if s.details.get("confidence", 0) < 50)

        lessons = []
        if trend_changes > len(analyses) * 0.4:
            lessons.append("Market was CHOPPY today -- many trend reversals. Trend-following was difficult.")
        if high_conf == 0 and signals:
            lessons.append("No high-confidence signals today. Staying out was the right call.")
        if trend_changes < 3 and len(analyses) > 5:
            lessons.append("Steady trend today -- ideal conditions for the bot's strategy.")

        return f"""
================================================================
  POST-MARKET REVIEW - {self._today}
================================================================

  Total Analyses:      {len(analyses)}
  Trend Changes:       {trend_changes}
  Signals Generated:   {len(signals)}
  High Conf (>65%):    {high_conf}
  Low Conf (<50%):     {low_conf}
  Choppiness:          {"HIGH" if trend_changes > len(analyses) * 0.4 else "LOW"}

  LESSONS:
{chr(10).join(f"  - {l}" for l in lessons) if lessons else "  - Clean session, no special notes."}

================================================================
"""

    def export_csv(self) -> str:
        """Export today's journal to CSV."""
        self._ensure_today()
        csv_path = self._filepath().replace(".json", ".csv")
        lines = ["timestamp,event_type,direction,details"]
        for e in self._entries:
            details_str = json.dumps(e.details).replace(",", ";")
            lines.append(f"{e.timestamp},{e.event_type},{e.direction},{details_str}")

        with open(csv_path, "w") as f:
            f.write("\n".join(lines))

        return f"Journal exported to {csv_path} ({len(self._entries)} entries)"


trade_journal = TradeJournal()
