"""
Profit Tracker — daily P&L recording + investor profit-share calculation.

Files created/managed:
  profit_share_config.json  — investor names, capital amounts, operator fee %
  profit_tracking.csv       — one row per trading day
  Google Sheet              — live sync (optional, configure via .env)

Google Sheets setup (one-time):
  1. Go to https://console.cloud.google.com/ → create project → enable "Google Sheets API"
  2. IAM & Admin → Service Accounts → Create → download JSON key
  3. Save the JSON file as  google_credentials.json  in the bot root folder
  4. Open your Google Sheet → Share → add the service-account email (from JSON) as Editor
  5. Copy the Spreadsheet ID from the URL and add to .env:
       GSHEET_SPREADSHEET_ID=your_sheet_id_here
       GSHEET_CREDENTIALS_PATH=google_credentials.json
  6. pip install gspread google-auth  (already in requirements.txt)

Both files live in the bot root directory (same level as main.py).
"""

import csv
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from loguru import logger

_ROOT = Path(__file__).parent.parent  # d:\...\BOT\
CONFIG_FILE = _ROOT / "profit_share_config.json"
TRACKER_CSV  = _ROOT / "profit_tracking.csv"

_CSV_FIELDS = [
    "date",
    "gross_pnl",
    "charges_est",
    "net_pnl",
    "operator_fee_pct",
    "operator_fee_amt",
    "remaining_pnl",
    "total_trades",
    "wins",
    "losses",
    "win_rate_pct",
    # investor columns are added dynamically as:  <name>_capital, <name>_share_pct, <name>_share_amt
    "notes",
]

# ─────────────────────────── Config helpers ─────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "operator_fee_pct": 25.0,          # % of profit taken as bot-operator fee first
    "brokerage_per_trade": 20.0,       # estimated per-trade brokerage (₹20 × trades)
    "investors": [
        {"name": "Sundhar", "capital": 40000},
        {"name": "Parthi",  "capital": 5000},
    ],
}


def load_config() -> Dict[str, Any]:
    """Load profit-share config, returning defaults if file doesn't exist."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Back-fill any missing keys from defaults
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception as e:
            logger.warning(f"profit_share_config.json load error: {e} — using defaults")
    return dict(DEFAULT_CONFIG)


def save_config(config: Dict[str, Any]) -> None:
    """Persist config to disk."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info("profit_share_config.json saved")
    except Exception as e:
        logger.error(f"profit_share_config.json save error: {e}")


# ─────────────────────────── Core calculation ────────────────────────────────

def _compute_shares(net_pnl: float, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns a dict with:
      operator_fee_pct, operator_fee_amt, remaining_pnl,
      investors: [{name, capital, share_pct, share_amt}, ...]
    """
    investors = config.get("investors", [])
    op_fee_pct = config.get("operator_fee_pct", 25.0)

    total_capital = sum(inv.get("capital", 0) for inv in investors) or 1

    # Operator fee only on profits
    op_fee_amt = round(max(net_pnl, 0) * op_fee_pct / 100, 2)
    remaining   = round(net_pnl - op_fee_amt, 2)

    result_investors = []
    for inv in investors:
        cap  = inv.get("capital", 0)
        pct  = round(cap / total_capital * 100, 4)
        amt  = round(remaining * pct / 100, 2)
        result_investors.append({
            "name":       inv["name"],
            "capital":    cap,
            "share_pct":  pct,
            "share_amt":  amt,
        })

    return {
        "operator_fee_pct": op_fee_pct,
        "operator_fee_amt": op_fee_amt,
        "remaining_pnl":    remaining,
        "investors":        result_investors,
    }


# ─────────────────────────── CSV helpers ────────────────────────────────────

def _build_row(
    trade_date: date,
    gross_pnl: float,
    total_trades: int,
    wins: int,
    losses: int,
    config: Dict[str, Any],
    notes: str = "",
) -> Dict[str, Any]:
    brokerage = config.get("brokerage_per_trade", 20.0) * max(total_trades, 0)
    net_pnl   = round(gross_pnl - brokerage, 2)
    shares    = _compute_shares(net_pnl, config)
    win_rate  = round(wins / total_trades * 100, 1) if total_trades > 0 else 0.0

    row: Dict[str, Any] = {
        "date":              trade_date.isoformat(),
        "gross_pnl":         round(gross_pnl, 2),
        "charges_est":       round(brokerage, 2),
        "net_pnl":           net_pnl,
        "operator_fee_pct":  shares["operator_fee_pct"],
        "operator_fee_amt":  shares["operator_fee_amt"],
        "remaining_pnl":     shares["remaining_pnl"],
        "total_trades":      total_trades,
        "wins":              wins,
        "losses":            losses,
        "win_rate_pct":      win_rate,
        "notes":             notes,
    }
    for inv in shares["investors"]:
        prefix = inv["name"].replace(" ", "_").lower()
        row[f"{prefix}_capital"]   = inv["capital"]
        row[f"{prefix}_share_pct"] = inv["share_pct"]
        row[f"{prefix}_share_amt"] = inv["share_amt"]

    return row


def _all_fieldnames(config: Dict[str, Any]) -> List[str]:
    """Build full csv header: static fields + per-investor columns."""
    investor_cols = []
    for inv in config.get("investors", []):
        prefix = inv["name"].replace(" ", "_").lower()
        investor_cols += [
            f"{prefix}_capital",
            f"{prefix}_share_pct",
            f"{prefix}_share_amt",
        ]
    # Insert investor columns before "notes"
    fields = list(_CSV_FIELDS)
    idx = fields.index("notes")
    return fields[:idx] + investor_cols + fields[idx:]


def _read_csv_rows() -> List[Dict[str, str]]:
    if not TRACKER_CSV.exists():
        return []
    with open(TRACKER_CSV, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _rewrite_csv(rows: List[Dict[str, Any]], config: Dict[str, Any]) -> None:
    """Rewrite the entire CSV (needed when investor columns change)."""
    fieldnames = _all_fieldnames(config)
    with open(TRACKER_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ─────────────────────────── Public API ──────────────────────────────────────

def record_daily_pnl(
    gross_pnl: float,
    total_trades: int,
    wins: int,
    losses: int,
    notes: str = "",
    trade_date: date | None = None,
) -> Dict[str, Any]:
    """
    Append (or update) today's row in profit_tracking.csv.
    If today's row already exists, it is overwritten with fresh data.
    Returns the computed row dict.
    """
    if trade_date is None:
        trade_date = date.today()

    config = load_config()
    row    = _build_row(trade_date, gross_pnl, total_trades, wins, losses, config, notes)

    # Read existing rows, replace today's if present
    existing = _read_csv_rows()
    today_str = trade_date.isoformat()
    updated   = [r for r in existing if r.get("date") != today_str]
    updated.append(row)

    _rewrite_csv(updated, config)
    logger.info(
        f"Profit tracker — {trade_date}: gross ₹{gross_pnl:,.2f} | "
        f"net ₹{row['net_pnl']:,.2f} | trades {total_trades} | "
        f"win rate {row['win_rate_pct']}%"
    )

    # Sync to Google Sheets (no-op if not configured)
    sync_to_google_sheets(updated)

    return row


def get_summary() -> Dict[str, Any]:
    """
    Return all CSV rows + cumulative totals + current config.
    Used by the /api/profit-tracker endpoint.
    """
    config = load_config()
    rows   = _read_csv_rows()

    # Cumulative
    total_net   = sum(float(r.get("net_pnl", 0))   for r in rows)
    total_gross = sum(float(r.get("gross_pnl", 0)) for r in rows)
    total_fees  = sum(float(r.get("operator_fee_amt", 0)) for r in rows)
    total_trades = sum(int(r.get("total_trades", 0)) for r in rows)

    # Per-investor cumulative share
    investor_totals: Dict[str, float] = {}
    for inv in config.get("investors", []):
        prefix = inv["name"].replace(" ", "_").lower()
        key    = f"{prefix}_share_amt"
        investor_totals[inv["name"]] = round(
            sum(float(r.get(key, 0)) for r in rows), 2
        )

    return {
        "config":       config,
        "rows":         rows,
        "cumulative": {
            "gross_pnl":      round(total_gross, 2),
            "net_pnl":        round(total_net, 2),
            "operator_fee":   round(total_fees, 2),
            "total_trades":   total_trades,
            "investor_totals": investor_totals,
        },
    }


def update_config(new_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Update and save config. Rewrites the CSV header if investor list changed
    (e.g. new investor, capital change). Returns the saved config.
    """
    # Validate basics
    if "investors" in new_config:
        for inv in new_config["investors"]:
            if not inv.get("name"):
                raise ValueError("Each investor must have a name")
            if float(inv.get("capital", 0)) < 0:
                raise ValueError("Capital cannot be negative")
    op_fee = float(new_config.get("operator_fee_pct", 25.0))
    if not (0 <= op_fee <= 100):
        raise ValueError("operator_fee_pct must be 0–100")

    save_config(new_config)

    # Rewrite CSV so new investor columns appear (re-compute shares with new capitals)
    existing = _read_csv_rows()
    if existing:
        _rewrite_csv(existing, new_config)
        logger.info("profit_tracking.csv header updated with new investor config")

    # Push updated config + recomputed shares to Google Sheets
    sync_to_google_sheets(existing if existing else _read_csv_rows())

    return new_config


# ─────────────────────────── Google Sheets sync ──────────────────────────────

# Sheet tab names
_TAB_TRACKING = "Profit Tracking"
_TAB_CONFIG   = "Config"
_TAB_SUMMARY  = "Summary"

# Friendly display headers for the Tracking sheet
_DISPLAY_HEADERS = [
    "Date", "Gross P&L (₹)", "Charges Est (₹)", "Net P&L (₹)",
    "Op Fee %", "Op Fee Amt (₹)", "Remaining P&L (₹)",
    "Total Trades", "Wins", "Losses", "Win Rate %",
    # investor columns inserted dynamically
    "Notes",
]


def _get_gsheet_client():
    """
    Return an authorised gspread client, or None if not configured.
    Credentials path and spreadsheet ID come from .env.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.debug("gspread not installed — Google Sheets sync disabled")
        return None, None

    creds_path = os.environ.get("GSHEET_CREDENTIALS_PATH", "google_credentials.json")
    sheet_id   = os.environ.get("GSHEET_SPREADSHEET_ID", "")

    if not sheet_id:
        return None, None  # silently disabled — user hasn't configured it yet

    creds_file = _ROOT / creds_path
    if not creds_file.exists():
        logger.warning(
            f"Google Sheets: credentials file not found at {creds_file}. "
            "Follow the setup instructions in profit_tracker.py"
        )
        return None, None

    try:
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = Credentials.from_service_account_file(str(creds_file), scopes=scopes)
        client = gspread.authorize(creds)
        sheet  = client.open_by_key(sheet_id)
        return client, sheet
    except Exception as e:
        logger.error(f"Google Sheets connection failed: {e}")
        return None, None


def _get_or_create_tab(spreadsheet, title: str, rows: int = 1000, cols: int = 30):
    """Return worksheet by title, creating it if absent."""
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def _display_fieldnames(config: Dict[str, Any]) -> List[str]:
    """Build display header row with per-investor columns."""
    investor_cols = []
    for inv in config.get("investors", []):
        n = inv["name"]
        investor_cols += [
            f"{n} Capital (₹)",
            f"{n} Share %",
            f"{n} Share Amt (₹)",
        ]
    base = list(_DISPLAY_HEADERS)
    idx  = base.index("Notes")
    return base[:idx] + investor_cols + base[idx:]


def _row_to_display(row: Dict[str, Any], config: Dict[str, Any]) -> List[Any]:
    """Convert a row dict to an ordered list matching _display_fieldnames."""
    investors = config.get("investors", [])
    base_keys = [
        "date", "gross_pnl", "charges_est", "net_pnl",
        "operator_fee_pct", "operator_fee_amt", "remaining_pnl",
        "total_trades", "wins", "losses", "win_rate_pct",
    ]
    values = [row.get(k, "") for k in base_keys]

    for inv in investors:
        prefix = inv["name"].replace(" ", "_").lower()
        values += [
            row.get(f"{prefix}_capital",   inv.get("capital", 0)),
            row.get(f"{prefix}_share_pct", ""),
            row.get(f"{prefix}_share_amt", ""),
        ]

    values.append(row.get("notes", ""))
    return values


def _sync_tracking_sheet(spreadsheet, config: Dict[str, Any], all_rows: List[Dict]) -> None:
    """Rewrite the Profit Tracking tab with all rows."""
    ws = _get_or_create_tab(spreadsheet, _TAB_TRACKING)
    headers = _display_fieldnames(config)
    data    = [headers] + [_row_to_display(r, config) for r in all_rows]
    ws.clear()
    ws.update(data, value_input_option="USER_ENTERED")

    # Bold header row
    try:
        ws.format("1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass


def _sync_config_sheet(spreadsheet, config: Dict[str, Any]) -> None:
    """Write current config to the Config tab so investors can see it."""
    ws  = _get_or_create_tab(spreadsheet, _TAB_CONFIG, rows=50, cols=5)
    investors = config.get("investors", [])
    total_cap = sum(inv.get("capital", 0) for inv in investors) or 1

    data = [
        ["Setting", "Value"],
        ["Operator Fee %", config.get("operator_fee_pct", 25.0)],
        ["Brokerage per Trade (₹)", config.get("brokerage_per_trade", 20.0)],
        ["Total Capital (₹)", total_cap],
        [],
        ["Investor", "Capital (₹)", "Capital Share %"],
    ]
    for inv in investors:
        cap = inv.get("capital", 0)
        data.append([inv["name"], cap, round(cap / total_cap * 100, 2)])

    ws.clear()
    ws.update(data, value_input_option="USER_ENTERED")
    try:
        ws.format("A1:B1", {"textFormat": {"bold": True}})
        ws.format("A6:C6", {"textFormat": {"bold": True}})
    except Exception:
        pass


def _sync_summary_sheet(spreadsheet, config: Dict[str, Any], all_rows: List[Dict]) -> None:
    """Write cumulative totals to the Summary tab."""
    ws = _get_or_create_tab(spreadsheet, _TAB_SUMMARY, rows=50, cols=5)

    investors       = config.get("investors", [])
    total_net       = sum(float(r.get("net_pnl", 0))          for r in all_rows)
    total_gross     = sum(float(r.get("gross_pnl", 0))         for r in all_rows)
    total_charges   = sum(float(r.get("charges_est", 0))       for r in all_rows)
    total_op_fee    = sum(float(r.get("operator_fee_amt", 0))  for r in all_rows)
    total_trades    = sum(int(r.get("total_trades", 0))        for r in all_rows)
    total_wins      = sum(int(r.get("wins", 0))                for r in all_rows)
    days            = len(all_rows)

    data = [
        ["Cumulative Summary", ""],
        ["Trading Days",       days],
        ["Total Trades",       total_trades],
        ["Total Wins",         total_wins],
        ["Overall Win Rate",   f"{round(total_wins/total_trades*100,1) if total_trades else 0}%"],
        [],
        ["Gross P&L (₹)",      round(total_gross,  2)],
        ["Charges Est (₹)",    round(total_charges, 2)],
        ["Net P&L (₹)",        round(total_net, 2)],
        ["Operator Fee (₹)",   round(total_op_fee, 2)],
        [],
        ["Investor", "Cumulative Share (₹)"],
    ]
    for inv in investors:
        prefix = inv["name"].replace(" ", "_").lower()
        total  = round(sum(float(r.get(f"{prefix}_share_amt", 0)) for r in all_rows), 2)
        data.append([inv["name"], total])

    data += [
        [],
        ["Last Updated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
    ]

    ws.clear()
    ws.update(data, value_input_option="USER_ENTERED")
    try:
        ws.format("A1:B1", {"textFormat": {"bold": True}, "fontSize": 14})
        ws.format("A12:B12", {"textFormat": {"bold": True}})
    except Exception:
        pass


def sync_to_google_sheets(all_rows: Optional[List[Dict]] = None) -> bool:
    """
    Full sync: Tracking + Config + Summary tabs.
    Returns True on success, False if Sheets not configured or error.
    """
    _, spreadsheet = _get_gsheet_client()
    if spreadsheet is None:
        return False

    config = load_config()
    if all_rows is None:
        all_rows = _read_csv_rows()

    try:
        _sync_tracking_sheet(spreadsheet, config, all_rows)
        _sync_config_sheet(spreadsheet, config)
        _sync_summary_sheet(spreadsheet, config, all_rows)
        logger.info("Google Sheets sync complete ✅")
        return True
    except Exception as e:
        logger.error(f"Google Sheets sync failed: {e}")
        return False

    return new_config
