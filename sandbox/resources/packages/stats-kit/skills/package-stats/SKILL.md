---
name: package-stats
description: Use the installed stats-kit package's python_stats tool to compute statistics and verify its Python output.
---

# Package statistics

Call `python_stats` with the user's numeric list. The tool executes the package's Python helper in the current session and writes `/workspace/package-stats.json`.

Read that file and report the result, including the Python executable path. If the tool is absent, report that the stats-kit package must be installed; do not substitute a guessed result.
