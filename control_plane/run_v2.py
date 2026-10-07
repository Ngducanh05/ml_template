"""Explicit v2 process entrypoints. No deployment or live activation on import."""
import argparse
import os
import time
from .telemetry import log


def aws_clients():
    import boto3
    from botocore.config import Config
    if not all(os.environ.get(k) for k in ("AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")):
        raise RuntimeError("Pod Identity required")
    if any(os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE")):
        raise RuntimeError("Static/profile/alternate credentials are prohibited")
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    config = Config(connect_timeout=5, read_timeout=25, retries={"mode": "standard", "total_max_attempts": 3})
    return lambda service: boto3.client(service, region_name="ap-southeast-1", config=config)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("consumer", "processor", "controller", "router", "reconciler"))
    parser.add_argument("--enable-live", action="store_true", help="Explicitly enable external effects; deployment approval still required")
    args = parser.parse_args()
    if not args.enable_live:
        parser.error("Live effects disabled; use deterministic tests or explicitly enable after reviewing isolation/RBAC")
    from .kube_v2 import KubeV2
    from .state_v2 import Store
    store = Store(KubeV2()); store.read()
    if args.role == "router":
        from .router_v2 import main as router_main
        return router_main()
    if args.role == "consumer":
        from .consumer_v2 import poll_once
        sqs = aws_clients()("sqs")
        tick = lambda: poll_once(sqs, store)
    elif args.role == "reconciler":
        from .reconciliation_v2 import Reconciler, s3_pointer
        s3 = aws_clients()("s3")
        tick = Reconciler(store, lambda: s3_pointer(s3)).tick
    elif args.role == "processor":
        from .processor_v2 import Processor, S3Metadata
        from .runtime_catalog import Catalog
        client = aws_clients()
        tick = Processor(store, S3Metadata(client("s3")), Catalog(client("ecr"))).tick
    else:
        from .controller_v2 import Controller
        from .kserve_effects import KServeEffects
        from .observations_v2 import Observations
        # No fallback to legacy metrics/router. URLs must be provided explicitly.
        observe = Observations(os.environ["V2_PROMETHEUS_URL"], os.environ["V2_ROUTER_URL"])
        tick = Controller(store, KServeEffects(KubeV2(), observe.router, observe.metrics)).tick
    while True:
        try: tick()
        except Exception as exc: log("V2_COMPONENT_RETRY", component=args.role, error_type=type(exc).__name__)
        time.sleep(3)


if __name__ == "__main__": main()
