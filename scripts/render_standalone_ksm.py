"""Render only standalone KSM resources with an immutable image identity."""
import argparse
import json
import re
from pathlib import Path
import yaml


def render(image):
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("Immutable image digest required")
    source = Path(__file__).resolve().parents[1] / "k8s/monitoring/standalone/ksm/resources.yaml"
    return {"apiVersion": "v1", "kind": "List", "items": list(yaml.safe_load_all(source.read_text().replace("IMAGE_DIGEST_REQUIRED", image)))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = render(args.image)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
