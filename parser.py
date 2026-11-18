"""Rule-based parser: turns free-text dealer runs into structured quotes.

Each line of a message is tried against a library of templates (regexes).
A match is normalised (fractional coupons, K/MM sizes, '--' for a missing
side) and its identifier is resolved against the security master. Lines that
look like quotes but cannot be parsed cleanly are routed to an exceptions
list with a reason, instead of being silently dropped or guessed.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from pathlib import Path

NUM = re.compile(r"^\d+(?:\.\d+)?$")
ISIN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")

TEMPLATES: list[tuple[str, re.Pattern]] = [
    # BRAVO style:  XS1234567890 | BID 98.10 ASK 98.60 | SZ 1MM x 500K
    ("isin_pipe", re.compile(
        r"^(?P<isin>[A-Z]{2}[A-Z0-9]{9}\d)\s*\|\s*BID\s+(?P<bid>\S+)\s+ASK\s+(?P<ask>\S+)"
        r"\s*\|\s*SZ\s+(?P<bsz>\S+)\s*x\s*(?P<asz>\S+)\s*$", re.I)),
    # DELTA style CSV:  ISIN,Description,Bid,Ask,BidSize,AskSize
    ("isin_csv", re.compile(
        r"^(?P<isin>[A-Z]{2}[A-Z0-9]{9}\d),(?P<desc>[^,]*),(?P<bid>[^,]*),(?P<ask>[^,]*),"
        r"(?P<bsz>[^,]*),(?P<asz>[^,]*)$")),
    # CHARLIE style:  VZ 4.5 08/33   T10+125/+118   98.40-98.85   3MM
    ("desc_spread_px", re.compile(
        r"^(?P<tkr>[A-Z]{1,6})\s+(?P<cpn>\d+(?:\.\d+)?)\s+(?P<mat>\d{2}/\d{2})\s+"
        r"(?P<bench>T\d+)\+(?P<sbid>\d+)/(?:\+(?P<sask>\d+)|--)\s+"
        r"(?P<bid>[^\s-]+)-(?P<ask>\S+)\s+(?P<sz>\d+(?:\.\d+)?)MM\s*$")),
    # ALPHA style:  AAPL   3 7/8   05/43   98.250  / 98.750  2x2
    ("desc_px", re.compile(
        r"^(?P<tkr>[A-Z]{1,6})\s+(?P<cpn>\d+(?:\.\d+)?(?:\s+\d/\d)?|\d/\d)\s+(?P<mat>\d{2}/\d{2})\s+"
        r"(?P<bid>\S+)\s*/\s*(?P<ask>\S+)\s+(?P<bsz>\d+(?:\.\d+)?)x(?P<asz>\d+(?:\.\d+)?)\s*$")),
    # ECHO style CDS:  F     5Y   245 / 252
    ("cds_spread", re.compile(
        r"^(?P<tkr>[A-Z]{1,6})\s+(?P<tenor>\d{1,2}Y)\s+(?P<bid>\S+)\s*/\s*(?P<ask>\S+)\s*$")),
]


class ParseError(ValueError):
    pass


@dataclass
class Quote:
    message_id: str
    line_no: int
    dealer: str
    trade_date: str
    received_at: str
    template: str
    instrument: str
    raw_id: str
    security_id: str | None
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    benchmark: str | None = None
    bid_spread: float | None = None
    ask_spread: float | None = None
    raw_line: str = ""


@dataclass
class ParseException:
    message_id: str
    line_no: int
    dealer: str
    trade_date: str
    template: str | None
    reason: str
    raw_line: str


@dataclass
class SecurityMaster:
    by_isin: dict = field(default_factory=dict)
    by_desc: dict = field(default_factory=dict)
    cds: dict = field(default_factory=dict)
    tickers: set = field(default_factory=set)

    @classmethod
    def load(cls, bonds_csv: Path, cds_csv: Path) -> "SecurityMaster":
        m = cls()
        with bonds_csv.open() as f:
            for r in csv.DictReader(f):
                m.by_isin[r["isin"]] = r
                m.by_desc[(r["ticker"], round(float(r["coupon"]), 4), r["maturity"])] = r["isin"]
                m.tickers.add(r["ticker"])
        with cds_csv.open() as f:
            for r in csv.DictReader(f):
                m.cds[(r["ticker"], r["tenor"])] = r["ref_id"]
        return m


# ---------------------------------------------------------------- normalisers
def to_price(tok: str, side: str) -> float | None:
    tok = (tok or "").strip()
    if tok in ("", "--", "-", "n/a", "N/A"):
        return None
    if not NUM.match(tok):
        raise ParseError(f"non-numeric {side} '{tok}'")
    return float(tok)


def to_coupon(tok: str) -> float:
    tok = tok.strip()
    total = Fraction(0)
    for part in tok.split():
        total += Fraction(part)  # handles '3', '3.25', '7/8'
    return round(float(total), 4)


def to_size_mm(tok: str | None) -> float | None:
    if tok is None:
        return None
    t = tok.strip().upper()
    if t in ("", "--", "0"):
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(MM|M|K)?$", t)
    if not m:
        raise ParseError(f"bad size '{tok}'")
    val, unit = float(m.group(1)), m.group(2)
    return val / 1000 if unit == "K" else val


def looks_like_quote(line: str, master: SecurityMaster) -> bool:
    """Used only when no template matched: is this a quote we failed to read?"""
    if ISIN.search(line):
        return True
    first = line.split()[0] if line.split() else ""
    nums = re.findall(r"\d+(?:\.\d+)?", line)
    return first in master.tickers and len(nums) >= 3


# -------------------------------------------------------------------- parser
def parse_message(msg: dict, master: SecurityMaster) -> tuple[list[Quote], list[ParseException]]:
    quotes, excs = [], []
    for line_no, raw in enumerate(msg["body"].split("\n"), start=1):
        line = raw.strip()
        if not line or not re.search(r"\d", line):
            continue
        matched = False
        for name, rx in TEMPLATES:
            m = rx.match(line)
            if not m:
                continue
            matched = True
            g = m.groupdict()
            try:
                q = _build_quote(name, g, msg, line_no, line, master)
            except ParseError as e:
                excs.append(ParseException(msg["message_id"], line_no, msg["dealer"], msg["trade_date"],
                                           name, str(e), line))
                break
            if q is not None:
                quotes.append(q)
            break
        if not matched and looks_like_quote(line, master):
            excs.append(ParseException(msg["message_id"], line_no, msg["dealer"], msg["trade_date"],
                                       None, "no template matched", line))
    return quotes, excs


def _build_quote(name, g, msg, line_no, line, master) -> Quote | None:
    base = dict(message_id=msg["message_id"], line_no=line_no, dealer=msg["dealer"],
                trade_date=msg["trade_date"], received_at=msg["received_at"], template=name, raw_line=line)
    bid, ask = to_price(g["bid"], "bid"), to_price(g["ask"], "ask")
    if bid is None and ask is None:
        raise ParseError("no price on either side")

    if name == "cds_spread":
        tkr, tenor = g["tkr"], g["tenor"]
        return Quote(**base, instrument="CDS", raw_id=f"{tkr} {tenor}",
                     security_id=master.cds.get((tkr, tenor)), bid=bid, ask=ask,
                     bid_size=None, ask_size=None)

    if name in ("isin_pipe", "isin_csv"):
        isin = g["isin"]
        sec = isin if isin in master.by_isin else None
        bsz, asz = to_size_mm(g["bsz"]), to_size_mm(g["asz"])
        return Quote(**base, instrument="BOND", raw_id=isin, security_id=sec, bid=bid, ask=ask,
                     bid_size=bsz, ask_size=None if ask is None else asz)

    # description-based templates
    cpn = to_coupon(g["cpn"])
    desc = f"{g['tkr']} {cpn:g} {g['mat']}"
    sec = master.by_desc.get((g["tkr"], cpn, g["mat"]))
    if name == "desc_spread_px":
        sz = float(g["sz"])
        return Quote(**base, instrument="BOND", raw_id=desc, security_id=sec, bid=bid, ask=ask,
                     bid_size=sz, ask_size=None if ask is None else sz, benchmark=g["bench"],
                     bid_spread=float(g["sbid"]), ask_spread=float(g["sask"]) if g["sask"] else None)
    bsz, asz = to_size_mm(g["bsz"]), to_size_mm(g["asz"])
    return Quote(**base, instrument="BOND", raw_id=desc, security_id=sec, bid=bid, ask=ask,
                 bid_size=bsz, ask_size=None if ask is None else asz)


def to_rows(items) -> list[dict]:
    return [asdict(x) for x in items]
