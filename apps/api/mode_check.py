from __future__ import annotations

import json
import sys
from pathlib import Path


API_ROOT = Path(__file__).resolve().parent
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.db import SessionLocal, init_db  # noqa: E402
from app.mode_guard import compute_mode_report  # noqa: E402


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        report = compute_mode_report(db=db, include_model_probe=True)
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
