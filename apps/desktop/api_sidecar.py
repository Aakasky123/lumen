from __future__ import annotations

import os
import sys
import traceback
from multiprocessing import freeze_support
from pathlib import Path

import uvicorn

if not getattr(sys, "frozen", False):
    API_ROOT = Path(__file__).resolve().parents[1] / "api"
    sys.path.insert(0, str(API_ROOT))

from app.main import app


def main() -> None:
    freeze_support()
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8001,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        data_root = Path(os.environ.get("LUMEN_DATA_ROOT") or Path.cwd())
        data_root.mkdir(parents=True, exist_ok=True)
        (data_root / "lumen-api-startup-error.log").write_text(
            traceback.format_exc(),
            encoding="utf-8",
        )
        raise
