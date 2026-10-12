"""Explicit operator bootstrap; verifies exact live backend before a v2-only CAS patch."""
import argparse
import copy
import json
import subprocess
import time
import tempfile
from pathlib import Path
from control_plane.state_v2 import Store, ServingPair, STATE_NAME, audit, numeric_version
from control_plane.state import Conflict
from control_plane.kserve_effects import KServeEffects, runtime_name, LANES


class KubectlV2:
    def command(self, *args, payload=None):
        result = subprocess.run(["kubectl", "--context", "ml-template-dev-sg", "--request-timeout=15s",
            "-n", "production", *args], input=payload, capture_output=True, text=True, timeout=30)
        if result.returncode:
            if "Conflict" in result.stderr or "test failed" in result.stderr or "test operation" in result.stderr:
                raise Conflict()
            raise RuntimeError("kubectl operation failed: " + result.stderr[:1000])
        return result.stdout

    def read_state(self):
        obj = json.loads(self.command("get", "configmap", STATE_NAME, "-o", "json"))
        return obj["metadata"]["resourceVersion"], json.loads(obj["data"]["state.json"])

    def write_state(self, revision, state):
        # JSON Patch tests resourceVersion atomically; preserve other CM data/metadata.
        patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": revision},
                 {"op": "replace", "path": "/data/state.json", "value": json.dumps(state, allow_nan=False)}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state-patch.json"
            path.write_text(json.dumps(patch), encoding="utf-8")
            self.command("patch", "configmap", STATE_NAME, "--type=json", "--patch-file=" + str(path))

    def get(self, kind, name):
        if kind not in ("inferenceservices", "servingruntimes") or not (
            name in LANES.values() if kind == "inferenceservices" else name.startswith("runtime-")):
            raise ValueError("Resource outside v2")
        raw = self.command("get", kind, name, "--ignore-not-found", "-o", "json")
        return json.loads(raw) if raw.strip() else None

    def get_deployment(self, lane):
        return json.loads(self.command('get', 'deployment', LANES[lane] + '-predictor', '-o', 'json'))

    def probe(self, url):
        code = "import json,sys;from urllib.request import urlopen; r=urlopen(sys.argv[1],timeout=5); b=r.read(1048577); assert len(b)<=1048576; print(json.dumps(json.loads(b) if b else {}))"
        return json.loads(self.command("exec", "deployment/credit-risk-stable-v2-predictor", "-c", "kserve-container",
                                      "--", "python", "-c", code, url))

    def inference(self):
        code = """import json, math
from pathlib import Path
from urllib.request import Request,urlopen
base='http://127.0.0.1:8080/v2/models/credit-risk-stable-v2'
with urlopen(base,timeout=5) as r: metadata=json.load(r)
example=json.loads(Path('/mnt/models/serving_input_example.json').read_text())['dataframe_split']
inputs=[]
for field in metadata['inputs']:
    column=example['columns'].index(field['name'])
    values=[row[column] for row in example['data']]
    values=[None if isinstance(x,float) and math.isnan(x) else x for x in values]
    inputs.append(dict(name=field['name'],datatype=field['datatype'],shape=[len(values),1],data=values))
body=json.dumps(dict(parameters={'content_type':'pd'},inputs=inputs),allow_nan=False).encode()
with urlopen(Request(base+'/infer',data=body,headers={'Content-Type':'application/json'}),timeout=10) as r:
    assert r.status==200
    print(json.dumps(json.load(r)))
"""
        return json.loads(self.command("exec", "deployment/credit-risk-stable-v2-predictor", "-c", "kserve-container",
                                      "--", "python", "-c", code))


def bootstrap(store, effects, pair, fingerprint, infer, *, operator, clock=time.time):
    pair = ServingPair(**pair).record()
    runtime_name(fingerprint)
    if not operator or len(operator) > 128 or any(ord(c) < 32 for c in operator):
        raise ValueError("Invalid operator")
    def mutate(state):
        if state["active"] or state["pending"] or state["lease"] or state["route"]["weight"] != 0:
            raise ValueError("Bootstrap requires idle state")
        existing = state["stable"] is not None
        if existing and (state["stable"] != pair or state["stable_fingerprint"] != fingerprint):
            raise ValueError("Conflicting stable identity")
        if state["highest_accepted"] > numeric_version(pair["model_version"]):
            raise ValueError("Bootstrap would regress version")
        if not effects.ready("stable", pair, fingerprint):
            raise ValueError("Stable backend not verified")
        prediction = infer()
        if prediction.get("model_name") != LANES["stable"] or prediction.get("outputs", [{}])[0].get("data") != [0, 0, 0]:
            raise ValueError("Stable native inference mismatch")
        if existing:
            return "already_bootstrapped"
        state["stable"] = copy.deepcopy(pair)
        state["stable_fingerprint"] = fingerprint
        state["highest_accepted"] = max(state["highest_accepted"], numeric_version(pair["model_version"]))
        audit(state, "operator_bootstrap", clock(), operator=operator, pair=pair, fingerprint=fingerprint)
        return "bootstrapped"
    return store.update(mutate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair-file", type=Path, required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--enable-live", action="store_true")
    args = parser.parse_args()
    if not args.enable_live: parser.error("--enable-live required")
    api = KubectlV2()
    result = bootstrap(Store(api), KServeEffects(api, None, None, probe=api.probe),
        json.loads(args.pair_file.read_text()), args.fingerprint, api.inference, operator=args.operator)
    print(json.dumps({"result": result, "state": Store(api).read()}))


if __name__ == "__main__": main()
