"""One-file launcher for the multi-asset paper-trading demo.

Run from this folder with:
    python main.py
    python main.py --events 3000 --audit artifacts/my-run.jsonl
"""

from __future__ import annotations

import sys
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))
os.chdir(PROJECT_ROOT)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "catalog":
        from quantpaper.research.catalog_cli import main as catalog_main

        raise SystemExit(catalog_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "refresh-data":
        from quantpaper.research.refresh_cli import main as refresh_main

        raise SystemExit(refresh_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "evidence":
        from quantpaper.research.evidence_cli import main as evidence_main

        raise SystemExit(evidence_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "marketdata":
        from quantpaper.marketdata.cli import main as marketdata_main

        raise SystemExit(marketdata_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "research":
        from quantpaper.research.cli import main as research_main

        raise SystemExit(research_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "ml":
        from quantpaper.ml.cli import main as ml_main

        raise SystemExit(ml_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "ml2":
        from quantpaper.ml.cli2 import main as ml2_main

        raise SystemExit(ml2_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "alpaca":
        from quantpaper.alpaca_cli import main as alpaca_main

        raise SystemExit(alpaca_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "warehouse":
        from quantpaper.warehouse_cli import main as warehouse_main

        raise SystemExit(warehouse_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "sources":
        from quantpaper.sources_cli import main as sources_main

        raise SystemExit(sources_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "shadow":
        from quantpaper.shadow_cli import main as shadow_main

        raise SystemExit(shadow_main(sys.argv[2:]))

    from quantpaper.cli import main as paper_main

    raise SystemExit(paper_main(["demo", *sys.argv[1:]]))
