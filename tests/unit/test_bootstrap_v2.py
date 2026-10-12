import copy
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from control_plane.state_v2 import Store, initial_state, REPOSITORY, Busy
from scripts.bootstrap_v2 import bootstrap, KubectlV2
from test_state_v2 import Backend

PAIR = dict(model_version="3", runtime_digest=REPOSITORY+"@sha256:"+"a"*64,
    storage_uri="s3://mlflow-test-artifacts-quangcm/mlflow/workspaces/mlops/27/models/m-3/artifacts")
FP = "sha256:"+"b"*64


def run(backend, effects=None, infer=None, pair=None, fp=FP):
    return bootstrap(Store(backend), effects or Mock(ready=Mock(return_value=True)), pair or PAIR, fp,
        infer or Mock(return_value={"model_name":"credit-risk-stable-v2","outputs":[{"data":[0,0,0]}]}),
        operator="test", clock=lambda: 10)


def test_bootstrap_cas_preserves_state_and_rechecks_backend_after_conflict():
    b=Backend(); b.conflicts=2
    b.state["audit"].append(dict(reason="existing",time=0))
    effects=Mock(); effects.ready.return_value=True
    assert run(b,effects)=="bootstrapped"
    assert effects.ready.call_count==3
    assert b.state["stable"]==PAIR and b.state["stable_fingerprint"]==FP
    assert b.state["highest_accepted"]==3 and b.state["route"]==dict(weight=0,revision=0)
    assert [a["reason"] for a in b.state["audit"]]==["existing","operator_bootstrap"]
    before=copy.deepcopy(b.state)
    assert run(b)=="already_bootstrapped" and b.state==before


@pytest.mark.parametrize("case", ["unready","metadata","prediction","name","conflict","fingerprint","regress","pending","lease","route","legacy","invalid-digest"])
def test_bootstrap_fails_without_changing_state(case):
    b=Backend(); effects=Mock(); effects.ready.return_value=True
    infer=Mock(return_value={"model_name":"credit-risk-stable-v2","outputs":[{"data":[0,0,0]}]})
    pair=copy.deepcopy(PAIR)
    if case=="unready": effects.ready.return_value=False
    elif case=="metadata": effects.ready.side_effect=ValueError("metadata mismatch")
    elif case=="prediction": infer.return_value["outputs"][0]["data"]=[1]
    elif case=="name": infer.return_value["model_name"]="other"
    elif case=="conflict": b.state=initial_state({**PAIR,"model_version":"2"},FP)
    elif case=="fingerprint": b.state=initial_state(PAIR,"sha256:"+"c"*64)
    elif case=="regress": b.state["highest_accepted"]=4
    elif case=="pending": b.state["pending"]=[dict(status="ACCEPTED")]
    elif case=="lease": b.state["lease"]={"owner":"another"}
    elif case=="route": b.state["route"]["weight"]=10
    elif case=="legacy": b.state["schema_version"]=1
    elif case=="invalid-digest": pair["runtime_digest"]="runtime:latest"
    before=copy.deepcopy(b.state)
    with pytest.raises(ValueError): run(b,effects,infer,pair)
    assert b.state==before


def test_cas_exhaustion_never_bootstraps():
    b=Backend(); b.conflicts=20
    with pytest.raises(Busy): run(b)
    assert b.state==initial_state()


def test_adapter_patches_only_state_with_resource_version_guard():
    api=KubectlV2()
    def capture(*args,**kwargs):
        assert args[:4]==("patch","configmap","model-release-state-v2","--type=json")
        patch=json.loads(Path(args[4].split("=",1)[1]).read_text())
        assert patch[0]==dict(op="test",path="/metadata/resourceVersion",value="42")
        assert len(patch)==2 and patch[1]["path"]=="/data/state.json"
        assert json.loads(patch[1]["value"])==initial_state()
    api.command=capture
    api.write_state("42",initial_state())
