"""Render isolated v2 objects as JSON; never contacts or mutates Kubernetes."""
import argparse
import json
from pathlib import Path
from control_plane.state_v2 import initial_state, STATE_NAME, ServingPair
from control_plane.kserve_effects import runtime_manifest, lane_manifest


def state_manifest():
    return dict(apiVersion="v1", kind="ConfigMap", metadata=dict(name=STATE_NAME, namespace="production"),
                data={"state.json": json.dumps(initial_state(), separators=(",", ":"))})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--fingerprint")
    p.add_argument("--runtime-image")
    p.add_argument("--storage-uri")
    p.add_argument("--model-version")
    args = p.parse_args()
    objects = [state_manifest()]
    values = (args.fingerprint, args.runtime_image, args.storage_uri, args.model_version)
    if any(values):
        if not all(values): p.error("All serving identity fields are required together")
        pair = ServingPair(args.model_version, args.runtime_image, args.storage_uri).record()
        objects += [runtime_manifest(args.fingerprint, args.runtime_image)]
        objects += [lane_manifest(lane, pair, args.fingerprint) for lane in ("stable", "canary")]
    args.output.write_text(json.dumps(dict(apiVersion="v1", kind="List", items=objects), indent=2), encoding="utf-8")


if __name__ == "__main__": main()
