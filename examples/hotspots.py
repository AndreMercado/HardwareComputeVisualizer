"""Example for the hot-spot profiler: a few deliberately different kinds of slow code.
In VS Code run "HCV: Profile this file" and see which lines get boxed."""
import time

import numpy as np


def slow_python_sum(n):
    # pure-Python loop: one CPU core does all the work, one number at a time
    total = 0
    for i in range(n):
        total += i * i
    return total


def fast_numpy_sum(n):
    # the same sum, vectorised: numpy does it in compiled code
    a = np.arange(n, dtype=np.float64)
    return float((a * a).sum())


def build_strings(n):
    # lots of small Python objects: slow and memory-hungry
    return [str(i) * 3 for i in range(n)]


def wait_for_network():
    # stands in for a network call: nothing is busy, the program just waits
    time.sleep(1.0)


def main():
    t0 = time.time()
    words = build_strings(3_000_000)
    s1 = slow_python_sum(150_000_000)
    s2 = fast_numpy_sum(60_000_000)
    wait_for_network()
    print(f"{len(words)} strings, sums {s1} / {s2:.0f}, took {time.time() - t0:.2f} s")


if __name__ == "__main__":
    main()
