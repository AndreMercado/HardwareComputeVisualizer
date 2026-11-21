"""HCV demo: a small pipeline over several modules. Press the yellow ▶ to run it with every colour."""
import time

from app.loader import load_orders
from app.compute import totals_slow, totals_fast, score_in_threads
from app.network import fetch_prices
from app.report import build_report
from app.sample_data import ensure_orders


def main():
    ensure_orders("data/orders.csv")  # first run: writes the sample data
    t0 = time.time()
    orders = load_orders("data/orders.csv")
    prices = fetch_prices(len(orders))
    slow = totals_slow(orders)
    fast = totals_fast(orders)
    scores = score_in_threads(orders)
    text = build_report(orders, prices, scores)
    print(f"{len(orders)} orders · totals {slow:.0f} / {fast:.0f} · report {len(text) // 1024} KB · "
          f"{time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
