#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT

tool="${repo_root}/scripts/image-dependency-health.py"
scan_tool="${repo_root}/scripts/scan-image-dependencies.sh"
workflow="${repo_root}/.github/workflows/build.yml"
baseline_commit="1111111111111111111111111111111111111111"
candidate_commit="2222222222222222222222222222222222222222"
configuration_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

write_provenance() {
  local path="$1"
  local source_commit="$2"
  local asserted_baseline="$3"
  local scope="${4:-image:runtime:linux/amd64}"
  cat >"${path}" <<EOF
{
  "repository": "cbusillo/odoo-docker",
  "source_commit": "${source_commit}",
  "baseline_commit": "${asserted_baseline}",
  "producer": "trivy",
  "producer_version": "0.70.0",
  "advisory_source": "trivy-db",
  "advisory_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "scan_scope": "${scope}",
  "scan_configuration_sha256": "${configuration_sha}"
}
EOF
}

write_report() {
  local path="$1"
  local vulnerability_id="${2:-}"
  local severity="${3:-HIGH}"
  local version="${4:-1.0}"
  local target="${5:-candidate-image (ubuntu 24.04)}"
  if [[ -z "${vulnerability_id}" ]]; then
    cat >"${path}" <<'EOF'
{"CreatedAt":"2026-08-20T00:00:00Z","Metadata":{"OS":{"Family":"ubuntu","Name":"24.04"}},"Results":[{"Target":"candidate-image (ubuntu 24.04)","Class":"os-pkgs","Type":"ubuntu","Vulnerabilities":[]}]}
EOF
    return
  fi
  cat >"${path}" <<EOF
{
  "CreatedAt": "2026-08-20T00:00:00Z",
  "Metadata": {"OS": {"Family": "ubuntu", "Name": "24.04"}},
  "Results": [{
    "Target": "${target}",
    "Class": "os-pkgs",
    "Type": "ubuntu",
    "Vulnerabilities": [{
      "VulnerabilityID": "${vulnerability_id}",
      "PkgName": "example-package",
      "InstalledVersion": "${version}",
      "Severity": "${severity}"
    }]
  }]
}
EOF
}

# Exercise the scanner command so comments and shell formatting cannot stand
# in for the two intentional gettext exclusions. No Docker daemon is needed.
mkdir -p "${test_root}/bin" "${test_root}/cache"
cat >"${test_root}/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" >"${SCANNER_CAPTURE_FILE:?}"
cp "${SCANNER_REPORT_FILE:?}" "${SCANNER_OUTPUT_DIRECTORY:?}/trivy.json"
EOF
chmod +x "${test_root}/bin/docker"
write_report "${test_root}/scanner-report.json"
trivy_image="$(sed -n 's/^  TRIVY_IMAGE: //p' "${workflow}")"
trivy_version="${trivy_image#*:}"
trivy_version="${trivy_version%%@*}"
for image_source in docker remote; do
  mkdir -p "${test_root}/scan-${image_source}"
  PATH="${test_root}/bin:${PATH}" \
  SCANNER_CAPTURE_FILE="${test_root}/scanner-arguments.txt" \
  SCANNER_REPORT_FILE="${test_root}/scanner-report.json" \
  SCANNER_OUTPUT_DIRECTORY="${test_root}/scan-${image_source}" \
  TRIVY_IMAGE="${trivy_image}" \
  TRIVY_VERSION="${trivy_version}" \
  TRIVY_CACHE_DIR="${test_root}/cache" \
  TRIVY_DB_SHA256="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" \
  SCAN_CONFIGURATION_SHA256="${configuration_sha}" \
  DEPENDENCY_HEALTH_REPOSITORY="cbusillo/odoo-docker" \
  bash "${scan_tool}" candidate-image "${candidate_commit}" "${baseline_commit}" \
    image:runtime:linux/amd64 "${image_source}" "${test_root}/scan-${image_source}"

  python3 - "${test_root}/scanner-arguments.txt" "${trivy_image}" <<'PY'
from pathlib import Path
import sys

arguments = Path(sys.argv[1]).read_bytes().decode().split("\0")[:-1]
image_index = arguments.index(sys.argv[2])
assert arguments[image_index + 1] == "image", arguments
scanner_arguments = arguments[image_index + 2:]
skipped_files = []
for index, argument in enumerate(scanner_arguments):
    if argument == "--skip-files":
        skipped_files.extend(scanner_arguments[index + 1].split(","))
    elif argument.startswith("--skip-files="):
        skipped_files.extend(argument.split("=", 1)[1].split(","))
assert sorted(skipped_files) == [
    "/usr/share/java/gettext.jar",
    "/usr/share/java/libintl-0.21.jar",
], f"unexpected scanner file exclusions: {skipped_files}"
PY
done

write_provenance "${test_root}/baseline-provenance.json" "${baseline_commit}" ""
write_provenance "${test_root}/candidate-provenance.json" "${candidate_commit}" "${baseline_commit}"

cat >"${test_root}/empty-trivy.json" <<'EOF'
{"Metadata":{"OS":{"Family":"ubuntu","Name":"24.04"}},"Results":[]}
EOF
if python3 "${tool}" snapshot \
  --trivy-json "${test_root}/empty-trivy.json" \
  --provenance "${test_root}/candidate-provenance.json" \
  --output "${test_root}/empty-snapshot.json"; then
  echo "empty scanner coverage unexpectedly passed" >&2
  exit 1
fi
write_report "${test_root}/baseline-trivy.json" "CVE-2026-1000" "HIGH" "1.0" \
  "baseline-image (ubuntu 24.04)"
write_report "${test_root}/candidate-trivy.json" "CVE-2026-1000" "HIGH" "1.0" \
  "candidate-image (ubuntu 24.04)"

python3 "${tool}" snapshot \
  --trivy-json "${test_root}/baseline-trivy.json" \
  --provenance "${test_root}/baseline-provenance.json" \
  --output "${test_root}/baseline.json"
python3 "${tool}" snapshot \
  --trivy-json "${test_root}/candidate-trivy.json" \
  --provenance "${test_root}/candidate-provenance.json" \
  --output "${test_root}/candidate.json"
python3 "${tool}" compare \
  --baseline "${test_root}/baseline.json" \
  --candidate "${test_root}/candidate.json" \
  --output "${test_root}/comparison.json"
jq -e '.policy_evaluation.status == "pass" and (.comparison.unchanged | length) == 1' \
  "${test_root}/comparison.json" >/dev/null

if python3 "${tool}" compare \
  --baseline "${test_root}/baseline.json" \
  --candidate "${test_root}/candidate.json" \
  --target-advisory CVE-2026-1000 \
  --output "${test_root}/unresolved-target.json"; then
  echo "unresolved target advisory unexpectedly passed" >&2
  exit 1
fi
jq -e '.policy_evaluation.reason_codes == ["target_advisory_unresolved"]' \
  "${test_root}/unresolved-target.json" >/dev/null

write_report "${test_root}/candidate-trivy.json" "CVE-2026-2000"
python3 "${tool}" snapshot \
  --trivy-json "${test_root}/candidate-trivy.json" \
  --provenance "${test_root}/candidate-provenance.json" \
  --output "${test_root}/candidate.json"
if python3 "${tool}" compare \
  --baseline "${test_root}/baseline.json" \
  --candidate "${test_root}/candidate.json" \
  --output "${test_root}/introduced.json"; then
  echo "introduced high finding unexpectedly passed" >&2
  exit 1
fi
jq -e '.policy_evaluation.reason_codes == ["introduced_high_or_critical"]' \
  "${test_root}/introduced.json" >/dev/null

write_report "${test_root}/candidate-trivy.json" "CVE-2026-1000" "CRITICAL" "2.0"
python3 "${tool}" snapshot \
  --trivy-json "${test_root}/candidate-trivy.json" \
  --provenance "${test_root}/candidate-provenance.json" \
  --output "${test_root}/candidate.json"
if python3 "${tool}" compare \
  --baseline "${test_root}/baseline.json" \
  --candidate "${test_root}/candidate.json" \
  --output "${test_root}/worsened.json"; then
  echo "worsened high finding unexpectedly passed" >&2
  exit 1
fi
jq -e '.policy_evaluation.reason_codes == ["worsened_to_high_or_critical"]' \
  "${test_root}/worsened.json" >/dev/null

write_report "${test_root}/candidate-trivy.json"
python3 "${tool}" snapshot \
  --trivy-json "${test_root}/candidate-trivy.json" \
  --provenance "${test_root}/candidate-provenance.json" \
  --output "${test_root}/candidate.json"
if python3 "${tool}" compare \
  --baseline "${test_root}/baseline.json" \
  --candidate "${test_root}/candidate.json" \
  --target-advisory CVE-2026-4040 \
  --output "${test_root}/missing-target.json"; then
  echo "missing target advisory unexpectedly passed" >&2
  exit 1
fi
jq -e '.policy_evaluation.reason_codes == ["target_advisory_missing_from_baseline"]' \
  "${test_root}/missing-target.json" >/dev/null

jq '.provenance.scan_configuration_sha256 = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"' \
  "${test_root}/candidate.json" >"${test_root}/mismatch.json"
set +e
python3 "${tool}" compare \
  --baseline "${test_root}/baseline.json" \
  --candidate "${test_root}/mismatch.json" \
  --output "${test_root}/mismatch-output.json" >/dev/null 2>"${test_root}/mismatch-error.txt"
mismatch_status=$?
set -e
[[ "${mismatch_status}" -eq 2 ]]
grep -q 'scan_configuration_sha256' "${test_root}/mismatch-error.txt"

amd64_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
arm64_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
cat >"${test_root}/manifest.json" <<EOF
{
  "schemaVersion": 2,
  "manifests": [
    {"digest": "${amd64_digest}", "platform": {"os": "linux", "architecture": "amd64"}},
    {"digest": "${arm64_digest}", "platform": {"os": "linux", "architecture": "arm64"}}
  ]
}
EOF
parent_digest="sha256:$(sha256sum "${test_root}/manifest.json" | awk '{print $1}')"
cat >"${test_root}/build-provenance.json" <<'EOF'
{"builder":"test","platforms":["linux/amd64","linux/arm64"]}
EOF
write_provenance "${test_root}/amd64-provenance.json" "${candidate_commit}" "" \
  "ghcr.io/cbusillo/odoo-docker@${amd64_digest}#linux/amd64"
write_provenance "${test_root}/arm64-provenance.json" "${candidate_commit}" "" \
  "ghcr.io/cbusillo/odoo-docker@${arm64_digest}#linux/arm64"
write_report "${test_root}/clean.json"
for platform in amd64 arm64; do
  python3 "${tool}" snapshot \
    --trivy-json "${test_root}/clean.json" \
    --provenance "${test_root}/${platform}-provenance.json" \
    --output "${test_root}/${platform}.json"
done
python3 "${tool}" artifact-evidence \
  --repository cbusillo/odoo-docker \
  --image ghcr.io/cbusillo/odoo-docker \
  --parent-digest "${parent_digest}" \
  --manifest-json "${test_root}/manifest.json" \
  --build-provenance "${test_root}/build-provenance.json" \
  --platform-snapshot "linux/amd64=${test_root}/amd64.json" \
  --platform-snapshot "linux/arm64=${test_root}/arm64.json" \
  --output "${test_root}/artifact-evidence.json"
jq -e '.status == "pass" and (.platforms | length) == 2' \
  "${test_root}/artifact-evidence.json" >/dev/null

echo "image dependency health checks passed"
