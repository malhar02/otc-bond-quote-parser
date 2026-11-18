"""End-to-end pipeline: messages -> parse -> validate -> SQLite -> reports -> scoring."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pandas as pd

from .parser import SecurityMaster, parse_message, to_rows
from .validate import RULES, disposition, validate

EXPECTED_RULES = {  # which rule(s) should catch each injected anomaly
    "inverted": {"INVERTED"},
    "fat_finger": {"CONSENSUS_OUTLIER", "DAY_JUMP", "PRICE_RANGE"},
    "wide": {"WIDE_MARKET"},
    "one_sided": {"ONE_SIDED"},
    "unknown_id": {"UNKNOWN_ID"},
    "malformed": {"PARSE_FAIL"},
    "duplicate": {"DUPLICATE"},
    "stale": {"STALE"},
    "off_market": {"CONSENSUS_OUTLIER", "DAY_JUMP"},
}


def load_messages(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def parse_all(messages: list[dict], master: SecurityMaster) -> tuple[pd.DataFrame, pd.DataFrame]:
    quotes, excs = [], []
    for m in messages:
        q, e = parse_message(m, master)
        quotes += q
        excs += e
    qdf = pd.DataFrame(to_rows(quotes))
    qdf.insert(0, "quote_id", range(1, len(qdf) + 1))
    edf = pd.DataFrame(to_rows(excs), columns=["message_id", "line_no", "dealer", "trade_date",
                                              "template", "reason", "raw_line"])
    return qdf, edf


def load_sql_queries(path: Path) -> dict[str, str]:
    out, name, buf = {}, None, []
    for line in path.read_text().splitlines():
        m = re.match(r"--\s*name:\s*(\w+)", line)
        if m:
            if name:
                out[name] = "\n".join(buf)
            name, buf = m.group(1), []
        else:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf)
    return out


def write_db(db: Path, schema: Path, root: Path, messages, quotes, excs, flags) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.executescript(schema.read_text())
    pd.read_csv(root / "data/reference/securities.csv").to_sql("securities", con, if_exists="append", index=False)
    pd.read_csv(root / "data/reference/cds_entities.csv").to_sql("cds_entities", con, if_exists="append", index=False)
    pd.DataFrame(messages).to_sql("messages", con, if_exists="append", index=False)
    quotes.to_sql("quotes", con, if_exists="append", index=False)
    excs.to_sql("parse_exceptions", con, if_exists="append", index=False)
    f = flags.copy()
    # attach message/line to quote flags for traceability
    ql = quotes.set_index("quote_id")[["message_id", "line_no"]]
    mask = f.quote_id.notna()
    f.loc[mask, "message_id"] = f.loc[mask, "quote_id"].map(ql.message_id)
    f.loc[mask, "line_no"] = f.loc[mask, "quote_id"].map(ql.line_no)
    f[["quote_id", "message_id", "line_no", "rule", "severity", "detail"]].to_sql(
        "flags", con, if_exists="append", index=False)
    con.commit()
    return con


def score(truth: pd.DataFrame, quotes: pd.DataFrame, excs: pd.DataFrame, flags: pd.DataFrame) -> dict:
    """Compare parser + validator output with the generator's ground truth."""
    t = truth.copy()
    key = ["message_id", "line_no"]
    qk = quotes.set_index(key)
    ek = set(map(tuple, excs[key].values.tolist())) if len(excs) else set()

    # --- parsing accuracy (field level) on lines that are parseable by design
    fields = ["security_id", "bid", "ask", "bid_size", "ask_size"]
    good = t[t.anomaly != "malformed"]
    total_fields = correct_fields = lines_ok = 0
    missed_lines = 0
    for r in good.itertuples():
        k = (r.message_id, r.line_no)
        if k not in qk.index:
            missed_lines += 1
            total_fields += len(fields)
            continue
        p = qk.loc[k]
        ok_line = True
        for fld in fields:
            tv, pv = getattr(r, fld), p[fld]
            same = (pd.isna(tv) and pd.isna(pv)) or (
                not pd.isna(tv) and not pd.isna(pv) and (
                    abs(float(tv) - float(pv)) < 1e-6 if fld != "security_id" else str(tv) == str(pv)))
            total_fields += 1
            correct_fields += same
            ok_line &= same
        lines_ok += ok_line
    malformed = t[t.anomaly == "malformed"]
    malformed_routed = sum((r.message_id, r.line_no) in ek for r in malformed.itertuples())
    truth_keys = set(map(tuple, t[key].values.tolist()))
    false_parses = sum(k not in truth_keys for k in qk.index)

    # --- anomaly detection --------------------------------------------------
    qid_by_key = {k: v for k, v in zip(map(tuple, quotes[key].values.tolist()), quotes.quote_id)}
    rules_by_qid = flags[flags.quote_id.notna()].groupby("quote_id").rule.apply(set).to_dict()
    held_qids = set(quotes.loc[quotes.disposition == "held", "quote_id"])
    detection = []
    for label, expected in EXPECTED_RULES.items():
        rows = t[t.anomaly == label]
        caught = caught_by_rule = 0
        for r in rows.itertuples():
            k = (r.message_id, r.line_no)
            qid = qid_by_key.get(k)
            if label == "malformed":
                hit, by_rule = k in ek, k in ek
            elif label == "one_sided":
                hit = by_rule = "ONE_SIDED" in rules_by_qid.get(qid, set())
            else:  # caught = held for review by any blocking rule
                hit = qid in held_qids
                by_rule = bool(rules_by_qid.get(qid, set()) & expected)
            caught += hit
            caught_by_rule += by_rule
        detection.append({"anomaly": label, "injected": int(len(rows)), "caught": int(caught),
                          "caught_by_expected_rule": int(caught_by_rule),
                          "expected_rules": ", ".join(sorted(expected))})

    # precision: of quotes held for review, how many carried an injected anomaly?
    anomalous_keys = set(map(tuple, t[t.anomaly.notna() & (t.anomaly != "one_sided")][key].values.tolist()))
    held = quotes[quotes.disposition == "held"]
    held_true = sum((r.message_id, r.line_no) in anomalous_keys for r in held.itertuples())
    injected_blocking = int(t.anomaly.isin([a for a in EXPECTED_RULES if a not in ("one_sided", "malformed")]).sum())
    caught_blocking = sum(d["caught"] for d in detection if d["anomaly"] not in ("one_sided", "malformed"))

    return {
        "truth_lines": int(len(t)),
        "parseable_lines": int(len(good)),
        "lines_fully_correct": int(lines_ok),
        "line_accuracy_pct": round(100 * lines_ok / len(good), 2),
        "field_accuracy_pct": round(100 * correct_fields / total_fields, 2),
        "lines_missed": int(missed_lines),
        "malformed_lines": int(len(malformed)),
        "malformed_routed_to_exceptions": int(malformed_routed),
        "false_parses": int(false_parses),
        "held_quotes": int(len(held)),
        "held_with_injected_anomaly": int(held_true),
        "review_precision_pct": round(100 * held_true / max(len(held), 1), 1),
        "anomaly_recall_pct": round(100 * caught_blocking / max(injected_blocking, 1), 1),
        "detection": detection,
    }


def run(root: Path) -> dict:
    master = SecurityMaster.load(root / "data/reference/securities.csv", root / "data/reference/cds_entities.csv")
    messages = load_messages(root / "data/raw/messages.jsonl")
    quotes, excs = parse_all(messages, master)
    flags = validate(quotes, excs)
    quotes["mid"] = quotes.apply(lambda r: (r.bid + r.ask) / 2 if pd.notna(r.bid) and pd.notna(r.ask)
                                 else (r.bid if pd.notna(r.bid) else r.ask), axis=1)
    quotes["disposition"] = disposition(quotes, flags)
    con = write_db(root / "output/quotes.db", root / "sql/schema.sql", root, messages, quotes, excs, flags)
    queries = load_sql_queries(root / "sql/reports.sql")
    reports = {name: pd.read_sql_query(sql, con) for name, sql in queries.items()}
    truth = pd.read_csv(root / "data/ground_truth.csv")
    metrics = score(truth, quotes, excs, flags)
    con.close()
    return {"messages": messages, "quotes": quotes, "exceptions": excs, "flags": flags,
            "reports": reports, "metrics": metrics, "rules": RULES}
