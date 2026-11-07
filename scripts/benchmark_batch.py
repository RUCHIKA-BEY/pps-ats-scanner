"""Benchmark batch throughput for different isolation/worker settings (synthetic data only).

Usage:  python scripts/benchmark_batch.py [--documents 60] [--repeats 2] [--workers 1 2 4]

Builds a synthetic submission folder in the system temp directory (outside OneDrive),
runs the batch with each configuration and prints seconds per document. Use the result
to decide `[batch] workers`; the default stays 1 unless a run shows a clear gain.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from pps_ats.batch.runner import BatchOptions, run_batch
from pps_ats.config import load_settings
from pps_ats.devtools.synthetic_batch import build_batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--documents", type=int, default=60)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json", type=Path, help="also write the results here")
    args = parser.parse_args()

    settings = load_settings()
    root = Path(tempfile.mkdtemp(prefix="pps_bench_"))
    try:
        src, manifest = build_batch(root, copies=max(1, args.documents - 10))
        configs = [("inline (no timeout)", 1, 0.0)] + [
            (f"process x{w} (timeout {args.timeout:g}s)", w, args.timeout) for w in args.workers
        ]
        results = []
        for label, workers, timeout in configs:
            runs = []
            for r in range(args.repeats):
                out = root / f"out_{workers}_{timeout:g}_{r}"
                t = time.perf_counter()
                summary = run_batch(
                    BatchOptions(src, out, manifest, workers=workers, timeout_s=timeout),
                    settings,
                    lambda _: None,
                )
                wall = time.perf_counter() - t
                runs.append(wall / summary.total)
                shutil.rmtree(out, ignore_errors=True)
            results.append(
                {
                    "config": label,
                    "documents": summary.total,
                    "seconds_per_document": round(statistics.median(runs), 3),
                    "runs": [round(x, 3) for x in runs],
                }
            )
            print(
                f"{label:<32} {results[-1]['seconds_per_document']:.3f} s/doc  {runs}",
                file=sys.stderr,
                flush=True,
            )
        if args.json:
            args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
