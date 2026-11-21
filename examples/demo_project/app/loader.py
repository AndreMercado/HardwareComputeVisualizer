"""Reads the orders file. The path is relative to the project folder, like VS Code's own Run."""
import csv


def parse_row(row):
    # pure Python, one row at a time
    return {"id": int(row[0]), "qty": int(row[1]), "price": float(row[2]), "region": row[3]}


def load_orders(path):
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    orders = [parse_row(r) for r in rows for _ in range(6)]  # many dicts: memory piles up here
    return orders
