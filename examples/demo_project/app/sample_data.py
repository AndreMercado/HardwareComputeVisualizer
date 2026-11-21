"""Writes data/orders.csv the first time the demo runs (the same rows every time)."""
import os
import random


def ensure_orders(path, rows=60000):
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rnd = random.Random(1)
    regions = ["north", "south", "east", "west"]
    with open(path, "w", newline="") as f:
        for i in range(rows):
            f.write(f"{i},{rnd.randint(1, 20)},{rnd.uniform(1, 300):.2f},{rnd.choice(regions)}\n")
