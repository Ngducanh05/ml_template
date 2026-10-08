# ServingPair release control plane v2

The local automated path is implemented separately from the live legacy stack:

`model_promoted -> CAS ingestion -> independent processor -> ECR catalog -> READY_FOR_RELEASE -> ServingPair controller -> KServe effects -> V2 router -> observations -> promotion/rollback`.

The current live path is **not automated end to end**. Gate 1 is still blocked before model download by S3 HeadBucket403. No new consumer/controller/router is running. No real SQS message was received during development.

## Ownership and entrypoints

- `state_v2.py`: strict schema-v1 ingestion, bounded schema-v2 state, highest accepted numeric version, immutable producer identity conflicts, resourceVersion/CAS retries. Empty state requires an explicitly verified KServe stable pair before releases can start; it never migrates legacy state.
- `consumer_v2.py`: validate and durably classify before deleting the current SQS receipt. Invalid/conflicting messages stay on native retry/DLQ handling. No build/deploy in receipt ownership.
- `processor_v2.py`: bounded S3 reads of python_env.yaml, requirements.txt and MLmodel only; approved sklearn/skops surface checks; shared technical fingerprint; ECR HIT -> exact pair, MISS -> BUILD_REQUIRED. Operational failures retry with persisted backoff then RESOLUTION_BLOCKED.
- `runtime_catalog.py`: ECR-only catalog with immutable tags, exact repository/account validation, not-found-only MISS, publication collision checks. No ConfigMap catalog mirror.
- `build_contract.py` and `runtime-build.yml`: validate normalized technical request and recompute fingerprint; workflow intentionally fails closed because a reproducible generic build recipe and approved trigger are not yet established. No in-cluster builder or GitHub token was introduced.
- `kserve_effects.py`/`kube_v2.py`: namespace production; deterministic runtime-<40 fingerprint hex> with full fingerprint annotation, immutable image, owner marker, configuration drift/collision rejection. Lanes are credit-risk-stable-v2 / credit-risk-canary-v2. Readiness checks exact pair/runtime, observed generation, Ready, server health, model readiness and metadata. Internal addresses come from ISVC status. No runtime mutation/deletion.
- `controller_v2.py`: lease, CAS, PREPARING/RAMP/FINALIZING/DRAINING/ROLLBACK/BLOCKED;35s dwell,300s deadline,min1 observation,error<.05,p95<.1 from existing Policy.0/10/25/50/100 routing. No traffic waits; invalid observed metrics fail closed. Finalization transport errors retry until deadline. Rollback restores recorded pair and fingerprint without resolver lookup; previous pair remains durable until completion.
- `router_v2.py`: separate process, leaves both legacy routers intact. Public POST /v2/models/credit-risk-classifier/infer. Only URL model component maps to KServe's lane-specific injected model name; body bytes/features/nulls, status, response bytes and content type pass through. X-Release-Track and existing metric names are retained. No old FastAPI schema translation.
- `reconciliation_v2.py`:900s scheduler, same parser/ingestion. Actual read-only champion.json was verified as schema-v1 model_promoted. Errors are bounded/logged.
- `run_v2.py`: explicit consumer/processor/reconciler/controller/router process roles. External effects require --enable-live. AWS roles require Pod Identity and reject static/profile/alternate credentials; EC2 metadata fallback disabled.

## Tests and reproducible local checks

From C:\ml_template:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest tests/unit/test_release_v2_e2e.py tests/unit/test_router_v2_http.py tests/unit/test_v2_render_and_fixture.py -q
```

Dry E2E crosses real ingestion/Store/processor/Catalog/controller with fake effects and AWS responses; covers promotion, exact rollback, duplicate/stale events, restart and BUILD_REQUIRED. A separate real loopback HTTP test verifies unchanged V2 bytes. Metadata-only fixtures reproduce the actual v3 fingerprint. No model bytes are test dependencies.

`Dockerfile.control-plane-v2` packages v2 dependencies separately. It has NOT been built, scanned, pushed or deployed in this session. The existing Runtime-X must NOT be used as a control-plane image.

## Current ECR eligibility

Fingerprint sha256:700096f6d55f8aa9b56c9049e1c4171633f403a8d3e2a2de2df1ff544eab63fc is seeded as runtime-fp-700096f6d55f8aa9b56c9049e1c4171633f403a8d3e2a2de2df1ff544eab63fc.

It resolves to596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service@sha256:f1ae0d6c95d19b73adbfa72ae183ef6f207a2dc647bc2d4a1a36386a02ba5ae0.

No rebuild/repush. Security remains PASS WITH DOCUMENTED RISK ACCEPTANCE, not clean. The16exact exceptions expire2026-10-21. ECR tags do not automatically expire; eligibility must be reviewed before that date. The fingerprint identifies a reviewed build/serving profile, not a substitute for reviewing future profiles, artifact loaders or security changes.

## Safe activation boundary — unfinished live work

1. Resolve resource-side S3 access with the owner; do not broaden IAM or modify artifact to bypass HeadBucket. Reverify existing Gate1download, model Ready, metadata and V2 inference without rebuilding Runtime-X.
2. Build/test/scan a new **control-plane-v2** image before using `python -m scripts.render_control_plane_v2 --image <control-plane repository@digest> --output <file>`. Rendered replicas are all0; rendering is not permission to activate.
3. Review/apply least-privilege v2 RBAC and provision approved Pod Identity associations for processor/reconciler metadata/catalog reads. Existing consumer SA is reused for SQS ingestion; artifact-reader remains separate. No IAM role/association was added here. Namespace create permissions cannot be resourceName-restricted by Kubernetes; effects enforce name/owner bounds. Do not grant legacy-state write access to new processes.
4. Establish an isolated KServe stable baseline and verify exact pair/readiness before CAS-initializing stable + stable_fingerprint. The live model-release-state-v2 currently has stable=null,pending=[],active=null,weight0. Do not apply rendered initial state over it. No bootstrap mutation command is supplied until the baseline is actually verified.
5. Add a real Prometheus scrape job named credit-risk-router-v2 for the isolated router. Observation queries intentionally select that job and cannot use legacy traffic. No live metrics/dashboard evidence is claimed.
6. Verify backend lane model naming in real MLServer metadata; currently based on verified KServe model-name injection and local builder/HTTP tests, not successful live Gate1serving.
7. Activate isolated components only after reviewing effects/RBAC and queue ownership. Do not run a second competing queue consumer or redirect the legacy Stable path. Real SQS-to-release and KServe Canary promotion/rollback remain unproven.

BUILD_REQUIRED CI still needs a pinned reproducible runtime recipe, functional validation, exact-image security review, candidate push, and eligibility publication. No automatic cluster-to-GitHub trigger exists. The workflow cannot build or publish until this is implemented and approved.

Processor RESOLUTION_BLOCKED supports explicit audited CAS recovery after the operational cause is fixed; none is auto-requeued. Controller BLOCKED retains previous pair and resumes recovery checks, never falsely reports success. Bounded history means older versions outside the retained identity window are classified stale by the permanent high-water mark rather than retaining an unbounded conflict ledger.

## Evidence

External evidence directory:
C:\Users\Admin\.codex\visualizations\2026\10\06\01a10f41-2b35-7c93-970a-b58ad0977ecc\runtime-x-security

Key files: catalog-seed-result.json, v3-live-catalog-reuse.json, champion-pointer-readonly.json, v2-validation-manifests.json, v2-empty-state.json. Only the empty v2 ConfigMap was created live; serving objects were server dry-run only. Existing state/resources/Iris/controllers/IAM/SQS/artifacts remain unchanged.


## Cập nhật CI — 2026-10-08

Đã bổ sung guards danh tính ServiceAccount/process/RoleBinding và security context cho cả năm workload v2. Negative tests kiểm tra 90 trường hợp sai lệch. Focused CI: 108 passed; toàn bộ regression: 670 passed, 2 cảnh báo dependency đã biết. Offline renderer và compileall đạt. Chưa có bằng chứng GitHub CI Gate hoặc Trivy 0.70.0 trong phase này. Không thay đổi hay kích hoạt tài nguyên live. Recovery RESOLUTION_BLOCKED và các phase tích hợp tiếp theo còn chưa thực hiện.

## Recovery v2 — 2026-10-08

Đã thêm control_plane.operator_v2: chỉ chuyển đúng một version RESOLUTION_BLOCKED sang RESOLUTION_RETRY qua Store CAS, reset attempts=0, next_attempt=now, lưu lỗi cũ trong audit và xóa error_type hiện hành. Sai version/trạng thái hoặc retry trùng bị từ chối; không resolve, deploy hay truy cập legacy state. 8 focused tests và toàn bộ 678 tests đạt (2 cảnh báo cũ); compileall, safe help và diff check đạt. Chưa chạy recovery live.

Lệnh operator dưới đây chạy bên trong một Pod quản trị đã được duyệt, có image control-plane mới và quyền get/update đúng model-release-state-v2; không chạy trên Pod legacy.

```sh
python -m control_plane.operator_v2 recover-resolution --model-version 3 --operator OPERATOR_ID --note "Operational cause verified fixed" --enable-live
```

CI hardening đã push: 6ae636a. Tạo Draft PR qua integration bị 403 Resource not accessible by integration; chưa có bằng chứng CI Gate xanh. Mở https://github.com/Ngducanh05/ml_template/compare/main...feat/canary-monitoring-demo?expand=1 rồi chọn Create draft pull request.

## Image và kiểm kê chỉ đọc — 2026-10-08

Image từ source de3fd311df7da971431f417b9ac4d41456957ec1 đã build một lần, smoke linux/amd64 dưới UID/GID 10001:10001, network=none/read-only, default help và imports đạt. Trivy 0.70.0 quét archive của cùng image: 0 HIGH/CRITICAL, ignore-unfixed=false, không ngoại lệ; scan bổ sung 0.75.0 cũng đạt. Đã push nguyên image vào ECR và xác nhận digest:

`596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service@sha256:5935e1d61fd248e545ff09d47a6d16f2ec517ac4441842950d9b961c4f0997bd`

Tag: control-plane-v2-de3fd31. Rendered v2 deployments replicas=0 ra evidence ngoài repo; chưa apply. STS xác nhận account 596432515999; context ml-template-dev-sg; EKS ACTIVE. Pod Identity hiện có: kserve-artifact-reader, model-promotion-consumer, model-image-watcher. Chưa có association processor/reconciler. Kubernetes API timeout: publicAccessCidrs chỉ 1.55.96.180/32, IP egress hiện tại 118.68.111.67. Đang chờ quyết định thêm riêng IP hiện tại hoặc đổi mạng; không tự mở rộng endpoint. Chưa retry Gate1; chưa verify stable/live metrics/consumer. Không có thay đổi IAM, Pod Identity hay Kubernetes trong phase này. QUANG_PROMOTION_READY=NO.

## Phase E/F đã xác minh — 2026-10-08

Theo xác nhận trực tiếp của người dùng, đã thêm 118.68.111.67/32 vào EKS publicAccessCidrs và giữ 1.55.96.180/32; update e86ca076-feb4-317c-b5cf-815db8a7df63 Successful. Cluster có 2 nodes Ready. Gate1 hiện Ready=True, observedGeneration=1; initializer tải artifact thành công. Đã kiểm tra server health, model-ready, metadata và inference native V2: model credit-risk-v3-gate1, output [0,0,0]. Không restart/retry Gate1. Runtime digest và storageUri khớp v3, ECR fingerprint mapping đúng; ServingRuntime Gate1 cũ thiếu annotation full fingerprint nên chưa tuyên bố hoàn tất baseline v2.

Đã tạo role arn:aws:iam::596432515999:role/ml-template-model-runtime-processor-v2 (association a-dvt9g8b9ayypwn1ml) và arn:aws:iam::596432515999:role/ml-template-model-champion-reconciler-v2 (association a-bwxvivizpzsvqenmq). Policy/trust lưu tại k8s/v2-iam/: trust giới hạn cluster/namespace/SA; processor chỉ đọc 3 metadata files và ECR DescribeRepositories/DescribeImages; reconciler chỉ đọc champion.json. Quyền bucket-side cross-account cho hai role mới chưa kiểm chứng; không sửa bucket Quang.

Đã server dry-run và apply SA/RBAC/Service/5 Deployments v2 bằng digest image đã scan. Tất cả replicas=0; không apply ConfigMap. Đã đọc lại state: stable=null, highest_accepted=0, pending=[], active=null, weight=0. Legacy controller vẫn 0; không đổi Iris/legacy. Prometheus/Grafana hiện có, chưa thêm scrape/dashboard v2.

Tiếp theo: tạo Draft PR thủ công (integration create PR bị403); xây dựng/test bootstrap CAS chỉ sau backend exact-pair verification; materialize deterministic runtime/full annotation và stable-v2; sau đó router, Prometheus, processor, reconciler, controller, consumer cuối cùng theo các gate đã định. Chưa chạy recovery live, chưa gửi yêu cầu promotion, chưa có candidate mới/Canary/rollback live. Grafana/autoscaling/BUILD_REQUIRED còn deferred theo thứ tự phase. QUANG_PROMOTION_READY=NO.
