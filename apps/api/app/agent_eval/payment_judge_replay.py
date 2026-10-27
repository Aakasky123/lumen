"""Replay the payment-floor judge on labelled cases (recorded non-payments from runs 1804-1807, synthetic payments).

python -m app.agent_eval.payment_judge_replay [repeat] [user_id]  -> false payments and missed payments (missed must be 0).
Calls the active model provider.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CASES = Path(__file__).with_name("payment_judge_cases.json")


def main() -> None:
    from app.lumen_model import LumenModelClient
    from app.db import SessionLocal

    repeat = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    false_payments = missed = negatives = positives = 0
    with SessionLocal() as db:
        client = LumenModelClient(db, user_id=int(sys.argv[2]) if len(sys.argv) > 2 else 1)
        for case in cases:
            for _ in range(repeat):
                got = client.judge_payment_final_action(viewport_text=case["viewport_text"], submit_label=case["label"])["payment"]
                if case["payment"]:
                    positives += 1
                    missed += 0 if got else 1
                else:
                    negatives += 1
                    false_payments += 1 if got else 0
    print(f"false payments {false_payments}/{negatives}; missed payments {missed}/{positives}")


if __name__ == "__main__":
    main()
