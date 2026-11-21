"""The same totals two ways, and some work spread over threads."""
from concurrent.futures import ThreadPoolExecutor

import numpy as np


def totals_slow(orders):
    # one CPU core does all of it, one number at a time: serial code
    total = 0.0
    for _ in range(12):
        for o in orders:
            total += o["qty"] * o["price"]
    return total / 12


def totals_fast(orders):
    # the same numbers in numpy: compiled code, a fraction of the time
    qty = np.fromiter((o["qty"] for o in orders), dtype=np.float64, count=len(orders))
    price = np.fromiter((o["price"] for o in orders), dtype=np.float64, count=len(orders))
    return float((qty * price).sum())


def _score(chunk):
    return sum((o["qty"] * 31 + o["id"]) % 97 for o in chunk)


def score_in_threads(orders, workers=4):
    # threads: Python code holds the GIL, so 4 threads still use about one core
    step = len(orders) // workers + 1
    chunks = [orders[i:i + step] for i in range(0, len(orders), step)]
    with ThreadPoolExecutor(workers) as ex:
        return sum(ex.map(_score, chunks * 3))
