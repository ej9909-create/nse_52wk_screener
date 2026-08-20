"""
Build the daily screener snapshot.

Runs the heavy price fetch + base-metric computation for the whole universe (both
bases) and writes data/screener_snapshot.csv. The web app then just READS this
snapshot and applies filters in memory — so the app never does heavy work at
request time (this is what keeps it reliable on a small host).

Runs in GitHub Actions after market close; commit the CSV. Can also be run
locally.

    python build_snapshot.py
"""

import os
import sys

# Cap numpy/BLAS thread pools before importing pandas (keeps CI/host lean).
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

import nse_screener as s


def main():
    uni = s.load_universe()
    print(f"universe: {len(uni)} symbols — computing snapshot (both bases)…")

    done_marker = {"n": 0}

    def cb(done, total):
        # print progress every ~10 batches so CI logs stay readable
        if done // 500 != done_marker["n"] // 500 or done == total:
            print(f"  {done}/{total}")
        done_marker["n"] = done

    snap, stats = s.compute_snapshot(uni, progress_cb=cb)
    if snap.empty or stats.fetched == 0:
        print("ERROR: snapshot empty; keeping previous file", file=sys.stderr)
        sys.exit(1)

    snap.to_csv(s.SNAPSHOT_PATH, index=False)
    print(f"Wrote {len(snap)} rows to {s.SNAPSHOT_PATH} "
          f"(as of {stats.as_of}); priced {stats.fetched}, "
          f"skipped no-data {len(stats.skipped_no_data)}, "
          f"short-history {len(stats.skipped_short_history)}")


if __name__ == "__main__":
    main()
