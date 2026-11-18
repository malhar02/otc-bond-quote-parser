"""Security master: the reference data every parsed quote is matched against.

All identifiers and prices in this project are SYNTHETIC. Issuer tickers are
used for realism only; ISINs are generated (with valid check digits) and do not
refer to real securities.
"""
from __future__ import annotations

import csv
import random
from dataclasses import dataclass, asdict
from pathlib import Path

ISSUERS = [
    # ticker, issuer name, sector, rating
    ("AAPL", "Apple Inc", "Technology", "AA+"),
    ("MSFT", "Microsoft Corp", "Technology", "AAA"),
    ("ORCL", "Oracle Corp", "Technology", "BBB"),
    ("IBM", "IBM Corp", "Technology", "A-"),
    ("VZ", "Verizon Communications", "Telecom", "BBB+"),
    ("T", "AT&T Inc", "Telecom", "BBB"),
    ("CMCSA", "Comcast Corp", "Media", "A-"),
    ("JPM", "JPMorgan Chase & Co", "Financials", "A-"),
    ("GS", "Goldman Sachs Group", "Financials", "BBB+"),
    ("BAC", "Bank of America Corp", "Financials", "A-"),
    ("WFC", "Wells Fargo & Co", "Financials", "BBB+"),
    ("F", "Ford Motor Co", "Autos", "BBB-"),
    ("GM", "General Motors Co", "Autos", "BBB"),
    ("XOM", "Exxon Mobil Corp", "Energy", "AA-"),
    ("CVX", "Chevron Corp", "Energy", "AA-"),
    ("PFE", "Pfizer Inc", "Healthcare", "A"),
    ("CVS", "CVS Health Corp", "Healthcare", "BBB"),
    ("KO", "Coca-Cola Co", "Consumer", "A+"),
    ("BA", "Boeing Co", "Industrials", "BBB-"),
    ("DIS", "Walt Disney Co", "Media", "A-"),
]

CDS_NAMES = ["F", "GM", "BA", "T", "ORCL", "CVS"]


def isin_check_digit(body: str) -> str:
    """ISO 6166 check digit (Luhn over letters expanded to numbers)."""
    digits = "".join(str(int(c, 36)) for c in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def make_isin(rng: random.Random, prefix: str = "XS") -> str:
    body = prefix + "".join(rng.choice("0123456789") for _ in range(9))
    return body + isin_check_digit(body)


@dataclass
class Bond:
    isin: str
    ticker: str
    issuer: str
    sector: str
    rating: str
    coupon: float
    maturity: str  # MM/YY
    currency: str
    base_price: float
    benchmark: str  # UST tenor used for spread quoting


@dataclass
class CDS:
    ref_id: str  # e.g. F-5Y-USD
    ticker: str
    tenor: str
    currency: str
    base_spread: float  # bp


COUPONS = [2.25, 2.5, 3.0, 3.125, 3.25, 3.5, 3.625, 3.85, 4.0, 4.125, 4.25, 4.5,
           4.75, 5.0, 5.125, 5.25, 5.5, 5.875, 6.0, 6.25]


def build_master(seed: int = 7) -> tuple[list[Bond], list[CDS]]:
    rng = random.Random(seed)
    bonds: list[Bond] = []
    for ticker, name, sector, rating in ISSUERS:
        used = set()
        for _ in range(2):  # two lines per issuer
            while True:
                cpn = rng.choice(COUPONS)
                yy = rng.randint(27, 55)
                mm = rng.randint(1, 12)
                key = (cpn, yy, mm)
                if key not in used:
                    used.add(key)
                    break
            years = (2000 + yy) - 2026
            # rough price: coupon vs a 5% market yield, damped by duration
            price = 100 + (cpn - 5.0) * min(years, 15) * 0.7 + rng.uniform(-3, 3)
            price = round(max(min(price, 112), 72), 3)
            bench = "UST2" if years <= 3 else "UST5" if years <= 7 else "UST10" if years <= 15 else "UST30"
            bonds.append(Bond(make_isin(rng), ticker, name, sector, rating, cpn,
                              f"{mm:02d}/{yy:02d}", "USD", price, bench))
    cds = []
    for t in CDS_NAMES:
        cds.append(CDS(f"{t}-5Y-USD", t, "5Y", "USD", round(rng.uniform(60, 320), 1)))
    return bonds, cds


def write_master(path: Path, bonds: list[Bond]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(bonds[0]).keys()))
        w.writeheader()
        for b in bonds:
            w.writerow(asdict(b))


def write_cds(path: Path, cds: list[CDS]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(cds[0]).keys()))
        w.writeheader()
        for c in cds:
            w.writerow(asdict(c))
