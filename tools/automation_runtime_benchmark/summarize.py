"""Print a compact comparison for runtime benchmark JSON files."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: summarize.py RESULT.json [...]")
    rows = []
    for raw_path in sys.argv[1:]:
        payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        median = payload["median_ms"]
        rows.append(
            (
                str(payload["runtime"]),
                float(median["base_ms"]),
                float(median["content_ms"]),
                float(median["audio_ms"]),
                float(median["total_ms"]),
            )
        )
    rows.sort(key=lambda row: row[-1])
    baseline = rows[0][-1]
    print("runtime\tbase_ms\tcontent_ms\taudio_ms\ttotal_ms\tvs_fastest")
    for runtime, base, content, audio, total in rows:
        print(
            f"{runtime}\t{base:.2f}\t{content:.2f}\t{audio:.2f}\t{total:.2f}\t{baseline / total:.3f}x"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
