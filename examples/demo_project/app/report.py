"""Builds a text report out of many small strings."""


def format_line(o, scores):
    return f"{o['id']:>8} {o['region']:<6} {o['qty']:>4} x {o['price']:>8.2f} = {o['qty'] * o['price']:>10.2f} ({scores % 97})"


def build_report(orders, prices, scores):
    lines = [format_line(o, scores) for o in orders]
    lines.append(f"prices: {prices}")
    return "\n".join(lines)
