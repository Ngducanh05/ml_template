"""Restart-safe artifact metadata resolution, independent of SQS receipt ownership."""
import copy
import time
from urllib.parse import urlsplit
from .runtime_resolver import ArtifactContract, normalize_requirements, parse_python_version, runtime_fingerprint, RUNTIME_X_V1, RUNTIME_CI_V2
from .build_contract import build_request
from .artifact_metadata import validate_mlmodel
from .state_v2 import storage_uri, ServingPair, audit
from .telemetry import log


class S3Metadata:
    def __init__(self, s3):
        self.s3 = s3

    def inspect(self, uri):
        u = urlsplit(storage_uri(uri))
        files = {}
        for filename in ("python_env.yaml", "requirements.txt", "MLmodel"):
            response = self.s3.get_object(Bucket=u.netloc, Key=u.path.lstrip("/") + "/" + filename)
            body = response["Body"]
            try:
                data = body.read(65537)
            finally:
                body.close()
            if len(data) > 65536:
                raise ValueError("Artifact metadata exceeds bound")
            files[filename] = data.decode("utf-8-sig")
        contract = ArtifactContract(parse_python_version(files["python_env.yaml"]),
                                    normalize_requirements(files["requirements.txt"]))
        validate_mlmodel(files["MLmodel"], contract)
        return contract


class Processor:
    def __init__(self, store, source, catalog, clock=time.time, max_attempts=5):
        self.store, self.source, self.catalog = store, source, catalog
        self.clock, self.max_attempts = clock, max_attempts

    def tick(self):
        eligible = [x for x in self.store.read()["pending"]
                    if x["status"] in ("ACCEPTED", "RESOLUTION_RETRY", "BUILD_REQUIRED")
                    and x["next_attempt"] <= self.clock()]
        if not eligible:
            return None
        snapshot = copy.deepcopy(eligible[0])
        event = snapshot["event"]
        log("PROCESSOR_STARTED", model_version=event["model_version"])
        try:
            contract = self.source.inspect(event["artifact_uri"])
            profile = RUNTIME_X_V1
            fingerprint = runtime_fingerprint(contract, profile)
            image = self.catalog.lookup(fingerprint)
            if image is None:
                # A new recipe must not inherit the identity of historical builds.
                profile = RUNTIME_CI_V2
                fingerprint = runtime_fingerprint(contract, profile)
                image = self.catalog.lookup(fingerprint)
            result = dict(status="READY_FOR_RELEASE" if image else "BUILD_REQUIRED",
                          fingerprint=fingerprint, build_request=build_request(contract, profile),
                          next_attempt=self.clock() + 900, attempts=0)
            if image:
                result["pair"] = ServingPair(event["model_version"], image, event["artifact_uri"]).record()
        except Exception as exc:
            attempts = snapshot["attempts"] + 1
            result = dict(status="RESOLUTION_BLOCKED" if attempts >= self.max_attempts else "RESOLUTION_RETRY",
                          attempts=attempts, next_attempt=self.clock() + min(900, 15 * 2**attempts),
                          error_type=type(exc).__name__)
        def commit(state):
            current = next((x for x in state["pending"] if x["event"] == event), None)
            # Concurrent processors may inspect twice, but only one may transition
            # this exact snapshot; deployment is never a processor side effect.
            if current != snapshot:
                return "superseded"
            current.update(result)
            audit(state, result["status"], self.clock(), model_version=event["model_version"])
            return result["status"]
        status = self.store.update(commit)
        log("PROCESSOR_RESULT", outcome=status, model_version=event["model_version"])
        return status
