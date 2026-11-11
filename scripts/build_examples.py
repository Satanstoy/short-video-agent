from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from short_video.runner import run_agent  # noqa: E402

CASES = [
    ("01-normal", []),
    ("02-over-duration", []),
    ("03-missing-info", []),
    ("04-no-material", []),
    ("05-tool-failure", ["estimate_duration:2"]),
    ("06-impossible-duration", []),
    ("07-target-35", []),
]


def main() -> None:
    for example, faults in CASES:
        example_dir = ROOT / "examples" / example
        for name in ("output.json", "trace.json", "scripts.json"):
            (example_dir / name).unlink(missing_ok=True)
        command = (
            f"python3 run.py run --input examples/{example}/input.json "
            f"--state-dir examples/{example} --engine offline"
        )
        if faults:
            command += " " + " ".join(f"--fault {fault}" for fault in faults)
        output, _ = run_agent(
            input_path=example_dir / "input.json",
            data_dir=ROOT / "data",
            state_dir=example_dir,
            engine_name="offline",
            faults=faults,
        )
        manifest = {
            "command": command,
            "engine": "offline",
            "model": "reference-policy",
            "faults": faults,
            "expected": output["task_status"],
            "final_duration_sec": output["estimated_total_sec"],
        }
        (example_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(example, output["task_status"])
    # One duplicate-save example: the same script id must not create two records.
    example_dir = ROOT / "examples" / "01-normal"
    (example_dir / "output.json").rename(example_dir / "output-run1.json")
    (example_dir / "trace.json").rename(example_dir / "trace-run1.json")
    output2, trace2 = run_agent(
        input_path=example_dir / "input.json",
        data_dir=ROOT / "data",
        state_dir=example_dir,
        engine_name="offline",
        faults=[],
    )
    (example_dir / "output-run2.json").write_text(
        json.dumps(output2, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (example_dir / "trace-run2.json").write_text(
        json.dumps(trace2, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (example_dir / "output.json").unlink(missing_ok=True)
    (example_dir / "trace.json").unlink(missing_ok=True)
    print("01-normal duplicate", output2["script"]["save_status"])


if __name__ == "__main__":
    main()
