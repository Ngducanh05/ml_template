# Generic runtime CI and BUILD_REQUIRED

## Contract and compatibility

A request is technical JSON only: canonical exact dependency pins, Python version, explicit MLServer/MLflow-runtime versions, platform/build/serving profiles, expected fingerprint. Validation recomputes the fingerprint; rejects identity/artifact fields, noncanonical pins, unsupported Python/profile and conflicting serving pins. Current new recipe supports Python3.12.14/linux-amd64 and the sklearn/skops loader; unsupported contracts fail closed rather than silently selecting another interpreter.

Historical runtime-x-hardened-v1 catalog hits remain first priority. Only a MISS proceeds to runtime-ci-py31214-slim-v2 lookup/build request. The new recipe has a new profile identity; existing stable fingerprints and eligibility entries are not relabeled. The processor source change requires its own tested image rollout before new live MISS requests use the CI profile. It does not alter rollback or existing state.

## Workflow

`runtime-build.yml` is manually dispatched with a normalized request copied from a real BUILD_REQUIRED entry (or an explicitly labeled isolated test). No GitHub token/dispatcher or builder is added to EKS. Normal processor polling rechecks the catalog after publication; no state edit/recovery command is required for BUILD_REQUIRED.

1. Validate independently, create an allowlisted three-file Docker context plus ignore file. No repository/model/S3 bytes enter it.
2. For publication mode, query the immutable ECR fingerprint tag before building. HIT skips the build. AWS errors are not MISS. The repository must have immutable tags without exclusions.
3. Build in a separate job with contents-read only, no AWS credentials/OIDC. Pinned base/profile and exact requested dependencies; wheel-only install; pip check; resolved transitive dependency list preserved in the image. Model artifacts are never baked in.
4. Run native MLServer in a network-disabled, non-root/read-only container with tmpfs. A tiny synthetic sklearn/skops artifact is created only after build in tmpfs to verify startup/readiness/inference. This is not live model/release evidence.
5. Scan the exact archive, no ignore file or unfixed suppression; fail on HIGH/CRITICAL/UNKNOWN or missing complete report. Preserve successful exact archive for publication. The frozen environment and image archive record the resolved first-build environment; retries use catalog HIT and do not rerun a potentially changed resolver.
6. A protected publication job loads that archive, checks scan config/layer identity, pushes those bytes, verifies registry manifest/config against the scan and uses the existing collision-checking Catalog.publish. Same fingerprint/different digest is rejected. Concurrent build races never overwrite eligibility.

`publish=false` performs isolated build/test/security only. It intentionally has no AWS lookup/push. `publish=true` requires reviewed OIDC and environment protection. No workflow dispatch, IAM creation or live BUILD_REQUIRED release is implied by source tests.

## Local commands

```powershell
python -m scripts.runtime_ci prepare --request <technical-request.json> --output <empty-context>
docker build --platform linux/amd64 --provenance=false -t runtime-ci:candidate <empty-context>
docker run --rm --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp:rw,size=512m,mode=1777 --mount type=bind,source=<absolute-runtime_ci/smoke.py>,target=/smoke.py,readonly runtime-ci:candidate python /smoke.py
docker save -o <runtime.tar> runtime-ci:candidate
```

Then scan the archive and enforce scripts.runtime_ci.require_clean_scan; publication is blocked until artifact identity and the required approvals are satisfied. No model runtime image should be described as clean until that exact artifact has actually passed.

## OIDC approval boundary

Use a dedicated role trusted only for the exact repository and protected `runtime-build-publish` environment, audience sts.amazonaws.com, session900seconds. Configure required reviewers and permitted deployment branches before granting trust; environment-name trust alone does not restrict a workflow file. Repository/environment admins remain trusted publishers.

Permissions: ECR login on its required wildcard resource; only DescribeRepositories/DescribeImages/BatchGetImage/BatchCheckLayerAvailability/GetDownloadUrlForLayer/InitiateLayerUpload/UploadLayerPart/CompleteLayerUpload/PutImage on the one runtime repository. No ECR delete, repository creation, IAM mutation, S3, EKS, SQS or static AWS secrets. Exact account-bound trust/permissions are rendered locally for owner review; never automatically applied. The protected environment variable RUNTIME_BUILD_ROLE_ARN selects the approved role.

Revocation: remove role trust or environment role variable, cancel active publication jobs and wait for short sessions to expire; preserve existing eligible images/tags. Do not delete model/ECR data as rollback. IAM integration and an actual new-contract real model are separate acceptance gates.

## Acceptance labels

Unit/workflow contract tests do not prove container build, MLServer functional smoke, security cleanliness, ECR publication, OIDC or a real release. Record each separately. An isolated technical fingerprint build is not a real BUILD_REQUIRED model release. Existing runtime risk exceptions must not be inherited by this strict CI gate.
