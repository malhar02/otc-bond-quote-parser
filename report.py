"""Excel data-quality report and dashboard data export."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .validate import RULES, THRESHOLDS, disposition, validate

HEADER_FILL = PatternFill("solid", fgColor="1F3A5F")
HEADER_FONT = Font(bold=True, color="FFFFFF")
RED_FILL = PatternFill("solid", fgColor="F8D7DA")
AMBER_FILL = PatternFill("solid", fgColor="FFF3CD")


def _sheet(wb: Workbook, title: str, df: pd.DataFrame, widths: dict | None = None):
    ws = wb.create_sheet(title)
    ws.append(list(df.columns))
    for row in df.itertuples(index=False):
        ws.append([None if (isinstance(v, float) and pd.isna(v)) else v for v in row])
    for c in ws[1]:
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(df.columns, start=1):
        w = (widths or {}).get(col)
        if w is None:
            sample = [len(str(col))] + [len(str(v)) for v in df[col].head(200).tolist()]
            w = min(max(sample) + 2, 60)
        ws.column_dimensions[get_column_letter(i)].width = w
    return ws


def write_excel(path: Path, result: dict) -> None:
    m, rep = result["metrics"], result["reports"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "OTC Quote Parsing - Data Quality Report"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = "Synthetic data for demonstration. Period: " + \
        f"{rep['daily_summary'].trade_date.min()} to {rep['daily_summary'].trade_date.max()}"
    kpis = [
        ("Messages processed", len(result["messages"])),
        ("Quote lines parsed", len(result["quotes"])),
        ("Parse exceptions (routed to review)", len(result["exceptions"])),
        ("Field-level parse accuracy vs ground truth", f"{m['field_accuracy_pct']}%"),
        ("Quotes held for review", m["held_quotes"]),
        ("Held quotes with a real problem (precision)", f"{m['review_precision_pct']}%"),
        ("Injected problems caught (recall, excl. info)", f"{m['anomaly_recall_pct']}%"),
    ]
    for i, (k, v) in enumerate(kpis, start=4):
        ws.cell(i, 1, k).font = Font(bold=True)
        ws.cell(i, 2, v)
    ws.column_dimensions["A"].width = 48
    ws.column_dimensions["B"].width = 18

    _sheet(wb, "Daily", rep["daily_summary"])
    dsc = _sheet(wb, "Dealer Scorecard", rep["dealer_scorecard"])
    col = get_column_letter(list(rep["dealer_scorecard"].columns).index("held_pct") + 1)
    rng = f"{col}2:{col}{len(rep['dealer_scorecard']) + 1}"
    dsc.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["9"], fill=RED_FILL))
    dsc.conditional_formatting.add(rng, CellIsRule(operator="between", formula=["5", "9"], fill=AMBER_FILL))

    rq = rep["review_queue"].drop(columns=["sev_rank"])
    _sheet(wb, "Review Queue", rq, {"details": 60, "raw_line": 60})
    _sheet(wb, "Flags by Rule", rep["flags_by_rule"].assign(
        description=lambda d: d.rule.map(lambda r: RULES[r][1])))
    _sheet(wb, "Parse Exceptions", result["exceptions"], {"raw_line": 60})
    _sheet(wb, "Detection Scoring", pd.DataFrame(m["detection"]))
    _sheet(wb, "Rules & Thresholds", pd.DataFrame(
        [{"rule": k, "severity": v[0], "description": v[1]} for k, v in RULES.items()] +
        [{"rule": f"threshold: {k}", "severity": "", "description": v} for k, v in THRESHOLDS.items()]))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def threshold_sensitivity(result: dict, truth: pd.DataFrame) -> list[dict]:
    """How many quotes would be held, and how many problems caught, at other consensus thresholds."""
    quotes, excs = result["quotes"], result["exceptions"]
    bad = set(map(tuple, truth[truth.anomaly.notna() & ~truth.anomaly.isin(["one_sided", "malformed"])]
                  [["message_id", "line_no"]].values.tolist()))
    off = set(map(tuple, truth[truth.anomaly == "off_market"][["message_id", "line_no"]].values.tolist()))
    rows = []
    for th in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]:
        t = dict(THRESHOLDS, bond_consensus_pts=th)
        f = validate(quotes[[c for c in quotes.columns if c not in ("mid", "disposition")]], excs, t)
        disp = disposition(quotes, f)
        held = quotes[disp == "held"]
        keys = set(map(tuple, held[["message_id", "line_no"]].values.tolist()))
        rows.append({"threshold_pts": th, "held": len(held), "true_problems": len(keys & bad),
                     "false_alarms": len(keys - bad), "off_market_caught": len(keys & off),
                     "off_market_total": len(off), "recall_pct": round(100 * len(keys & bad) / len(bad), 1)})
    return rows


def export_dashboard(path: Path, result: dict, truth: pd.DataFrame) -> dict:
    q = result["quotes"]
    f = result["flags"]
    rep = result["reports"]
    sec = pd.read_csv(Path(path).parents[1] / "data/reference/securities.csv")
    rules_by_q = f[f.quote_id.notna()].groupby("quote_id").agg(
        rules=("rule", lambda s: sorted(set(s))), details=("detail", lambda s: " | ".join(s))).reset_index()
    qq = q.merge(rules_by_q, on="quote_id", how="left")
    qq["rules"] = qq.rules.apply(lambda v: v if isinstance(v, list) else [])
    qq["details"] = qq.details.fillna("")

    def clean(v):
        if isinstance(v, float) and pd.isna(v):
            return None
        if hasattr(v, "item"):
            return v.item()
        return v

    quote_rows = [{k: clean(v) for k, v in r.items()} for r in qq[[
        "quote_id", "message_id", "line_no", "dealer", "trade_date", "template", "instrument", "raw_id",
        "security_id", "bid", "ask", "bid_size", "ask_size", "mid", "disposition", "rules", "details",
        "raw_line"]].to_dict("records")]

    # bonds with most contributors for the price-history chart
    counts = q[q.instrument == "BOND"].groupby("security_id").dealer.nunique().sort_values(ascending=False)
    chart_secs = counts.index.tolist()
    sec_meta = {r.isin: {"label": f"{r.ticker} {r.coupon:g} {r.maturity}", "issuer": r.issuer,
                         "sector": r.sector, "rating": r.rating} for r in sec.itertuples()}

    data = {
        "generated": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M UTC"),
        "metrics": result["metrics"],
        "rules": {k: {"severity": v[0], "description": v[1]} for k, v in RULES.items()},
        "thresholds": THRESHOLDS,
        "daily": rep["daily_summary"].to_dict("records"),
        "flags_by_rule": rep["flags_by_rule"].to_dict("records"),
        "dealers": rep["dealer_scorecard"].to_dict("records"),
        "exceptions": result["exceptions"].to_dict("records"),
        "messages": [{k: m[k] for k in ("message_id", "dealer", "received_at", "trade_date", "subject", "body")}
                     for m in result["messages"]],
        "quotes": quote_rows,
        "securities": {s: sec_meta[s] for s in chart_secs if s in sec_meta},
        "chart_securities": chart_secs,
        "sensitivity": threshold_sensitivity(result, truth),
        "off_market_misses": _off_market_misses(q, truth),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("window.DASH = " + json.dumps(data, default=clean, separators=(",", ":")) + ";\n")
    return data


def _off_market_misses(q: pd.DataFrame, truth: pd.DataFrame) -> list[dict]:
    """For each planted off-market quote that was published, how many dealers quoted that bond that day."""
    x = q.merge(truth[["message_id", "line_no", "anomaly"]], on=["message_id", "line_no"])
    out = []
    for r in x[(x.anomaly == "off_market") & (x.disposition == "published")].itertuples():
        n = q[(q.security_id == r.security_id) & (q.trade_date == r.trade_date)].dealer.nunique()
        out.append({"dealer": r.dealer, "trade_date": r.trade_date, "security_id": r.security_id, "dealers_that_day": int(n)})
    return out
