"""Stands in for calls to a web service: the program only waits."""
import time


def fetch_prices(n):
    prices = []
    for batch in range(8):
        time.sleep(0.15)  # one request per batch, nothing busy meanwhile
        prices.append(batch * 1.5)
    return prices
