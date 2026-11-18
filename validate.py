"""Data-quality rules applied to parsed quotes.

Every rule is a small, explainable check. A quote can carry several flags.
Quotes with a critical or warning flag are held for analyst review; quotes
with only info flags (or none) are published.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

import pandas as pd

RULES = {
    # rule: (severity, description)
    "PARSE_FAIL": ("critical", "Line looked like a quote but could not be parsed"),
    "UNKNOWN_ID": ("critical", "Identifier not found in the security master"),
    "INVERTED": ("critical", "Bid is above ask (crossed market)"),
    "PRICE_RANGE": ("critical", "Price/spread outside plausible range"),
    "CONSENSUS_OUTLIER": ("critical", "Mid far from other dealers' median the same day"),
    "DAY_JUMP": ("critical", "Mid moved too far vs the dealer's last good quote"),
    "WIDE_MARKET": ("warning", "Bid/ask width unusually wide"),
    "STALE": ("warning", "Identical bid/ask for 3+ consecutive quote days"),
    "DUPLICATE": ("warning", "Same quote repeated in the same message"),
    "ONE_SIDED": ("info", "Only one side of the market quoted"),
}

THRESHOLDS = {
    "bond_price_min": 40.0, "bond_price_max": 150.0,
    "bond_wide_pts": 2.0,
    "bond_consensus_pts": 1.25,  # tuned down from 2.0 after sensitivity analysis
    "bond_day_jump_pts": 3.0,
    "cds_min_bp": 5.0, "cds_max_bp": 1500.0,
    "cds_wide_bp": 20.0, "cds_wide_pct": 0.15,
    "cds_day_jump_pct": 0.30,
    "stale_days": 3,
}


def _mid(r) -> float | None:
    if pd.notna(r.bid) and pd.notna(r.ask):
        return (r.bid + r.ask) / 2
    return r.bid if pd.notna(r.bid) else (r.ask if pd.notna(r.ask) else None)


def validate(quotes: pd.DataFrame, exceptions: pd.DataFrame, t: dict = THRESHOLDS) -> pd.DataFrame:
    """Return one row per (quote, rule) flag. Quotes are identified by quote_id."""
    flags: list[dict] = []

    def flag(qid, rule, detail):
        sev = RULES[rule][0]
        flags.append({"quote_id": qid, "rule": rule, "severity": sev, "detail": detail})

    q = quotes.copy()
    q["mid"] = q.apply(_mid, axis=1)
    q = q.sort_values(["trade_date", "received_at", "line_no"])

    # ---- single-quote checks -------------------------------------------
    for r in q.itertuples():
        is_cds = r.instrument == "CDS"
        if r.security_id is None or pd.isna(r.security_id):
            flag(r.quote_id, "UNKNOWN_ID", f"'{r.raw_id}' not in master")
        if pd.isna(r.bid) or pd.isna(r.ask):
            flag(r.quote_id, "ONE_SIDED", "missing ask" if pd.isna(r.ask) else "missing bid")
        if pd.notna(r.bid) and pd.notna(r.ask):
            if r.bid > r.ask:
                flag(r.quote_id, "INVERTED", f"bid {r.bid:g} > ask {r.ask:g}")
            w = r.ask - r.bid
            if is_cds:
                lim = max(t["cds_wide_bp"], t["cds_wide_pct"] * abs(r.mid))
                if w > lim:
                    flag(r.quote_id, "WIDE_MARKET", f"width {w:g}bp > {lim:.0f}bp")
            elif w > t["bond_wide_pts"]:
                flag(r.quote_id, "WIDE_MARKET", f"width {w:.2f}pts > {t['bond_wide_pts']}")
        if r.mid is not None:
            lo, hi = (t["cds_min_bp"], t["cds_max_bp"]) if is_cds else (t["bond_price_min"], t["bond_price_max"])
            if not lo <= r.mid <= hi:
                flag(r.quote_id, "PRICE_RANGE", f"mid {r.mid:.2f} outside [{lo:g}, {hi:g}]")

    # ---- duplicates within a message -----------------------------------
    seen = set()
    for r in q.itertuples():
        key = (r.message_id, r.raw_id, r.bid, r.ask, r.bid_size, r.ask_size)
        if key in seen:
            flag(r.quote_id, "DUPLICATE", f"repeat of earlier line in {r.message_id}")
        seen.add(key)

    # ---- cross-dealer consensus (bonds) ----------------------------------
    known = q[q.security_id.notna() & q.mid.notna() & (q.instrument == "BOND")]
    for (sec, day), grp in known.groupby(["security_id", "trade_date"]):
        if grp.dealer.nunique() < 3:
            continue
        # median over all contributors is robust to one bad quote among three+
        med = median(grp.groupby("dealer").mid.first())
        for r in grp.itertuples():
            if abs(r.mid - med) > t["bond_consensus_pts"]:
                flag(r.quote_id, "CONSENSUS_OUTLIER", f"mid {r.mid:.2f} vs consensus median {med:.2f}")

    # ---- time-series checks per dealer/security ------------------------
    already_bad = defaultdict(set)
    for f in flags:
        if f["severity"] == "critical":
            already_bad[f["quote_id"]].add(f["rule"])
    last_good: dict[tuple, float] = {}
    history: dict[tuple, list[tuple]] = defaultdict(list)
    for r in q[q.security_id.notna()].itertuples():
        key = (r.dealer, r.security_id)
        if r.mid is not None and key in last_good:
            prev = last_good[key]
            jump = abs(r.mid - prev)
            if r.instrument == "CDS":
                if prev and jump / prev > t["cds_day_jump_pct"]:
                    flag(r.quote_id, "DAY_JUMP", f"mid {r.mid:.0f} vs last good {prev:.0f}")
                    already_bad[r.quote_id].add("DAY_JUMP")
            elif jump > t["bond_day_jump_pts"]:
                flag(r.quote_id, "DAY_JUMP", f"mid {r.mid:.2f} vs last good {prev:.2f}")
                already_bad[r.quote_id].add("DAY_JUMP")
        if r.mid is not None and not already_bad[r.quote_id]:
            last_good[key] = r.mid
        # stale: one entry per trade date (ignore duplicates within the day)
        h = history[key]
        if not h or h[-1][0] != r.trade_date:
            h.append((r.trade_date, r.bid, r.ask))
            n = t["stale_days"]
            if len(h) >= n and all(x[1:] == h[-1][1:] for x in h[-n:]) and pd.notna(r.ask):
                flag(r.quote_id, "STALE", f"same {r.bid:g}/{r.ask:g} for {n}+ days")

    # ---- parse exceptions become flags too --------------------------------
    out = pd.DataFrame(flags, columns=["quote_id", "rule", "severity", "detail"])
    if len(exceptions):
        ex = pd.DataFrame({"quote_id": None, "rule": "PARSE_FAIL", "severity": "critical",
                           "detail": exceptions.reason.values,
                           "message_id": exceptions.message_id.values,
                           "line_no": exceptions.line_no.values})
        out = pd.concat([out, ex], ignore_index=True)
    return out


def disposition(quotes: pd.DataFrame, flags: pd.DataFrame) -> pd.Series:
    """'held' if any critical/warning flag, else 'published'."""
    blocking = flags[flags.severity.isin(["critical", "warning"]) & flags.quote_id.notna()]
    held = set(blocking.quote_id)
    return quotes.quote_id.map(lambda x: "held" if x in held else "published")
