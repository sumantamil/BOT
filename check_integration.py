"""One-shot integration health check — safe to delete after use."""
import sys
sys.path.insert(0, ".")

from bot.ai_gateway import get_ai_gateway
from bot.ai_validation_gateway import get_ai_gateway as gw2
from config import settings

results = []

def check(label, cond, detail=""):
    ok = bool(cond)
    results.append((label, ok, detail))
    return ok

# 1. Singleton identity
check("singleton (both imports same obj)", get_ai_gateway() is gw2())

# 2. Config flags
check("AI_ENABLED=true",                    settings.ai.enabled)
check("AI_USE_AI_SIGNAL_VALIDATION=true",   settings.ai.use_ai_signal_validation)

# 3. AI service live
gw = get_ai_gateway()
check("ai_service present",  gw.ai_service is not None)
check("ai_service.enabled",  getattr(gw.ai_service, "enabled", False))

# 4. engine.py source checks
src = open("bot/engine.py", encoding="utf-8").read()

check("Gap start = 9:20 AM",  "minute >= 20" in src)

filter_blocks = [
    "GAP_ai_gateway",
    "ORB_ai_gateway",
    "VWAP_ai_gateway",
    "EOD_ai_gateway",
    "LATEDAY_ai_gateway",
]
for b in filter_blocks:
    check(f"filter block {b}", b in src)

approved_key = "_gw_res['approved']"
count = src.count(approved_key)
check(f"approved checks = 6  (found {count})", count == 6)

ai_conf_vars = ["_ai_conf_for_tg", "_ai_conf_gap", "_ai_conf_eod", "_ai_conf_lateday"]
for v in ai_conf_vars:
    check(f"confidence var {v}", v in src)

# ── Report ─────────────────────────────────────────────────────────────────────
passed  = sum(1 for _, ok, _ in results if ok)
failed  = sum(1 for _, ok, _ in results if not ok)
width   = max(len(label) for label, _, _ in results)

print()
print("=" * (width + 12))
print("  AI INTEGRATION HEALTH CHECK")
print("=" * (width + 12))
for label, ok, detail in results:
    icon = "✅" if ok else "❌"
    print(f"  {icon}  {label}")
print("-" * (width + 12))
print(f"  {passed}/{passed+failed} checks passed")
print("=" * (width + 12))

if failed:
    sys.exit(1)
