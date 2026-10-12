"""Write security evidence before returning a failing gate exit status."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from control_plane.runtime_security import evaluate_scan, blocked_integrity, load_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('request', 'scan', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--waivers', type=Path, default=Path(__file__).resolve().parents[1]/'security/runtime-vulnerability-waivers.json')
    args = parser.parse_args()
    try:
        decision = evaluate_scan(load_json(args.scan), load_json(args.request), load_json(args.waivers), now=datetime.now(timezone.utc))
    except (OSError, ValueError) as exc:
        decision = blocked_integrity(type(exc).__name__ + ': unable to read valid policy inputs')
    args.output.write_text(json.dumps(decision, sort_keys=True, indent=2)+'\n')
    print(json.dumps({'decision': decision['decision'], 'counts': decision['counts']}))
    return 0 if decision['decision'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
