"""Sequential node pipeline for the scheduled task: collect -> ops -> provide.

Each step runs even if a previous one failed: a failed collect still lets ops
refresh measurements on the existing table, and a failed ops round still lets
provide republish the last valid set.  Exit code is 1 if any step failed.
"""

import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
LOG = ROOT / "logs" / "node-pipeline.log"
PRODUCTION_PROVIDER = r"C:\mihomo\providers\node-valid.yaml"

STEPS = [
    [sys.executable, "-m", "node_provider", "--collect",
     "--sources-env", str(ROOT / "config" / "subscription.env"),
     "--from-app", "source4"],
    [sys.executable, "-m", "node_ops"],
    [sys.executable, "-m", "node_provider", "--output", PRODUCTION_PROVIDER],
]


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    failed = []
    with open(LOG, "a", encoding="utf-8") as log:
        for cmd in STEPS:
            label = " ".join(cmd[1:3])
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} START {label}\n")
            log.flush()
            proc = subprocess.run(cmd, cwd=SCRIPTS, stdout=log,
                                  stderr=subprocess.STDOUT)
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} EXIT {proc.returncode} {label}\n")
            log.flush()
            if proc.returncode != 0:
                failed.append(label)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())