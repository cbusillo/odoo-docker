#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 6 ]]; then
  echo "Usage: scan-image-dependencies.sh <image> <source-commit> <baseline-commit> <scan-scope> <image-source> <output-directory>" >&2
  exit 64
fi

image_reference="$1"
source_commit="$2"
baseline_commit="$3"
scan_scope="$4"
image_source="$5"
output_directory="$6"

: "${TRIVY_IMAGE:?TRIVY_IMAGE is required}"
: "${TRIVY_CACHE_DIR:?TRIVY_CACHE_DIR is required}"
: "${TRIVY_VERSION:?TRIVY_VERSION is required}"
: "${TRIVY_DB_SHA256:?TRIVY_DB_SHA256 is required}"
: "${SCAN_CONFIGURATION_SHA256:?SCAN_CONFIGURATION_SHA256 is required}"
: "${DEPENDENCY_HEALTH_REPOSITORY:?DEPENDENCY_HEALTH_REPOSITORY is required}"

expected_trivy_image='aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969'
if [[ "${TRIVY_IMAGE}" != "${expected_trivy_image}" ]]; then
  echo "TRIVY_IMAGE must use the repository-approved digest pin" >&2
  exit 64
fi
if [[ "${TRIVY_VERSION}" != "0.74.0" ]]; then
  echo "TRIVY_VERSION must match the repository-approved scanner" >&2
  exit 64
fi

case "${image_source}" in
docker | remote) ;;
*)
  echo "image source must be docker or remote" >&2
  exit 64
  ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${output_directory}"
output_directory="$(cd "${output_directory}" && pwd)"
cache_directory="$(cd "${TRIVY_CACHE_DIR}" && pwd)"

docker_arguments=(
  run
  --rm
  --workdir /tmp
  --tmpfs /root/.cache/trivy
  --volume "${cache_directory}/db:/root/.cache/trivy/db:ro"
  --volume "${output_directory}:/output"
)
if [[ "${image_source}" == "docker" ]]; then
  docker_arguments+=(--volume /var/run/docker.sock:/var/run/docker.sock)
elif [[ -f "${HOME}/.docker/config.json" ]]; then
  docker_arguments+=(--volume "${HOME}/.docker/config.json:/root/.docker/config.json:ro")
fi

docker "${docker_arguments[@]}" "${TRIVY_IMAGE}" image \
  --cache-dir /root/.cache/trivy \
  --image-src "${image_source}" \
  --ignorefile /dev/null \
  --ignore-unfixed \
  --scanners vulnerability \
  --severity LOW,MEDIUM,HIGH,CRITICAL \
  --skip-files /usr/share/java/gettext.jar \
  --skip-files /usr/share/java/libintl-0.21.jar \
  --skip-db-update \
  --skip-java-db-update \
  --skip-version-check \
  --format json \
  --output /output/trivy.json \
  "${image_reference}"

jq -cn \
  --arg repository "${DEPENDENCY_HEALTH_REPOSITORY}" \
  --arg source_commit "${source_commit}" \
  --arg baseline_commit "${baseline_commit}" \
  --arg producer_version "${TRIVY_VERSION}" \
  --arg advisory_revision "${TRIVY_DB_SHA256}" \
  --arg scan_scope "${scan_scope}" \
  --arg scan_configuration_sha256 "${SCAN_CONFIGURATION_SHA256}" \
  '{
    repository: $repository,
    source_commit: $source_commit,
    baseline_commit: $baseline_commit,
    producer: "trivy",
    producer_version: $producer_version,
    advisory_source: "trivy-db",
    advisory_revision: $advisory_revision,
    scan_scope: $scan_scope,
    scan_configuration_sha256: $scan_configuration_sha256
  }' >"${output_directory}/provenance.json"

python3 "${repo_root}/scripts/image-dependency-health.py" snapshot \
  --trivy-json "${output_directory}/trivy.json" \
  --provenance "${output_directory}/provenance.json" \
  --output "${output_directory}/snapshot.json"
