---
name: python-stats
description: Compute descriptive statistics by writing and running Python in the current isolated session. Use for count, sum, mean, minimum, and maximum of a numeric list.
---

# Python statistics

Use the numbers in the user's request; ask for numbers if none were provided.

1. Write `/workspace/analysis.py` using Python's standard library. Reject non-finite numbers and empty input.
2. Calculate count, sum, mean, minimum, and maximum. Print one JSON object and save the same object to `/workspace/analysis.json`.
3. Run the file with `python /workspace/analysis.py`, which uses the session's venv.
4. Report the actual execution output and the generated file paths. Do not invent results or read another session's files.

For a quick check use `[2, 4, 6, 8]`; its mean is 5.
