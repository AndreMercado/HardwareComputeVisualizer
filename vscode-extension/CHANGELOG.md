# Changelog

## 0.2.0
- Packaged as a normal extension (.vsix): the profiler ships inside it, no setup script.
- Yellow ▶: a normal run in a terminal with every colour (speed, memory, crash).
- Eye menu: choose what to show (speed, memory, every line, function totals, bottlenecks, hardware).
- Lines under each other form one box; memory highlights ranked like speed.
- Bottlenecks tied to lines, hardware per line, run report with links into the code.
- Works across project folders (src/ layouts, packages, relative imports, venvs).
- One-click install of the optional Python tools (psutil, py-spy, nvidia-ml-py).

## 0.1.0
- First version: boxes around the slowest code.
