"""A package helper; the same file is called by its extension tool and command."""

import json
import math
import statistics
import sys
from pathlib import Path

numbers = json.loads(sys.argv[1])
if not isinstance(numbers, list) or not 1 <= len(numbers) <= 1000:
    raise ValueError("Expected 1-1000 numbers")
if any(
    isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in numbers
):
    raise ValueError("Expected finite numbers")
result = {
    "count": len(numbers),
    "sum": sum(numbers),
    "mean": statistics.mean(numbers),
    "min": min(numbers),
    "max": max(numbers),
    "python": sys.executable,
}
text = json.dumps(result, allow_nan=False)
Path("/workspace/package-stats.json").write_text(text + "\n")
print(text)
