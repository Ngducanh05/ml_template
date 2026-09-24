# Week 2 manual configuration and decisions

## Confirmed, proposed, unknown

**Mentor-confirmed:** the ticket is the production gate and must be approved
before production.

**Proposed and implemented in this demo:** tests, registry push, blocking security
scan and Docker Desktop validation complete first. A candidate fixes the source,
release version and image digest. A human verifies the ticket and approves the
GitHub `production` Environment. The job then deploys automatically by digest.

**Unknown company policy:** ticket system/API/schema/status vocabulary, authorized
approver roles/count, approved security severities, treatment of unfixed findings,
release retention, release source policy and real production infrastructure.
Nothing in this file confirms those as company standards.

## 1. Repository and release source protection

- Merge the Week 2 files into `main` before creating a release tag. The workflow
  must exist on the default branch for manual dispatch to be available.
- Protect `main` with reviewed changes and passing CI. Protect `v*` tags against
  updates/deletion and restrict tag creation to release maintainers. Review changes
  to workflows, release helper, Dockerfile, requirements and manifests as code
  that will run with release privileges. A workflow cannot defend against an
  administrator deliberately replacing its own controls.
- Proposed trigger: manually dispatch `release.yml` at a tag exactly matching
  `vX.Y.Z` (no prerelease suffix or leading zeros), with a non-sensitive ticket or
  release reference. The tag's commit must be reachable from `origin/main`.
- Example after creating an authorized tag: `gh workflow run release.yml --ref
  v1.2.3 -f ticket_reference=<reference>`. The Actions UI can also dispatch the tag.
  Do not enter secrets, signed links, ticket bodies or confidential data as the
  reference; workflow inputs and artifacts are visible to repository readers.
- PR jobs use only hosted runners and read-only contents permission. Do not run
  untrusted PR code on either privileged self-hosted deployment runner. Restrict
  runner groups to trusted repositories/workflows where the account supports it.
- Release reruns are rejected, including failed-job reruns. After investigation,
  use a new source commit and new version. Never repush an existing release tag.
  This conservative demo rule preserves Build Once; it is not company policy.

## 2. JFrog and repository configuration

The reference registry is `jfrog.vinsmartfuture.tech`; the Docker repository path
and credentials have not been supplied. Confirm the registry's Docker V2 routing,
trusted TLS certificate chain and the permitted repository/image path with its
owner. The helper checks `https://<host>/v2/<image-path>/manifests/<tag>` using a
username plus access token. Only HTTP 404 means an unused tag; 401/403, timeouts,
redirect/auth incompatibility or other failures must be fixed, not ignored.

Configure **Settings > Secrets and variables > Actions**:

| Kind | Name | Required value / purpose |
| --- | --- | --- |
| Variable | `JFROG_REGISTRY` | Optional host[:port], no scheme/path; defaults to the reference host. Change only with an authorized registry endpoint. |
| Variable | `JFROG_IMAGE_PATH` | Required Docker repository and image path below the host; obtain the exact value from the owner. No leading slash, tag or digest. |
| Secret | `JFROG_USERNAME` | Scoped publisher/scanner identity. |
| Secret | `JFROG_TOKEN` | Access token/password for that identity; never commit or print it. |
| Variable | `TRIVY_BLOCKING_SEVERITIES` | Required non-empty comma-separated subset of UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL. |
| Variable | `TRIVY_IGNORE_UNFIXED` | Required literal `true` or `false`. `false` includes unfixed findings in blocking evaluation. |
| Variable | `VALIDATION_NAMESPACE` | Pre-created dedicated Docker Desktop namespace, e.g. `model-service-validation`. |

For mentor review, a possible starting policy is `HIGH,CRITICAL` with
`TRIVY_IGNORE_UNFIXED=false`. This is a **proposal**, not a confirmed threshold.
There is deliberately no silent policy default or vulnerability bypass input.
Trivy scans OS and language-package vulnerabilities; it is not a claim that all
security risks have been eliminated. Its JSON report records the evaluated
severity set and the release evidence records the selected policy.

The build runs on hosted Ubuntu and needs registry access, Docker/BuildKit base
image access, Python packages and Trivy database access. If JFrog requires a
private network, arrange an authorized Linux build runner/network and change only
that runner selection; do not substitute another registry or disable TLS/security
checks. Trivy action and scanner versions are explicit in the workflow.

Configure JFrog to prevent release/build tag overwrite and deletion by the CI
publisher (including redeploy permission), within the registry's supported access
model. The workflow rejects existing `vX.Y.Z` and `m-<12-char-SHA>` tags, but that
preflight alone is not a registry-wide lock. The publisher must have only the
read/push permissions necessary for this path. Separate Kubernetes pull identities
should be read-only. Keep images/manifests available throughout approval and
retention; deleting a digest must fail deployment rather than trigger a rebuild.

## 3. Docker Desktop validation runner and private image pulls

Register an authorized Windows x64 GitHub runner with custom label
`model-service-validation`. Install a current supported Actions runner, Python
3.12.14 support for setup-python, and kubectl. Docker Desktop Kubernetes must be
running and reachable as context `docker-desktop` for the runner's service account.
The runner account may differ from the interactive user; verify its kubeconfig.

Create the dedicated validation namespace and provision a Docker registry pull
secret named `model-service-registry` in it. Both deployment environments use this
secret name; their values can differ. Prefer an isolated Docker config containing
only a read-only JFrog credential, populated interactively using `docker login`.
Import that config without putting credentials on a command line or in Git:

```powershell
kubectl --context docker-desktop create namespace <validation-namespace>
kubectl --context docker-desktop --namespace <validation-namespace> create secret generic model-service-registry --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson=<path-to-isolated-docker-config.json>
```

The Docker config must actually contain registry auth data; a `credsStore` pointer
to a desktop credential helper is not usable by Kubernetes. Arrange secure secret
creation/rotation with the operator and remove temporary credential files. Do not
commit the secret YAML or copy credentials into evidence. Docker login on the
build runner does **not** authenticate Kubernetes pulls.

The reusable `cd-local.yml` runs automatically inside Release and consumes that
run's build evidence and digest. It is no longer a separate rebuild/dispatch path.
Directly applying `k8s/deployment.yaml` is unsupported: its sentinel image must be
rendered by the helper before apply. Existing default-namespace Week 1 workloads
are separate from the dedicated release-validation namespace.

## 4. Configure the real human production gate before any demo run

In **Settings > Environments**, create `production` explicitly, then configure:

1. **Required reviewers:** choose real authorized reviewer(s). At least one must
   be a different person from the release initiator. GitHub native protection
   requires one of the listed reviewers, not all of them. Confirm company roles
   and any multi-person approval requirement with the mentor.
2. Enable **Prevent self-review**.
3. Disable **Allow administrators to bypass configured protection rules** for
   this demo. This is an implemented proposed rule. Do not use an admin bypass to
   demonstrate approval. Admins can still edit repository settings, so restrict
   administrative access and retain environment history.
4. **Deployment branches and tags > Selected branches and tags:** add exactly one
   rule of type **Tag**, pattern `v*`; no branch rules. The release helper applies
   the stricter SemVer and main-ancestry checks. This environment restriction is
   separate from protecting Git tags against mutation.
5. Save all protection rules and verify them in the UI. Verify the repository's
   visibility/plan supports required reviewers and prevent self-review. If these
   protections are unavailable, the demo gate is **blocked**; do not replace it
   with a workflow input or remove the check.

The candidate job reads the environment and tag-policy APIs with `actions: read`.
Missing reviewers, self-review prevention, disabled bypass, exact tag rule,
API access or recognized settings blocks candidacy. It does not write GitHub
settings. The environment still supplies the actual human gate. Keep protection
rules stable while a release is waiting/running.

A job referencing a nonexistent environment can cause GitHub to create it without
protections; the preflight explicitly rejects that configuration. Protected
secrets are released only after environment protection passes. See the official
[environment configuration guide](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
and [reviewing deployments](https://docs.github.com/en/actions/how-tos/managing-workflow-runs-and-deployments/managing-deployments/reviewing-deployments).

## 5. Production infrastructure and protected secrets

No production cluster, network, runner or credential has been provisioned by this
change. Docker Desktop must never stand in for production.

Provide a dedicated Linux x64 runner labelled `model-service-production`, with
kubectl, supported Actions runner/Python setup, and network access to the real
production API and registry. Do not share it with the Windows validation runner
or PR jobs. Both targets must support `linux/amd64` workloads for this proposal.

Configure the following **on the production Environment**, not as broadly
available repository secrets:

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `PRODUCTION_KUBE_CONTEXT` | Explicit real production context; `docker-desktop` is rejected. |
| Variable | `PRODUCTION_NAMESPACE` | Pre-created dedicated target namespace. |
| Secret | `KUBECONFIG_B64` | Base64 of a scoped kubeconfig for that context/namespace. Base64 is encoding, not encryption. |

Use a trusted kubeconfig with the required credentials/CA; arrange any credential
plugin explicitly on the runner. The helper writes it to an owner-private
temporary file and removes it in a finally block. Force-killed runners may need
operator cleanup. Do not print/decode credentials in workflow logs. Avoid ambient
production credentials on the runner; use only the protected secret.

Pre-provision `model-service-registry` as a read-only pull secret in the production
namespace. Grant the deployment identity namespace-scoped get/list/watch and
create/update/patch for Deployments, ConfigMaps and Services, read Pod state, and
create Pod port-forward connections. It does not need cluster-admin, namespace
creation, Secret read, or registry push permission. Verify the exact RBAC with the
cluster owner. The current manifests implement the demo service, not a complete
company production platform configuration.

## 6. Reviewer procedure and acceptance exercise

The reviewer downloads `release-candidate-*`, build/test/scan and validation
artifacts, checks the source/version/exact digest, and looks up the referenced
ticket in the authoritative process. Confirm approval is valid, for production,
for this service and this exact release; reject if unclear. Check the ticket has
not been invalidated while waiting. Use a non-sensitive approval comment tying
the reference to the version/digest. Only then click **Approve and deploy**.

After approval there is no second manual deploy command: the protected job applies
the candidate digest, checks rollout/readiness/running image, runs smoke tests,
and records success or failure. Failed rollout can leave a partial rollout;
failed smoke can leave the new workload running. Investigate using namespace
state and logs, stop further releases and follow the operator's recovery process.
Automatic rollback, deletion and retry are intentionally absent.

Before claiming the gate works end to end, run controlled acceptance checks on
authorized targets:

| Exercise | Required observation |
| --- | --- |
| Failed tests, push/auth, missing identity/digest/evidence, blocking scan | No eligible candidate or production approval request. |
| Validation apply/rollout/readiness/smoke failure | Failure evidence and production skipped. |
| Empty/whitespace ticket or missing environment protection | Candidate blocked. |
| Leave a valid candidate unapproved | Production steps never start; protected secrets unavailable. |
| Reject it | No deployment; native rejection history plus terminal audit when available. |
| Approve a new valid candidate as a different reviewer | Automatic digest-only deployment and successful rollout/readiness/smoke evidence. |
| Production failure | Workflow/release fails; never recorded as release success. |

Use new source commits/versions for these runs; reruns are disabled. Artifacts
expire after the proposed 30-day retention and may be subject to repository limits.
Confirm retention with the mentor and preserve required audit records before
expiry. A force cancellation or runner outage may interrupt final evidence;
GitHub's native run/deployment history remains essential.

## 7. Future ticket API contract (design only; not integrated)

Keep the human Environment approval. Automating eligibility later requires a real
company contract and must fail closed. Configuration must define endpoint/auth,
timeout, required fields, service/environment/release mappings, approved states,
invalid states, approval authority and validity/expiry rules. No company-specific
schema, status vocabulary or role name is assumed here.

The validator should verify that the reference exists and can be retrieved; all
required fields are present; approval comes from an authorized source; status is
approved and not rejected, cancelled, revoked, expired or otherwise invalid; the
target is production; service/repository matches; and the approved release is the
one being deployed. Prefer binding version **and immutable digest**. Any mismatch,
ambiguous/unknown state, timeout, 401/403, unavailable service or malformed response
blocks the gate. Consider rechecking immediately after a long human wait so revoked
approval cannot be used.

Record a minimal sanitized result: reference, validated version/digest/environment,
validation timestamp, rule/version, outcome and non-sensitive reason code. Do not
store credentials, raw API responses or sensitive ticket contents. A future fully
automated ticket gate replacing the human reviewer requires explicit mentor/company
confirmation; this repository implements no such bypass.

## 8. Execution status of this implementation

Repository implementation and local checks are separate from external rollout.
The implementation session found Docker Desktop Kubernetes available, but no
JFrog path/credentials or production configuration was supplied. No company
registry push/scan, protected GitHub approval, SIT release or production deployment
has been demonstrated by these edits. Complete the configuration and acceptance
exercise above before calling the release flow operational.

Local validation performed on 2026-09-23:

| Check | Actual result |
| --- | --- |
| `python -m pytest -q` before edits | PASS: 54 tests. |
| `python -m pytest -q` after implementation | PASS: 113 tests on local Python 3.11.9, with one existing Starlette/AnyIO deprecation warning. Workflow Python remains 3.12.14. |
| actionlint 1.7.12, custom runner labels supplied, shellcheck disabled | PASS for all three workflows. |
| PyYAML parsing and Python compileall | PASS. |
| `docker build --tag model-service:week2-check .` | PASS: one local verification build, not a published release. |
| Existing smoke script against that container | PASS: liveness, readiness and prediction; runtime UID/GID 10001. Temporary container stopped. |
| Kubernetes server-side dry-run of ConfigMap, Service and digest-rendered Deployment | PASS on Docker Desktop; placeholder digest was used only for schema validation, with no pull/deploy. |
| Existing Week 1 workload rollout, readiness and smoke checks | PASS; workload was not changed. This is not proof of the new registry deployment path. |
| JFrog push/remote Trivy scan and GitHub approval/production flow | NOT_RUN: external setup/credentials and real production target unavailable. |

Failure-path unit tests cover missing/mismatched identities, failed tests/push/
security outcomes, missing/failed reports, failed apply/rollout/readiness/smoke,
environment protection checks/API timeout, rerun rejection and audit success
requirements. Native GitHub pending/rejected/approved behavior still needs the
manual acceptance exercise; a local unit test cannot prove repository protection.
