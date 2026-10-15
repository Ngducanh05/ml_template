#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo --preserve-env=GITHUB_RUNNER_TOKEN ./bootstrap_release_runner.sh \
  --role readonly|authorizer --runner-version X.Y.Z --runner-sha256 HEX

GITHUB_RUNNER_TOKEN must be a short-lived repository registration token. The
script configures exactly one ephemeral runner and then runs it in the
foreground. Use a separate, freshly provisioned host for each role.
EOF
}

role=""
runner_version=""
runner_sha256=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) role="${2:-}"; shift 2 ;;
    --runner-version) runner_version="${2:-}"; shift 2 ;;
    --runner-sha256) runner_sha256="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ ${EUID} -eq 0 ]] || { echo "Run as root on a dedicated Ubuntu 24.04 host" >&2; exit 1; }
[[ ${role} == "readonly" || ${role} == "authorizer" ]] || { usage >&2; exit 2; }
[[ ${runner_version} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid runner version" >&2; exit 2; }
[[ ${runner_sha256} =~ ^[0-9a-fA-F]{64}$ ]] || { echo "Invalid runner SHA-256" >&2; exit 2; }
[[ -n ${GITHUB_RUNNER_TOKEN:-} ]] || { echo "GITHUB_RUNNER_TOKEN is required" >&2; exit 2; }

repo_url="https://github.com/Ngducanh05/ml_template"
runner_user="mlops-runner-${role}"
runner_label="mlops-release-${role}"
install_dir="/opt/actions-runner-${role}"
archive="/tmp/actions-runner-linux-x64-${runner_version}.tar.gz"
download_url="https://github.com/actions/runner/releases/download/v${runner_version}/actions-runner-linux-x64-${runner_version}.tar.gz"

[[ ! -S /var/run/docker.sock ]] || { echo "Docker socket present; refusing privileged host" >&2; exit 1; }
[[ ! -e /root/.aws/credentials ]] || { echo "Static root AWS credentials found; refusing host" >&2; exit 1; }
getent group docker >/dev/null 2>&1 && { echo "Docker group present; refusing host" >&2; exit 1; }

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl git jq python3.12 python3.12-venv sudo
id -u "${runner_user}" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "${runner_user}"
if id -nG "${runner_user}" | grep -Eq '(^| )(sudo|docker)( |$)'; then
  echo "Runner user has a privileged group; refusing host" >&2
  exit 1
fi

install -d -o "${runner_user}" -g "${runner_user}" -m 0750 "${install_dir}"
curl --fail --show-error --silent --location "${download_url}" --output "${archive}"
echo "${runner_sha256}  ${archive}" | sha256sum --check --strict
tar -xzf "${archive}" -C "${install_dir}"
rm -f "${archive}"
chown -R "${runner_user}:${runner_user}" "${install_dir}"

token="${GITHUB_RUNNER_TOKEN}"
unset GITHUB_RUNNER_TOKEN
sudo -u "${runner_user}" "${install_dir}/config.sh" \
  --unattended --ephemeral --disableupdate \
  --url "${repo_url}" --token "${token}" \
  --name "$(hostname)-${runner_label}" \
  --labels "${runner_label}" --work _work
unset token

echo "Starting one-shot ${runner_label}; the process exits after one job."
exec sudo -u "${runner_user}" "${install_dir}/run.sh"
