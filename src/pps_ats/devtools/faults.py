"""Fault injection for batch tests (never used in production runs).

``process_with_faults`` is a drop-in replacement for ``pps_ats.batch.worker:process`` that
misbehaves based on the file name, so timeouts and worker crashes can be tested in real
worker processes:

* ``*HANG*``  - never returns (simulates a stuck parser)
* ``*SLOW*``  - sleeps 3 s, then processes normally
* ``*DIE*``   - the worker process exits abruptly (simulates a native crash)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from pps_ats.batch import worker


def process_with_faults(job, settings, manifest):  # type: ignore[no-untyped-def]
    name = Path(job.path).name.upper()
    if "HANG" in name:
        while True:
            time.sleep(0.2)
    if "DIE" in name:
        os._exit(3)
    if "SLOW" in name:
        time.sleep(3)
    return worker.process(job, settings, manifest)
