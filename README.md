# OTC Bond & CDS Quote Parser

**Live dashboard: https://malhar02.github.io/otc-bond-quote-parser/**

Dealers in OTC markets send prices for bonds and CDS as free-text messages ("runs") in their own layouts.
This project parses those messages into structured quotes, matches each one to a security master,
runs data-quality checks and holds suspect quotes for analyst review before publishing, then reports
on the results in SQL, Excel and an online dashboard.

> All data is **synthetic**. Issuer tickers are used for realism only; ISINs (valid check digits),
> prices, spreads and dealers are generated and do not represent real securities or real quotes.

## Results (10 trading days, 5 dealers, 50 messages)

| Measure | Result |
|---|---|
| Quote lines parsed | 929 |
| Field-level parse accuracy vs ground truth (ID, bid, ask, sizes) | 100% (929/929 lines) |
| Garbled lines routed to an exceptions queue instead of being guessed | 6/6 |
| Quotes held for review | 64 (6.9%) |
| Held quotes that had a real problem (precision) | 100% |
| Planted bad quotes caught (recall) | 94.1% |
| Subtle off-market prices (1.2–2.8 pts away) caught | 4/8 after tuning (2/8 before) |

The parse accuracy is high because the five message layouts are known in advance. With live dealer traffic,
new layouts and format drift would show up as parse exceptions and need new templates.

## How it works

```
messages.jsonl ─► parse (5 regex templates) ─► match to security master ─► 10 validation rules
                                                                              │
                     dashboard ◄─ docs/data.js ◄─ Excel DQ pack ◄─ SQL reports ◄─ SQLite
```

### 1. Five dealer formats

| Dealer | Style | Example line |
|---|---|---|
| ALPHA | Ticker, fractional coupon, price in 1/8ths, size `BxA` | `AAPL   3 7/8   05/43   98.250  / 98.750  2x2` |
| BRAVO | ISIN, pipe-delimited, sizes in K/MM | `XS1860913901 \| BID 98.10 ASK 98.60 \| SZ 500K x 2MM` |
| CHARLIE | Spread vs Treasury **and** price | `VZ 5.125 12/36   T10+136/+135   99.65-100.13   1MM` |
| DELTA | CSV pasted into the email body | `XS2227918054,XOM 5.125 03/51,102.03,102.43,3,1` |
| ECHO | Single-name CDS, 5Y, basis points | `F     5Y   245 / 252` |

The parser handles fractional coupons (`5 7/8` → 5.875), K/MM sizes, one-sided quotes (`--`), headers,
greetings and disclaimers. Lines that look like quotes but cannot be read cleanly (e.g. `98.2S` from a typo or bad OCR)
go to `parse_exceptions` with a reason.

### 2. Validation rules

| Rule | Severity | Check |
|---|---|---|
| PARSE_FAIL | critical | Line looked like a quote but could not be parsed |
| UNKNOWN_ID | critical | Identifier not in the security master |
| INVERTED | critical | Bid above ask (crossed market) |
| PRICE_RANGE | critical | Bond mid outside 40–150; CDS outside 5–1,500 bp |
| CONSENSUS_OUTLIER | critical | Mid more than 1.25 pts from the median of all dealers that day (needs 3+ dealers) |
| DAY_JUMP | critical | Mid moved more than 3 pts (bonds) / 30% (CDS) from the dealer's last good quote |
| WIDE_MARKET | warning | Bid/ask wider than 2 pts (bonds) or max(20 bp, 15%) for CDS |
| STALE | warning | Same bid/ask from a dealer for 3+ consecutive quote days |
| DUPLICATE | warning | Same quote repeated within one message |
| ONE_SIDED | info | Only one side quoted (published, but flagged) |

Quotes with any critical or warning flag are **held**; everything else is **published**.

### 3. Scoring against ground truth

The generator plants known problems (crossed markets, fat-finger digit swaps, subtle off-market prices,
wide markets, stale quotes, duplicates, unknown securities, garbled numbers, one-sided quotes) and writes
`data/ground_truth.csv`. The pipeline compares its own output with that file, so parse accuracy and detection
rates are measured, not assumed.

### 4. Threshold tuning

The consensus tolerance started at 2.0 pts. Re-running the validator at tolerances from 0.5 to 3.0 pts showed that
1.25 pts catches twice as many subtle off-market prices (4/8 vs 2/8) with no extra false alarms; tighter than 1.0
starts flagging good quotes. All 4 remaining misses were on bonds quoted by 2 or fewer dealers that day, where a
consensus check cannot run. The full table is on the dashboard's *Accuracy & tuning* tab.

## Outputs

| File | What it is |
|---|---|
| `output/quotes.db` | SQLite database: `messages`, `quotes`, `parse_exceptions`, `flags`, `securities`, `cds_entities` |
| `sql/reports.sql` | Report queries: daily summary, flags by rule, dealer scorecard, review queue, consensus mid |
| `output/dq_report.xlsx` | Excel data-quality pack: summary, daily trend, dealer scorecard, review queue, exceptions, scoring, rules |
| `docs/` | The dashboard (static HTML + Chart.js), served by GitHub Pages |

Example query (dealer scorecard, abridged):

```sql
SELECT dealer, COUNT(*) AS quotes,
       SUM(disposition = 'held') AS held,
       ROUND(100.0 * SUM(disposition = 'held') / COUNT(*), 1) AS held_pct
FROM quotes
GROUP BY dealer
ORDER BY held_pct DESC;
```

## Run it

```bash
pip install -r requirements.txt
python run_pipeline.py               # parse, validate, build DB, Excel and dashboard data
python run_pipeline.py --regenerate  # also regenerate the synthetic messages
pytest -q                            # 16 unit tests
```

Open `docs/index.html` in a browser to view the dashboard locally.

## Project layout

```
src/quoteparser/
  reference.py   security master + ISIN check digits
  generate.py    synthetic dealer messages + ground truth
  parser.py      templates, normalisers, identifier matching, exceptions
  validate.py    data-quality rules and thresholds
  pipeline.py    end-to-end run, SQLite load, scoring
  report.py      Excel report, threshold sensitivity, dashboard export
sql/             schema.sql, reports.sql
data/            raw messages, ground truth, reference data
output/          quotes.db, dq_report.xlsx
docs/            dashboard (GitHub Pages)
tests/           pytest suite
```

## Limitations and next steps

- Formats are fixed and known; a real system would need template versioning and alerts when a dealer's layout changes.
- Prices follow a simple random walk with one sell-off day; real markets have more structure.
- Next: an MS Access front end for the review queue, yield/spread conversion from price, and bond-level
  checks such as price vs. coupon and maturity consistency.
