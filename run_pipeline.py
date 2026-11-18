"""Run the full pipeline.

    python run_pipeline.py              # parse existing messages, rebuild DB, report and dashboard
    python run_pipeline.py --regenerate # also regenerate the synthetic messages first
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from quoteparser.generate import generate  # noqa: E402
from quoteparser.pipeline import run  # noqa: E402
from quoteparser.reference import build_master, write_cds, write_master  # noqa: E402
from quoteparser.report import export_dashboard, write_excel  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regenerate", action="store_true", help="regenerate synthetic input data")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.regenerate or not (ROOT / "data/raw/messages.jsonl").exists():
        bonds, cds = build_master()
        write_master(ROOT / "data/reference/securities.csv", bonds)
        write_cds(ROOT / "data/reference/cds_entities.csv", cds)
        print("generated:", generate(ROOT / "data", seed=args.seed))

    result = run(ROOT)
    write_excel(ROOT / "output/dq_report.xlsx", result)
    export_dashboard(ROOT / "docs/data.js", result, pd.read_csv(ROOT / "data/ground_truth.csv"))

    m = result["metrics"]
    print(f"messages: {len(result['messages'])}  quotes parsed: {len(result['quotes'])}  "
          f"exceptions: {len(result['exceptions'])}")
    print(f"field accuracy: {m['field_accuracy_pct']}%  held: {m['held_quotes']}  "
          f"precision: {m['review_precision_pct']}%  recall: {m['anomaly_recall_pct']}%")
    print("wrote output/quotes.db, output/dq_report.xlsx, docs/data.js")


if __name__ == "__main__":
    main()
