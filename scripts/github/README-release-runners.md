# Release v2 isolated runner bootstrap

This directory prepares, but does not provision, the two privileged Release v2
runners. Each runner must use a different dedicated Ubuntu 24.04 X64 host:

| Host role | Required custom label | Workflow access |
|---|---|---|
| read-only | `mlops-release-readonly` | Release preflight and the scheduled runtime catalog observer |
| authorizer | `mlops-release-authorizer` | Protected `production` authorization only |

Do not add both labels to one runner. Do not use a personal workstation, an EKS
worker, or a host with a Docker socket, static AWS keys, sudo access for the
runner user, or a broad kubeconfig. Repository settings must prevent pull
request workflows from targeting either label.

## Inputs owned outside the repository

1. Select two approved isolated hosts and permit only outbound HTTPS to GitHub,
   GitHub Actions artifact endpoints, AWS STS/ECR, and the private EKS endpoint.
2. From the official `actions/runner` release, record the pinned X64 version and
   published SHA-256. Never use an unverified archive.
3. Generate a short-lived repository runner registration token immediately
   before each host starts. Do not store it in source, user data, logs, or a
   long-lived secret.
4. Configure the readonly workflow to assume the dedicated OIDC IAM Reader.
   It may only `get` `ConfigMap/production/model-release-state-v2`; the
   authorizer may only `get,update` that exact object. Do not install an admin
   kubeconfig. See `docs/oidc-eks-reader-operator.md`.

Run on each freshly provisioned host, replacing the example version and hash
with the independently verified official values:

```bash
export GITHUB_RUNNER_TOKEN='<short-lived registration token>'
sudo --preserve-env=GITHUB_RUNNER_TOKEN ./bootstrap_release_runner.sh \
  --role readonly \
  --runner-version '<pinned version>' \
  --runner-sha256 '<official sha256>'
```

Use `--role authorizer` only on the second host. The runner is registered with
`--ephemeral --disableupdate`, runs one job in the foreground, and exits. Rebuild
the host from the approved image for the next release window. The scheduled
runtime observer cannot be considered active with this one-shot runner model;
it needs a separately reviewed ephemeral-runner controller or a dedicated
persistent read-only runner.

## Acceptance checks

- GitHub shows one online Linux/X64 runner for each exact custom label.
- A test workflow proves the read-only label cannot receive authorizer work and
  the authorizer label cannot receive read-only work.
- GitHub OIDC returns account `596432515999` and role
  `ml-template-v2-catalog-reader` for an allowed release tag/environment.
- ECR describe/batch-get on `model-service` succeeds; ECR writes and reads of a
  different repository fail.
- Kubernetes impersonation proves reader `get` succeeds and `update` fails;
  authorizer can `get,update` only the exact v2 state ConfigMap.
- The `production` Environment has an independent reviewer, prevent self-review,
  no administrator bypass, and only the `v*` deployment tag policy.

No runner is ready until every check above has live evidence.

## Kubernetes credentials

The read-only path obtains a fresh EKS token from the dedicated GitHub OIDC IAM
Reader in every job. It verifies the exact STS role, cluster ARN, endpoint, CA,
state object and schema, and never stores the token. `RELEASE_STATE_READER_B64`
is not used by the scheduled trigger or Release preflight.

The writer remains operator-rotated and protected by the `production`
Environment. Run writer rotation only from an approved operator workstation
that already has access to the EKS control-plane API:

```bash
python -m scripts.github.rotate_release_state_credential --role writer
python -m scripts.github.rotate_release_state_credential \
  --role writer --store --confirm-writer-rotation
```

The writer is a `production` Environment secret; rotate it immediately before
the independent reviewer approves the protected job. A delayed approval must
receive a fresh writer credential before approval; an expired token fails
closed at the Kubernetes API.

Never paste the writer value into a command line, file, log, artifact, or chat.
The helper passes it to `gh secret set` through standard input. Writer
replacement requires the explicit confirmation flag because an existing
protected Environment secret may still be in use.

Before bringing the read-only runner online, cancel or otherwise disposition
all old queued runs. The scheduled observer requires a dedicated persistent
read-only host or a separately reviewed ephemeral-runner controller. The
authorizer remains one-shot and offline outside an approved release window.
