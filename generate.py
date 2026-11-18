"""Generate synthetic dealer quote messages ("runs") plus a ground-truth file.

Five fictional dealers send runs in five different formats over ten business
days. A small share of quotes carry deliberately injected problems
(inverted markets, fat-finger prices, stale quotes, duplicates, unknown
securities, one-sided quotes, garbled numbers) so the validator can be scored.
"""
from __future__ import annotations

import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from .reference import Bond, CDS, build_master

BOND_DEALERS = ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]
CDS_DEALER = "ECHO"
FRACTIONS = {0.125: "1/8", 0.25: "1/4", 0.375: "3/8", 0.5: "1/2", 0.625: "5/8", 0.75: "3/4", 0.875: "7/8"}

ANOMALY_RATES = [  # (label, probability) applied per quote line
    ("inverted", 0.012),
    ("fat_finger", 0.009),
    ("wide", 0.009),
    ("one_sided", 0.008),
    ("unknown_id", 0.007),
    ("malformed", 0.007),
    ("duplicate", 0.008),
    ("off_market", 0.012),  # subtle: 1.2-2.8 pts away from market, hard to catch
]


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def fmt_coupon_frac(c: float) -> str:
    whole, frac = int(c), round(c - int(c), 3)
    if frac == 0:
        return str(whole)
    if frac in FRACTIONS:
        return f"{whole} {FRACTIONS[frac]}"
    return f"{c:g}"


def pick_anomaly(rng: random.Random) -> str | None:
    u = rng.random()
    acc = 0.0
    for label, p in ANOMALY_RATES:
        acc += p
        if u < acc:
            return label
    return None


def transpose_price(p: float) -> float:
    s = f"{p:.2f}"
    if len(s) >= 5 and s[0] != s[1] and s[0].isdigit() and s[1].isdigit():
        return float(s[1] + s[0] + s[2:])
    return p + 10.0


def garble(num: str) -> str:
    """Corrupt a number the way bad OCR / fat fingers do (5->S, 0->O, double dot)."""
    for a, b in (("5", "S"), ("0", "O"), ("8", "B")):
        if a in num:
            return num.replace(a, b, 1)
    return num.replace(".", "..", 1)


def generate(out_dir: Path, seed: int = 42, n_days: int = 10) -> dict:
    rng = random.Random(seed)
    bonds, cds_list = build_master()
    days = business_days(date(2026, 11, 2), n_days)

    # ---- true market path -------------------------------------------------
    mids: dict[str, list[float]] = {}
    for b in bonds:
        p, path = b.base_price, []
        for i in range(n_days):
            shock = rng.gauss(0, 0.22)
            if i == 5:  # credit sell-off day
                shock -= 0.9
            if i == 6:
                shock += 0.35
            p += shock
            path.append(round(p, 3))
        mids[b.isin] = path
    cds_mids: dict[str, list[float]] = {}
    for c in cds_list:
        s, path = c.base_spread, []
        for i in range(n_days):
            s += rng.gauss(0, 2.0) + (9 if i == 5 else 0) - (3 if i == 6 else 0)
            path.append(round(s, 1))
        cds_mids[c.ref_id] = path

    # ---- dealer universes & quirks ---------------------------------------
    universes = {d: rng.sample(bonds, 26) for d in BOND_DEALERS}
    bias = {d: rng.uniform(-0.12, 0.12) for d in BOND_DEALERS}
    width = {"ALPHA": 0.5, "BRAVO": 0.4, "CHARLIE": 0.45, "DELTA": 0.35}
    stale_pairs = {(d, universes[d][i].isin) for d, i in (("BRAVO", 3), ("DELTA", 7), ("ALPHA", 11))}
    frozen: dict[tuple[str, str], tuple[float, float]] = {}

    messages, truth = [], []
    msg_counter = 0

    def new_msg(dealer: str, d: date, hour: int, subject: str) -> dict:
        nonlocal msg_counter
        msg_counter += 1
        ts = datetime(d.year, d.month, d.day, hour, rng.randint(0, 59), rng.randint(0, 59))
        return {"message_id": f"M{msg_counter:04d}", "dealer": dealer, "received_at": ts.isoformat(),
                "trade_date": d.isoformat(), "subject": subject, "lines": []}

    def add_truth(msg, line_no, **kw):
        row = {"message_id": msg["message_id"], "line_no": line_no, "dealer": msg["dealer"],
               "trade_date": msg["trade_date"], "instrument": "BOND", "security_id": None,
               "bid": None, "ask": None, "bid_size": None, "ask_size": None, "anomaly": None}
        row.update(kw)
        truth.append(row)

    for di, d in enumerate(days):
        for dealer in BOND_DEALERS:
            hour = {"ALPHA": 13, "BRAVO": 14, "CHARLIE": 15, "DELTA": 16}[dealer]
            subj = {"ALPHA": f"ALPHA IG RUN {d:%m/%d} - indicative",
                    "BRAVO": f"BRAVO Capital | USD IG axes {d:%d-%b}",
                    "CHARLIE": f"CHARLIE MKTS credit run {d:%d%b%y}".upper(),
                    "DELTA": f"DELTA levels {d.isoformat()}"}[dealer]
            msg = new_msg(dealer, d, hour, subj)
            L = msg["lines"]
            # headers / chatter
            if dealer == "ALPHA":
                L += ["Morning all, IG run below. Px indicative, sizes in MM.", "",
                      "TICKER CPN     MAT     BID       ASK      SIZE"]
            elif dealer == "BRAVO":
                L += ["BRAVO Capital - USD IG Axes", "pls call for size / color", ""]
            elif dealer == "CHARLIE":
                L += ["CHARLIE MARKETS RUN  (spd vs UST / px)", "----------------------------------"]
            else:
                L += ["Hi team, please find today's levels:", "",
                      "ISIN,Description,Bid,Ask,BidSize,AskSize"]

            todays = [b for b in universes[dealer]
                      if rng.random() < 0.82 or ((dealer, b.isin) in stale_pairs and di >= 1)]
            for b in todays:
                mid = mids[b.isin][di] + bias[dealer] + rng.gauss(0, 0.06)
                w = width[dealer] * rng.uniform(0.8, 1.25)
                dec = 3 if dealer == "ALPHA" else 2
                bid, ask = round(mid - w / 2, dec), round(mid + w / 2, dec)
                if dealer == "ALPHA":  # ALPHA quotes in 1/8ths
                    bid, ask = round(bid * 8) / 8, round(ask * 8) / 8
                    if ask <= bid:
                        ask = bid + 0.125
                bs, asz = rng.choice([1, 2, 2, 3, 5]), rng.choice([1, 2, 2, 3, 5])
                anomaly = pick_anomaly(rng)
                key = (dealer, b.isin)
                if key in stale_pairs and di >= 1:
                    anomaly = None
                    if key not in frozen:
                        frozen[key] = (bid, ask)
                    bid, ask = frozen[key]
                    run_len = di  # frozen from day 1 → identical on day 1..di
                    if run_len >= 3:
                        anomaly = "stale"
                ticker, cpn = b.ticker, b.coupon
                if anomaly == "inverted":
                    bid, ask = ask, bid
                elif anomaly == "fat_finger":
                    bid, ask = transpose_price(bid), transpose_price(ask)
                elif anomaly == "off_market":
                    shift = rng.choice([-1, 1]) * rng.uniform(1.2, 2.8)
                    bid, ask = round(bid + shift, dec), round(ask + shift, dec)
                    if dealer == "ALPHA":
                        bid, ask = round(bid * 8) / 8, round(ask * 8) / 8
                elif anomaly == "wide":
                    m = (bid + ask) / 2
                    w2 = rng.uniform(3.0, 5.0)
                    bid, ask = round(m - w2 / 2, dec), round(m + w2 / 2, dec)
                    if dealer == "ALPHA":
                        bid, ask = round(bid * 8) / 8, round(ask * 8) / 8
                elif anomaly == "unknown_id":
                    cpn = round(cpn + 0.1, 3)  # description no longer matches master
                one_sided = anomaly == "one_sided"
                malformed = anomaly == "malformed"

                # ---- render line in dealer format -----------------------
                bid_s = f"{bid:.{dec}f}"
                ask_s = "--" if one_sided else f"{ask:.{dec}f}"
                if malformed:
                    bid_s = garble(bid_s)
                if dealer == "ALPHA":
                    c_txt = fmt_coupon_frac(cpn)
                    line = f"{ticker:<6} {c_txt:<7} {b.maturity}   {bid_s:<8} / {ask_s:<8} {bs}x{'' if one_sided else asz}".rstrip()
                    if one_sided:
                        line = line.rstrip("x") + "x0"
                elif dealer == "BRAVO":
                    isin = b.isin if anomaly != "unknown_id" else b.isin[:-3] + f"{(int(b.isin[-3:]) + 7) % 1000:03d}"
                    sz = lambda n: "500K" if n == 1 and rng.random() < 0.3 else f"{n}MM"
                    bsz_txt, asz_txt = sz(bs), sz(asz)
                    line = f"{isin} | BID {bid_s} ASK {ask_s} | SZ {bsz_txt} x {'--' if one_sided else asz_txt}"
                    bs = 0.5 if bsz_txt == "500K" else bs
                    asz = 0.5 if asz_txt == "500K" else asz
                elif dealer == "CHARLIE":
                    base_spd = {"UST2": 55, "UST5": 95, "UST10": 135, "UST30": 165}[b.benchmark]
                    sp_bid = round(base_spd + (100 - bid) * 3.0)
                    sp_ask = round(base_spd + (100 - (ask if not one_sided else bid)) * 3.0)
                    tenor = b.benchmark.replace("UST", "T")
                    spd_txt = f"{tenor}+{sp_bid}/+{sp_ask}" if not one_sided else f"{tenor}+{sp_bid}/--"
                    asz = bs  # CHARLIE shows one size for both sides
                    line = f"{ticker} {cpn:g} {b.maturity}   {spd_txt:<14} {bid_s}-{ask_s}   {bs}MM"
                else:  # DELTA CSV
                    isin = b.isin if anomaly != "unknown_id" else b.isin[:-3] + f"{(int(b.isin[-3:]) + 7) % 1000:03d}"
                    line = f"{isin},{ticker} {cpn:g} {b.maturity},{bid_s},{'' if one_sided else ask_s},{bs},{'' if one_sided else asz}"

                L.append(line)
                ln = len(L)
                if malformed:
                    add_truth(msg, ln, anomaly="malformed")
                else:
                    add_truth(msg, ln, security_id=None if anomaly == "unknown_id" else b.isin,
                              bid=bid, ask=None if one_sided else ask, bid_size=float(bs),
                              ask_size=None if one_sided else float(asz), anomaly=anomaly)
                if anomaly == "duplicate":
                    # the original line was clean; the repeat is the anomaly
                    truth[-1]["anomaly"] = None
                    L.append(line)
                    add_truth(msg, len(L), security_id=b.isin, bid=bid, ask=ask, bid_size=float(bs),
                              ask_size=float(asz), anomaly="duplicate")

            if dealer == "ALPHA":
                L += ["", "-- ALPHA Securities Credit Desk. Indicative only, not an offer."]
            elif dealer == "DELTA":
                L += ["", "Thanks, DELTA Trading"]
            messages.append(msg)

        # ---- ECHO CDS run -------------------------------------------------
        msg = new_msg(CDS_DEALER, d, 17, f"ECHO CDS 5Y USD {d:%d/%m}")
        L = msg["lines"]
        L += ["ECHO Derivatives - single name CDS, 5Y, bp running", ""]
        names = list(cds_list) + ([CDS("TSLA-5Y-USD", "TSLA", "5Y", "USD", 150.0)] if di in (3, 8) else [])
        for c in names:
            unknown = c.ticker == "TSLA"
            mid = cds_mids.get(c.ref_id, [150.0] * n_days)[di] + rng.gauss(0, 1.0)
            w = rng.uniform(5, 9)
            bid, ask = round(mid - w / 2), round(mid + w / 2)
            anomaly = "unknown_id" if unknown else None
            r = rng.random()
            if not unknown and r < 0.03:
                anomaly, (bid, ask) = "inverted", (ask, bid)
            elif not unknown and r < 0.05:
                anomaly, bid, ask = "fat_finger", bid * 10, ask * 10
            L.append(f"{c.ticker:<5} 5Y   {bid} / {ask}")
            add_truth(msg, len(L), instrument="CDS", security_id=None if unknown else c.ref_id,
                      bid=float(bid), ask=float(ask), anomaly=anomaly)
        messages.append(msg)

    # ---- write ------------------------------------------------------------
    raw = out_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    with (raw / "messages.jsonl").open("w") as f:
        for m in messages:
            rec = {k: v for k, v in m.items() if k != "lines"}
            rec["body"] = "\n".join(m["lines"])
            f.write(json.dumps(rec) + "\n")
    with (out_dir / "ground_truth.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(truth[0].keys()))
        w.writeheader()
        w.writerows(truth)
    return {"messages": len(messages), "quote_lines": len(truth),
            "anomalies": sum(1 for t in truth if t["anomaly"])}
