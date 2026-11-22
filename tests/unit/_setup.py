"""Shared setup for the unit tests: puts the app's folder first on sys.path and gives
helpers for building samples and fake objects."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# runs, tokens and the AI workspace of anything imported during the tests go to a temp folder,
# never into the repo or the user's own data
os.environ.setdefault("HCV_DATA_DIR", tempfile.mkdtemp(prefix="hcv-unit-data-"))


def samples(n, dt=0.05, **values):
    """n recorded samples, dt seconds apart, each with the given readings.
    A value may be a function of the sample index."""
    out = []
    for i in range(n):
        row = {"t": round(i * dt, 3)}
        for k, v in values.items():
            row[k] = v(i) if callable(v) else v
        out.append(row)
    return out
