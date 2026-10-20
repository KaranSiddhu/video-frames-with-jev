"""Recompute run measurements from saved records, without a key or network."""

import argparse
import json
from pathlib import Path

from domain import summarize, usable_box


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Saved run directory or exported run JSON")
    path = parser.parse_args().path
    if path.is_dir():
        manifest = json.loads((path / "manifest.json").read_text())
        records = [json.loads(p.read_text()) for p in sorted(path.glob("[0-9][0-9][0-9][0-9].json"))]
    else:
        exported = json.loads(path.read_text())
        manifest, records = exported["manifest"], exported["frames"]
    recomputed = summarize(records)
    print(json.dumps({
        "run_id": manifest["run_id"], "mode": manifest["settings"]["mode"],
        "recomputed": recomputed,
        "matches_saved_summary": recomputed == manifest.get("summary"),
        "ungated_structurally_usable_boxes": sum(usable_box(r.get("response", {}).get("answers", {}).get("person", {}).get("box")) for r in records),
        "gated_overlays": sum(r["decision"]["draw"] for r in records),
        "accuracy": "Not calculated: no human ground-truth labels in viewer logs.",
    }, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
