# HCV demo project

A small project spread over several files and folders, for trying HCV Hot Spots in VS Code.
Open this folder in VS Code, open `main.py` and press the yellow ▶ in the editor title.

| File | What kind of slow code it has |
|---|---|
| `app/loader.py` | reads `data/orders.csv` (path relative to the project; written on the first run) and parses it in pure Python; holds a lot of memory |
| `app/compute.py` | a pure-Python loop (one core: serial code) next to the same sum in numpy, and a thread pool |
| `app/network.py` | waits (stands in for network calls): nothing is busy |
| `app/report.py` | builds many small strings |
