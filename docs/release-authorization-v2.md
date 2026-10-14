# Release v2 authorization - implementation and operator handoff

The authoritative workflow remains `.github/workflows/release.yml` (`Release`). This is an offline implementation, not a live deployment or an approval of v9.

## Responsibility mapping

| Original Release responsibility | Existing mechanism | v2 adaptation |
|---|---|---|
| Controlled source/version | release.py first attempt, SemVer tag, main ancestry | Reuse validation conventions; verify workflow path, checkout SHA and remote tag again after approval |
| Required tests/quality | Release tests and separate CI | Release runs full tests; CI remains independent |
| Build once and scan | Old runtime + artifact image build/scan | Consume successful runtime-build contract, smoke and security artifacts; re-evaluate raw scan; verify immutable ECR catalog and config; never rebuild |
| EKS dev validation | Dual-image deployment helper | Read-only exact candidate/idle durable stable baseline and artifact evidence checks; staging inference explicitly NOT_PERFORMED |
| Eligible candidate/ticket | candidate.json, ticket_reference | Bind complete ServingPair, event identity, fingerprint, source, runtime evidence and ticket; reference is not ticket approval |
| Human approval | production Environment | Preserve native reviewer gate; no writer credential before approval; check review history and protection settings again |
| Production action | Dual-image deploy | CAS authorization only; Controller owns all KServe and traffic effects |
| Audit | Release summary | AUTHORIZED is not PROMOTED; only matching durable terminal audit proves outcome |

## Flow and trust boundary

```text
CI (source/tests/manifests/control-plane security)
Model promotion -> Consumer -> Processor -> runtime REUSE / BUILD_REQUIRED
  BUILD_REQUIRED -> manual runtime-build dispatch -> build/smoke/security -> eligible catalog
  REUSE ---------------------------------------------------------------+
                                                                       v
READY_FOR_RELEASE -> Release validation/preflight -> human ticket approval
 -> APPROVED_FOR_RELEASE (CAS, exact identity) -> Controller PREPARING
 -> external Canary 10/25/50/100 -> Promote / exact-pair Rollback
```

Processor never approves. Controller claim checks the authorization under the existing Store CAS before any Canary preparation. No candidate without authorization can start; malformed legacy active authorization is routed to safe rollback rather than resumed as a new approved rollout. Existing rollback and 10/25/50/100 logic are unchanged.

Evidence binds model_version, immutable runtime_digest, immutable storage_uri, fingerprint, event identity, stable pair/fingerprint, highest accepted version, ticket, repository, workflow path, run ID/attempt, source SHA/tag and security evidence hash. Only the newest accepted version can start; a newer unresolved promotion also invalidates an older approval. Stable/identity changes during review fail closed. Preflight evidence expires after24h; authorization must be claimed within15min. Retries never extend it. Same evidence is idempotent before claim and never extends expiry. A different run cannot replace a still-valid authorization. Expired, unclaimed authorization can be replaced only by a fresh independently approved Release run created after the previous expiry, with fresh preflight and exact prior authorization hash (`supersedes`). CAS compares the original stable baseline, complete candidate, latest accepted version and prior authorization; a winning claim, baseline change, newer promotion or competing approval fails closed. The bounded `release_reauthorized` audit binds old/new IDs, source and ticket. Old runs, reruns and replayed artifacts cannot renew the claim window. A claimed rollout may safely complete/recover after expiry. State/history bounds remain500KB/100audit rows/20pending. No unbounded approval ledger or tokens/ticket bodies are stored.

The pure authorize function trusts its caller, like existing Store transitions. GitHub's protected writer is the authority boundary; JSON is not a cryptographic human signature. Native review history is checked against the exact run/workflow/source and an independent reviewer, without inventing reviewer identity or ticket status. Kubernetes ConfigMap update RBAC cannot enforce field-level writes: all identities with update permission on v2 state remain trusted control-plane identities. No broader credential is used as a shortcut.

## Runtime evidence requirements

Evidence selection is explicit and mutually exclusive; no missing-input fallback:

- `runtime_evidence_source=workflow_run` requires only `runtime_evidence_run_id`. Existing checks remain: successful runtime-build in this repository, source reachable from main, canonical request, build/smoke/Trivy artifacts, artifact ZIP digests, current vulnerability policy, immutable ECR catalog and registry config. Missing/expired artifacts classify the candidate NOT_ELIGIBLE. No build is dispatched by Release.
- `runtime_evidence_source=retained_provenance` requires only `runtime_provenance_digest=sha256:<64 hex>`. Release reads a content-addressed reviewed record from the operator-controlled authority store described below, validates its exact technical contract/image and time-bounded approval, re-evaluates its retained raw scan against the unchanged current policy, and checks the catalog plus registry config. This supports compatible historical runtimes and archived generic CI evidence without rebuilding or rescanning. It never trusts catalog HIT or inference alone.

### Investigation: actual historical provenance, not an approval migration

The local Runtime-X audit directory referenced by docs/release-v2.md contains the unsuppressed scan, functional checks, security-policy.json and catalog-seed-result.json. It binds fingerprint `sha256:700096f6d55f8aa9b56c9049e1c4171633f403a8d3e2a2de2df1ff544eab63fc` to immutable digest `sha256:f1ae0d6c95d19b73adbfa72ae183ef6f207a2dc647bc2d4a1a36386a02ba5ae0`. The historical report says PASS WITH DOCUMENTED RISK ACCEPTANCE, with 16 exceptions expiring 2026-10-21; the unsuppressed report includes CRITICAL/HIGH. Those local historical assertions are not automatically a trusted Release authority record and do not satisfy the current stricter policy. No historical audit files or sensitive content are copied into this public repository. No exception is inherited or extended.

Generic runtime-build retains its artifacts for 14 days. A still-valid, separately reviewed retained record may replace the artifact transport after archival; expiration must never trigger an automatic build or fabricated PASS. If no acceptable record exists, the result remains NOT_ELIGIBLE and requires independent evidence review, not a runtime rebuild just for evidence.

### Retained provenance interface (not provisioned or approved live)

Fixed trust root on both isolated Linux Release runners: `/etc/mlops/release/runtime-provenance/`. This path is outside the checkout and workflow artifacts. The runner must be non-root; every file and ancestor must be root-owned, non-symlink, and not group/world writable. Runners must have no sudo/root/container-host escape privileges that could alter this store. Administrators provision it through a separately approved process; workflow inputs cannot choose a directory, install records, or grant authority. Read-only mount and restricted runner scheduling are operational prerequisites, not claims proven by local unit tests.

`approvals.json` is an operator-controlled index (maximum 64 KiB/100 entries), schema `{schema_version: 1, records: [...]}`. Each entry has exactly:

- `record_digest`: canonical JSON SHA-256 of one retained record.
- `authority`, `approval_reference`: non-sensitive identifiers of the independent evidence reviewer and real approval record (same bounded token syntax as ticket_reference).
- `approved_at`, `expires_at`: timezone-qualified ISO timestamps; both enforced at preflight and immediately before authorization.
- `risk_acceptance_expires_at`: null or the original acceptance expiry, also enforced; it grants no security waiver.
- `status`: must be `approved`; revocation or ambiguity blocks.

`<digest hex>.json` (maximum 16 MiB) contains exactly `schema_version: 1`, canonical `request`, immutable `image`, retained raw `scan`, original `security_decision`, and SHA-256 references `build_evidence` / `smoke_evidence`. The original PASS decision must match the fingerprint and scanned image config; policy is independently reevaluated today. Authority provisioning must independently inspect and preserve the exact original build, smoke, scan, compatible contract and approval evidence corresponding to these hashes. A submitter's JSON, owner string, catalog tag or PASS label alone cannot establish authority. The root-owned index approval of the full content hash is the explicit trust anchor, not a signature inferred from the record. Retain underlying evidence privately in controlled durable storage; no public disclosure is authorized here.

No real index entry, record, signing credential, new security exception, evidence import or approval has been created by this task. The interface is tested with synthetic fixtures only. Runtime-X remains blocked for this new Release path until an independently reviewed record and current policy eligibility exist. Even a trusted retained record cannot override CRITICAL or other current policy BLOCK findings. Configuration/authority provisioning and any policy decision remain separate approval boundaries.

v9-like BUILD_REQUIRED is rejected before authorization. MLflow3.14.0 security blockers remain upstream contract work. No waiver, pin/fingerprint edit, ECR publication or new model promotion is part of this task.

## Workflow source and human operation

Keep immutable, maintainer-controlled `vX.Y.Z` release tags and protected main. The tag versions release source, not the numeric model version; model_version is a separate input. Feature-branch dispatch cannot authorize production. First attempt only; reruns fail closed. Tag existence/SHA and main ancestry are rechecked before authorization; administrative tag/ruleset protection still requires owner configuration. No tags are created by this implementation.

Human flow: inspect candidate/test/security artifacts, independently open the referenced company ticket and verify approved status for that exact release; only then use GitHub Approve and deploy. No ticket API or boolean approval substitute exists. Rejection/cancellation before the protected job means no writer execution. Cancellation after CAS cannot revoke a rollout already authorized; inspect durable state rather than assuming cancellation rolls back. Any revoke/cancel operation after authorization needs a separate reviewed design, not direct state editing.

The post-approval job rechecks source/protection/native reviews/candidate/catalog/security, then stores approval via CAS. Controller may claim it immediately. One read-only observation records PROMOTED/ROLLED_BACK/ROLLOUT_STARTED only from matching authorization IDs; otherwise AUTHORIZED/PENDING_UNVERIFIED. Audit never equates a successful GitHub job with a successful model rollout. GitHub failure alone cannot distinguish rejection from other failure; native review history is authoritative. Runner interruption can prevent final artifact upload.

## Configuration requiring explicit owner approval (not applied)

- `k8s/release-v2/authorization-rbac.proposed.yaml`: separate reader get-only and authorizer get/update-only, both restricted to production/model-release-state-v2. No KServe/router/legacy/IAM/ECR-write privileges.
- Trusted isolated Linux runners: labels mlops-release-readonly and mlops-release-authorizer. No ambient administrator/AWS/Kubernetes credentials; never schedule untrusted PR code on them.
- Reader repository secret RELEASE_STATE_READER_B64; writer secret RELEASE_STATE_WRITER_B64 **only in production Environment**. Each encodes a minimal JSON `{server, certificate_authority_data, token, namespace, state_name}`; namespace=production, state_name=model-release-state-v2. CA is base64 PEM; token must belong to the corresponding dedicated scoped identity. Provision/rotate limited-lifetime credentials through an approved operator process. This does not create a token or IAM association.
- RELEASE_CATALOG_READ_ROLE_ARN: proposed OIDC ECR reader, only DescribeRepositories/DescribeImages/BatchGetImage for the existing runtime repository. Trust is limited to main for scheduled BUILD_REQUIRED observation, immutable release tags for preflight, and the protected production Environment for the authorizer recheck. See catalog-reader-policy.proposed.json/trust.proposed.json; no IAM mutation performed.
- Environment Settings **UNVERIFIED**: connector could not read the environment API. Settings > Environments > production: required authorized reviewers; Prevent self-review enabled; administrator bypass disabled; selected deployment policies containing exactly one Tag rule v*. Do not create an unprotected environment by merely running YAML. Ensure the workflow's GitHub credential can read the documented protection and review APIs; missing/inaccessible API-verifiable protections block. The API parser reuses the original required-reviewer/prevent-self-review/selected-policy checks. Documented GET examples may omit `can_admins_bypass` and policy `type`; absence is not interpreted as a favorable value. If present, an explicitly unsafe value still blocks. Preflight records both settings as REQUIRES_SETTINGS_VERIFICATION. The runbook still requires disabling administrator bypass and a Tag v* rule in Settings before live acceptance. Source checks independently require an immutable SemVer tag, and the writer requires native independent production review even if an administrator bypassed the job gate. Do not substitute broad production credentials.
- Review/merge to main and create protected release tags are separate human actions, not authorized here. PR8 remains Draft. Deploying the new controller image and RBAC requires separate approval. Old live controller behavior is not changed by a source commit; this gate is not claimed enforced live until that rollout.

API compatibility references: [Get an environment](https://docs.github.com/en/rest/deployments/environments#get-an-environment) and [deployment policies](https://docs.github.com/en/rest/deployments/branch-policies#list-deployment-branch-policies). Settings remain UNVERIFIED in this source-only repair; no live settings were modified.

Official behavior: [GitHub deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments) confirms jobs wait for configured reviewers and environment secrets become available afterward; [review history API](https://docs.github.com/en/rest/actions/workflow-runs#get-the-review-history-for-a-workflow-run) supplies the native review record.

## Existing workflow status

CI remains source validation. Release is authoritative orchestration. Generic Runtime Build is its conditional technical capability and currently manually dispatched. Demo - Human Approval Gate is historical/non-authoritative and has no v2 state writer; Local validation is an unused legacy reusable dual-image helper. Both remain present, not deleted. scripts/release.py dual-image functions and tests remain for historical/shared local callers; release.yml no longer calls their production deploy path. No parallel release-v2.yml is introduced.

## Next safe validation

Run offline tests and YAML/actionlint. Inspect PR diff and CI. Review proposed credentials/RBAC and Environment protection in UI without applying anything. Do not dispatch production Release from this feature branch, authorize v9, create tags, or mutate live state. Later, only after separately approved main/tag/controller/identity configuration, a genuinely eligible model and retained runtime security evidence can enter the protected human acceptance test.
