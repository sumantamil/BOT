"""
Theta Decay Clock

Shows exactly how much money your options are losing per hour, per minute,
and per second to time decay (theta). Makes the invisible cost of holding
options painfully visible.

Chat commands:
    theta <premium> <days_to_expiry>   - Show decay for a position
    theta 150 3                         - Rs.150 premium, 3 days to expiry
"""

import math
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

from bot.index_config import IndexConfig, NIFTY


@dataclass
class ThetaDecayReport:
    premium: float
    days_to_expiry: float
    estimated_theta_per_day: float
    theta_per_hour: float
    theta_per_minute: float
    decay_by_tomorrow: float
    decay_by_expiry_pct: float
    time_value_remaining_pct: float
    decay_acceleration: str
    urgency: str


class ThetaDecayClock:
    """
    Calculates and visualizes option time decay.

    Theta decay is NOT linear -- it accelerates exponentially in the last
    5 days before expiry. This clock makes that visible so you know exactly
    when to exit positions to avoid the "theta bleed."
    """

    def __init__(self):
        self._index: IndexConfig = NIFTY

    def set_index(self, idx: IndexConfig):
        self._index = idx

    @property
    def _lot_size(self) -> int:
        return self._index.lot_size

    def calculate(
        self,
        premium: float,
        days_to_expiry: float,
        is_atm: bool = True,
    ) -> ThetaDecayReport:
        """
        Calculate theta decay metrics.

        Theta for ATM options approximately:
            theta = -S * sigma * N'(d1) / (2 * sqrt(T))
        Simplified: theta ~ premium * (1 / (2 * sqrt(days)))
        """
        if days_to_expiry <= 0:
            days_to_expiry = 0.1

        # Estimate time value (for ATM, most of premium is time value)
        time_value_pct = 1.0 if is_atm else 0.7  # ATM is ~100% time value
        time_value = premium * time_value_pct

        # Theta approximation using square root decay
        # Theta accelerates as sqrt(T) decreases
        sqrt_t = math.sqrt(days_to_expiry / 365)
        if sqrt_t > 0:
            theta_per_day = time_value / (2 * math.sqrt(days_to_expiry))
        else:
            theta_per_day = time_value

        theta_per_hour = theta_per_day / 6.25  # 6.25 trading hours per day
        theta_per_minute = theta_per_hour / 60

        # Premium tomorrow (one day less)
        if days_to_expiry > 1:
            tomorrow_factor = math.sqrt((days_to_expiry - 1) / days_to_expiry)
            decay_by_tomorrow = time_value * (1 - tomorrow_factor)
        else:
            decay_by_tomorrow = time_value * 0.5  # Last day loses ~50%

        # Decay acceleration zone
        if days_to_expiry <= 1:
            acceleration = "EXTREME -- losing value every minute"
            urgency = "EXIT NOW unless deeply ITM"
        elif days_to_expiry <= 3:
            acceleration = "VERY HIGH -- theta is eating 30-50% of premium"
            urgency = "Exit by today's close or risk major decay overnight"
        elif days_to_expiry <= 5:
            acceleration = "HIGH -- entering the theta danger zone"
            urgency = "Plan your exit. Don't hold through the last 2 days."
        elif days_to_expiry <= 10:
            acceleration = "MODERATE -- decay is noticeable but manageable"
            urgency = "You have time but don't get complacent."
        else:
            acceleration = "LOW -- time is on your side (relatively)"
            urgency = "Decay is minimal. Focus on direction, not theta."

        time_value_remaining = 100 * (1 - (theta_per_day * days_to_expiry) / max(premium, 0.01))
        time_value_remaining = max(0, min(100, time_value_remaining))

        return ThetaDecayReport(
            premium=premium,
            days_to_expiry=days_to_expiry,
            estimated_theta_per_day=round(theta_per_day, 2),
            theta_per_hour=round(theta_per_hour, 2),
            theta_per_minute=round(theta_per_minute, 4),
            decay_by_tomorrow=round(decay_by_tomorrow, 2),
            decay_by_expiry_pct=round(100 - time_value_remaining, 1),
            time_value_remaining_pct=round(time_value_remaining, 1),
            decay_acceleration=acceleration,
            urgency=urgency,
        )

    def format_report(self, r: ThetaDecayReport) -> str:
        # Visual decay bar
        filled = int(r.decay_by_expiry_pct / 5)
        remaining = 20 - filled
        bar = "#" * filled + "." * remaining

        # Decay schedule for next few days
        schedule_lines = []
        premium = r.premium
        for d in range(int(r.days_to_expiry), max(0, int(r.days_to_expiry) - 5), -1):
            if d <= 0:
                break
            daily_theta = premium / (2 * math.sqrt(max(d, 0.1)))
            premium_after = max(0, premium - daily_theta)
            schedule_lines.append(
                f"  Day {d:>2}: Rs.{premium:.1f}  ->  Rs.{premium_after:.1f}  (lose Rs.{daily_theta:.1f})"
            )
            premium = premium_after

        return f"""
================================================================
  THETA DECAY CLOCK
================================================================

  Premium:             Rs.{r.premium:.2f}
  Days to Expiry:      {r.days_to_expiry:.1f}
  
----------------------------------------------------------------
  YOUR MONEY IS BLEEDING:
----------------------------------------------------------------
  Per Day:             Rs.{r.estimated_theta_per_day:.2f}
  Per Hour:            Rs.{r.theta_per_hour:.2f}
  Per Minute:          Rs.{r.theta_per_minute:.4f}
  
  Per LOT ({self._lot_size} qty):
  - Per Day:           Rs.{r.estimated_theta_per_day * self._lot_size:.0f}
  - Per Hour:          Rs.{r.theta_per_hour * self._lot_size:.0f}
  - Tomorrow's Loss:   Rs.{r.decay_by_tomorrow * self._lot_size:.0f}

----------------------------------------------------------------
  DECAY VISUALIZATION
----------------------------------------------------------------
  [{"#" * int((100 - r.time_value_remaining_pct) / 5)}{"." * int(r.time_value_remaining_pct / 5)}]
   Decayed: {100 - r.time_value_remaining_pct:.0f}%     Remaining: {r.time_value_remaining_pct:.0f}%

----------------------------------------------------------------
  DECAY SCHEDULE (next days)
----------------------------------------------------------------
{chr(10).join(schedule_lines) if schedule_lines else "  Expiry imminent!"}

----------------------------------------------------------------
  URGENCY LEVEL
----------------------------------------------------------------
  Acceleration: {r.decay_acceleration}
  
  >>> {r.urgency} <<<

================================================================
  REMEMBER: Theta decay is NOT your friend when buying options.
  The last 3 days before expiry destroy 40-60% of time value.
  If your directional bet hasn't worked, EXIT before theta eats
  your capital.
================================================================
"""


theta_clock = ThetaDecayClock()
