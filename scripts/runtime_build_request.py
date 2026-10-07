"""Validate a CI request; intentionally fail closed until a pinned recipe exists."""
import argparse
import json
from pathlib import Path
from control_plane.build_contract import validate_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    data = json.loads(args.request.read_text(encoding="utf-8-sig"))
    validate_request(data)
    print(json.dumps(dict(status="VALIDATED", fingerprint=data["fingerprint"])))
    if not args.validate_only:
        raise SystemExit("BUILD_REQUIRED: reproducible generic recipe and approved CI trigger are not yet configured; no image or eligibility tag published")


if __name__ == "__main__": main()
