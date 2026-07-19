#!/usr/bin/env bash
set -euo pipefail

image_reference="${1:?Usage: scripts/test-downstream-helpers.sh <image-reference>}"
downstream_helper_repo="${ODOO_DOWNSTREAM_HELPER_REPO:-McMillan-Woods-Global/disable_odoo_online}"
downstream_helper_ref="${ODOO_DOWNSTREAM_HELPER_REF:-c69e4df113df460fb933a3519331fdadbca1a32f}"
support_ref="1111111111111111111111111111111111111111"
tenant_ref="2222222222222222222222222222222222222222"
external_ref="3333333333333333333333333333333333333333"
shared_ref="4444444444444444444444444444444444444444"

test_root="$(mktemp -d)"
trap 'rm -rf "${test_root}"' EXIT

legacy_root="${test_root}/legacy"
strict_support_root="${test_root}/strict-support"
strict_tenant_root="${test_root}/strict-tenant"
strict_external_root="${test_root}/strict-external"
layout_only_root="${test_root}/layout-only"
empty_project_root="${test_root}/empty-project"

write_source_marker() {
	local root="$1"
	local repository="$2"
	local ref="$3"
	local lock_path="${4:-}"

	if [[ -n "${lock_path}" ]]; then
		cat >"${root}/.odoo-python-source.json" <<EOF
{"repository":"${repository}","ref":"${ref}","lock_path":"${lock_path}"}
EOF
	else
		cat >"${root}/.odoo-python-source.json" <<EOF
{"repository":"${repository}","ref":"${ref}"}
EOF
	fi
}

write_external_source_marker() {
	local root="$1"
	local repository="$2"
	local ref="$3"

	printf '%s@%s\n' "${repository}" "${ref}" >"${root}.odoo-source"
}

mkdir -p \
	"${legacy_root}/tools" \
	"${legacy_root}/addons/test_local_pkg/test_local_pkg" \
	"${strict_support_root}" \
	"${strict_tenant_root}/addons/test_local_pkg/test_local_pkg" \
	"${strict_tenant_root}/addons/shared/test_shared_pkg/test_shared_pkg" \
	"${strict_external_root}/test_external/test_external_pkg" \
	"${layout_only_root}" \
	"${empty_project_root}/addons/code_only"

cat >"${legacy_root}/pyproject.toml" <<'EOF'
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "downstream-root"
version = "0.0.0"
dependencies = [
    "python-slugify==8.0.4",
]

[project.optional-dependencies]
dev = [
    "humanize==4.13.0",
]
EOF

cat >"${legacy_root}/tools/__init__.py" <<'EOF'
"""Downstream helper test package."""
EOF

cat >"${legacy_root}/addons/test_local_pkg/pyproject.toml" <<'EOF'
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "test-local-pkg"
version = "0.0.0"
EOF

cat >"${legacy_root}/addons/test_local_pkg/test_local_pkg/__init__.py" <<'EOF'
VALUE = "legacy-local-package-installed"
EOF

cat >"${strict_support_root}/pyproject.toml" <<'EOF'
[project]
name = "odoo-support-runtime"
version = "0.0.0"
dependencies = [
    "hatchling==1.27.0",
    "python-slugify==8.0.4",
]

[tool.uv]
package = false
EOF

cat >"${strict_tenant_root}/pyproject.toml" <<'EOF'
[project]
name = "tenant-runtime"
version = "0.0.0"
requires-python = ">=3.13"
dependencies = [
    "hatchling==1.27.0",
]

[tool.uv]
package = false

[tool.uv.workspace]
members = ["addons/test_local_pkg", "addons/shared/*"]

[tool.uv.sources]
test-local-pkg = { workspace = true }
test-shared-pkg = { workspace = true }
EOF

cat >"${strict_tenant_root}/addons/test_local_pkg/pyproject.toml" <<'EOF'
[build-system]
requires = ["hatchling==1.27.0"]
build-backend = "hatchling.build"

[project]
name = "test-local-pkg"
version = "0.0.0"
dependencies = [
    "humanize==4.13.0",
]

[project.optional-dependencies]
dev = [
    "boltons==25.0.0",
]

[tool.hatch.build.targets.wheel]
packages = ["test_local_pkg"]

[tool.uv]
package = false
EOF

cat >"${strict_tenant_root}/addons/test_local_pkg/test_local_pkg/__init__.py" <<'EOF'
VALUE = "strict-local-package-installed"
EOF

cat >"${strict_tenant_root}/addons/shared/test_shared_pkg/pyproject.toml" <<'EOF'
[build-system]
requires = ["hatchling==1.27.0"]
build-backend = "hatchling.build"

[project]
name = "test-shared-pkg"
version = "0.0.0"
dependencies = []

[tool.hatch.build.targets.wheel]
packages = ["test_shared_pkg"]

[tool.uv]
package = false
EOF

cat >"${strict_tenant_root}/addons/shared/test_shared_pkg/test_shared_pkg/__init__.py" <<'EOF'
VALUE = "strict-shared-package-installed"
EOF

cat >"${strict_external_root}/test_external/requirements.txt" <<'EOF'
boltons==25.0.0
EOF

cat >"${strict_external_root}/test_external/pyproject.toml" <<'EOF'
[build-system]
requires = ["hatchling==1.27.0"]
build-backend = "hatchling.build"

[project]
name = "test-external-pkg"
version = "0.0.0"
dependencies = [
    "boltons==25.0.0",
]

[tool.hatch.build.targets.wheel]
packages = ["test_external_pkg"]
EOF

cat >"${strict_external_root}/test_external/test_external_pkg/__init__.py" <<'EOF'
VALUE = "strict-external-package-installed"
EOF

cat >"${empty_project_root}/addons/code_only/__manifest__.py" <<'EOF'
{"name": "Code-only strict fixture", "version": "1.0.0"}
EOF

write_external_source_marker \
	"${strict_external_root}/test_external" \
	"example/external-addons" \
	"${external_ref}"

printf '2\n' >"${strict_support_root}/.odoo-python-sync-layout"
printf '2\n' >"${layout_only_root}/.odoo-python-sync-layout"
write_source_marker \
	"${strict_support_root}" \
	"cbusillo/odoo-devkit" \
	"${support_ref}" \
	"docker/runtime-python/uv.lock"
write_source_marker "${strict_tenant_root}" "example/tenant-runtime" "${tenant_ref}"
write_source_marker \
	"${strict_tenant_root}/addons/shared" \
	"example/shared-addons" \
	"${shared_ref}"

chmod -R a+rX "${test_root}"

generate_lockfile() {
	local root="$1"
	local container_root="$2"

	docker run --rm \
		--user root \
		-v "${root}:${container_root}" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; uv lock --project ${container_root} >/dev/null"
}

assert_command_fails() {
	local label="$1"
	shift
	if "$@"; then
		echo "Expected failure: ${label}" >&2
		exit 1
	fi
}

assert_missing_legacy_lock_fails() {
	assert_command_fails "legacy pyproject without lock" \
		docker run --rm \
		-v "${legacy_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_strict_pair_mismatch_fails() {
	local incomplete_root="${test_root}/incomplete-support"
	mkdir -p "${incomplete_root}"
	cp "${strict_support_root}/pyproject.toml" "${incomplete_root}/pyproject.toml"
	cp "${strict_support_root}/.odoo-python-sync-layout" "${incomplete_root}/.odoo-python-sync-layout"
	cp "${strict_support_root}/.odoo-python-source.json" "${incomplete_root}/.odoo-python-source.json"
	chmod -R a+rX "${incomplete_root}"

	assert_command_fails "strict support pyproject without lock" \
		docker run --rm \
		-v "${incomplete_root}:/opt/runtime" \
		-v "${empty_project_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_stale_tenant_lock_fails() {
	local stale_root="${test_root}/stale-tenant"
	cp -R "${strict_tenant_root}" "${stale_root}"
	cat >>"${stale_root}/pyproject.toml" <<'EOF'

[project.optional-dependencies]
stale = ["requests==2.32.5"]
EOF
	chmod -R a+rX "${stale_root}"

	assert_command_fails "stale tenant lock" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${stale_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_tampered_tenant_lock_fails() {
	local tampered_root="${test_root}/tampered-tenant-lock"
	cp -R "${strict_tenant_root}" "${tampered_root}"
	sed -i.bak 's/version = "4.13.0"/version = "4.12.0"/' \
		"${tampered_root}/uv.lock"
	rm -f "${tampered_root}/uv.lock.bak"
	grep -q 'version = "4.12.0"' "${tampered_root}/uv.lock"
	chmod -R a+rX "${tampered_root}"

	assert_command_fails "tampered tenant lock" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${tampered_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_owned_requirements_fail() {
	local invalid_root="${test_root}/owned-requirements"
	cp -R "${strict_tenant_root}" "${invalid_root}"
	printf 'requests==2.32.5\n' >"${invalid_root}/addons/test_local_pkg/requirements.txt"
	chmod -R a+rX "${invalid_root}"

	assert_command_fails "owned addon requirements outside workspace metadata" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${invalid_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_local_source_fails() {
	local invalid_root="${test_root}/local-source"
	cp -R "${strict_tenant_root}" "${invalid_root}"
	sed -i.bak \
		'/"humanize==4.13.0",/a\
    "unsafe-local @ file:///tmp/unsafe-local",' \
		"${invalid_root}/addons/test_local_pkg/pyproject.toml"
	rm -f "${invalid_root}/addons/test_local_pkg/pyproject.toml.bak"
	chmod -R a+rX "${invalid_root}"

	assert_command_fails "tenant local dependency source" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${invalid_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_compact_direct_source_fails() {
	local invalid_root="${test_root}/compact-local-source"
	cp -R "${strict_tenant_root}" "${invalid_root}"
	sed -i.bak \
		'/"humanize==4.13.0",/a\
    "unsafe-local@file:///tmp/unsafe-local",' \
		"${invalid_root}/addons/test_local_pkg/pyproject.toml"
	rm -f "${invalid_root}/addons/test_local_pkg/pyproject.toml.bak"
	chmod -R a+rX "${invalid_root}"

	assert_command_fails "compact tenant local dependency source" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${invalid_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_mutable_build_requirement_fails() {
	local invalid_root="${test_root}/mutable-build-requirement"
	cp -R "${strict_tenant_root}" "${invalid_root}"
	sed -i.bak 's/hatchling==1.27.0/hatchling>=1.25/' \
		"${invalid_root}/addons/test_local_pkg/pyproject.toml"
	rm -f "${invalid_root}/addons/test_local_pkg/pyproject.toml.bak"
	chmod -R a+rX "${invalid_root}"

	assert_command_fails "mutable owned build requirement" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${invalid_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_bare_external_archive_fails() {
	local invalid_root="${test_root}/external-archive"
	mkdir -p "${invalid_root}/test_external"
	printf 'https://example.invalid/packages/unsafe.whl\n' \
		>"${invalid_root}/test_external/requirements.txt"
	write_external_source_marker \
		"${invalid_root}/test_external" \
		"example/external-addons" \
		"${external_ref}"
	chmod -R a+rX "${invalid_root}"

	assert_command_fails "bare external archive requirement" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		-v "${invalid_root}:/opt/extra_addons" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_mutable_external_ref_fails() {
	assert_command_fails "mutable external addon ref" \
		docker run --rm \
		-e ODOO_ADDON_REPOSITORIES="${downstream_helper_repo}@main" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-fetch-addons.sh"
}

assert_external_repository_traversal_fails() {
	assert_command_fails "external repository path traversal" \
		docker run --rm \
		-e ODOO_ADDON_REPOSITORIES="../unsafe@${external_ref}" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-fetch-addons.sh"
}

assert_invalid_source_lock_path_fails() {
	local test_case label lock_path invalid_root
	for test_case in \
		"traversal|../uv.lock" \
		"absolute|/tmp/uv.lock" \
		"wrong-filename|locks/runtime.lock"; do
		IFS='|' read -r label lock_path <<<"${test_case}"
		invalid_root="${test_root}/invalid-source-lock-path-${label}"
		cp -R "${strict_support_root}" "${invalid_root}"
		write_source_marker \
			"${invalid_root}" \
			"cbusillo/odoo-devkit" \
			"${support_ref}" \
			"${lock_path}"

		assert_command_fails "source marker lock path ${label}" \
			docker run --rm \
			-v "${invalid_root}:/opt/runtime" \
			-v "${strict_tenant_root}:/opt/project" \
			--entrypoint /bin/bash \
			"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
	done
}

assert_external_duplicate_ref_fails() {
	local output
	if output="$(docker run --rm \
		-e ODOO_ADDON_REPOSITORIES="${downstream_helper_repo}@${downstream_helper_ref},${downstream_helper_repo}@${external_ref}" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-fetch-addons.sh" 2>&1)"; then
		echo "Expected failure: external repository duplicate commits" >&2
		exit 1
	fi
	printf '%s\n' "${output}"
	if grep -q '^Fetching ' <<<"${output}"; then
		echo "Duplicate repository commits must fail before fetch" >&2
		exit 1
	fi
	grep -q 'cannot use multiple commits' <<<"${output}"
}

assert_external_symlink_escape_fails() {
	local fake_bin="${test_root}/fake-git"
	mkdir -p "${fake_bin}"
	cat >"${fake_bin}/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

while [[ "${1:-}" == "-c" ]]; do
    shift 2
done
[[ "${1:-}" == "-C" ]]
worktree="$2"
shift 2
command="$1"
shift

case "${command}" in
    init)
        mkdir -p "${worktree}/.git"
        ;;
    remote)
        ;;
    fetch)
        printf '%s\n' "${@: -1}" >"${worktree}/.fake-ref"
        ;;
    rev-parse)
        cat "${worktree}/.fake-ref"
        ;;
    checkout)
        mkdir -p "${worktree}/addons"
        ln -s /opt/launchplane/addons/launchplane_runtime_health "${worktree}/addons/escape"
        ;;
    *)
        echo "Unexpected fake git command: ${command}" >&2
        exit 1
        ;;
esac
EOF
	chmod +x "${fake_bin}/git"

	assert_command_fails "external addon symlink escape" \
		docker run --rm \
		-e GITHUB_TOKEN=test-token \
		-e ODOO_ADDON_REPOSITORIES="example/symlink-escape@${external_ref}" \
		-e PATH=/tmp/fake-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
		-v "${fake_bin}:/tmp/fake-bin:ro" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-fetch-addons.sh"
}

assert_strict_skip_addons_fails() {
	assert_command_fails "strict skip addon ambiguity" \
		docker run --rm \
		-e ODOO_PYTHON_SYNC_SKIP_ADDONS=test_local_pkg \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_target_platform_mismatch_fails() {
	assert_command_fails "target platform mismatch" \
		docker run --rm \
		-e TARGETPLATFORM=windows/amd64 \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_external_conflict_fails() {
	local conflict_root="${test_root}/external-conflict"
	mkdir -p "${conflict_root}/test_external"
	printf 'python-slugify==7.0.0\n' >"${conflict_root}/test_external/requirements.txt"
	write_external_source_marker \
		"${conflict_root}/test_external" \
		"example/external-addons" \
		"${external_ref}"
	chmod -R a+rX "${conflict_root}"

	assert_command_fails "external compatibility changed locked package" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		-v "${conflict_root}:/opt/extra_addons" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_external_same_version_replacement_fails() {
	local conflict_root="${test_root}/external-source-replacement"
	mkdir -p "${conflict_root}/test_external"
	cat >"${conflict_root}/test_external/pyproject.toml" <<'EOF'
[build-system]
requires = ["hatchling==1.27.0"]
build-backend = "hatchling.build"

[project]
name = "python-slugify"
version = "8.0.4"
EOF
	write_external_source_marker \
		"${conflict_root}/test_external" \
		"example/external-addons" \
		"${external_ref}"
	chmod -R a+rX "${conflict_root}"

	assert_command_fails "external same-version source replacement" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		-v "${conflict_root}:/opt/extra_addons" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_cross_distribution_file_overlap_fails() {
	local conflict_root="${test_root}/external-file-overlap"
	mkdir -p "${conflict_root}/shadow_package/urllib3"
	cat >"${conflict_root}/shadow_package/pyproject.toml" <<'EOF'
[build-system]
requires = ["hatchling==1.27.0"]
build-backend = "hatchling.build"

[project]
name = "shadow-package"
version = "0.0.0"

[tool.hatch.build.targets.wheel]
packages = ["urllib3"]
EOF
	cat >"${conflict_root}/shadow_package/urllib3/__init__.py" <<'EOF'
raise RuntimeError("cross-distribution overwrite")
EOF
	write_external_source_marker \
		"${conflict_root}/shadow_package" \
		"example/external-addons" \
		"${external_ref}"
	chmod -R a+rX "${conflict_root}"

	assert_command_fails "cross-distribution file overlap" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		-v "${conflict_root}:/opt/extra_addons" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_nested_external_marker_is_ignored() {
	local nested_root="${test_root}/nested-external-marker"
	local checkout_root="${nested_root}/_checkouts/source"
	local project_root="${checkout_root}/addons/test_external"
	mkdir -p "${project_root}"
	printf 'boltons==25.0.0\n' >"${project_root}/requirements.txt"
	printf '%s@%s\n' "forged/source" "4444444444444444444444444444444444444444" \
		>"${checkout_root}/addons/test_external.odoo-source"
	write_external_source_marker \
		"${checkout_root}" \
		"example/external-addons" \
		"${external_ref}"
	ln -s "_checkouts/source/addons/test_external" "${nested_root}/test_external"
	chmod -R a+rX "${nested_root}"

	docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		-v "${nested_root}:/opt/extra_addons" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "
set -euo pipefail
odoo-python-sync.sh prod
/venv/bin/python - <<'PY'
import json
from pathlib import Path

evidence = json.loads(Path('/opt/launchplane/evidence/dependency-provenance.json').read_text())
assert evidence['external_compatibility_inputs'] == [{
    'dependency_file_path': 'addons/test_external/requirements.txt',
    'dependency_file_sha256': evidence['external_compatibility_inputs'][0]['dependency_file_sha256'],
    'format': 'requirements_txt',
    'resolution_posture': 'exact_source_unlocked',
    'source_ref': '${external_ref}',
    'source_repository': 'example/external-addons',
}]
PY
"
}

assert_base_package_override_fails() {
	local conflict_root="${test_root}/base-package-conflict"
	cp -R "${strict_support_root}" "${conflict_root}"
	sed -i.bak \
		'/"python-slugify==8.0.4",/a\
    "urllib3==2.6.3",' \
		"${conflict_root}/pyproject.toml"
	rm -f "${conflict_root}/pyproject.toml.bak" "${conflict_root}/uv.lock"
	chmod -R a+rX "${conflict_root}"
	generate_lockfile "${conflict_root}" /opt/runtime

	assert_command_fails "locked layer changed base package" \
		docker run --rm \
		-v "${conflict_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_layered_lock_conflict_fails() {
	local conflict_root="${test_root}/tenant-lock-conflict"
	cp -R "${strict_tenant_root}" "${conflict_root}"
	sed -i.bak 's/humanize==4.13.0/python-slugify==7.0.0/' \
		"${conflict_root}/addons/test_local_pkg/pyproject.toml"
	rm -f "${conflict_root}/addons/test_local_pkg/pyproject.toml.bak" "${conflict_root}/uv.lock"
	chmod -R a+rX "${conflict_root}"
	generate_lockfile "${conflict_root}" /opt/project

	assert_command_fails "support and tenant lock conflict" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${conflict_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod"
}

assert_final_environment_incompatibility_fails() {
	assert_command_fails "final environment incompatibility" \
		docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		-v "${test_root}/empty-external:/opt/extra_addons" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "set -euo pipefail; uv pip uninstall --python /venv/bin/python urllib3; odoo-python-sync.sh prod"
}

assert_strict_evidence_is_deterministic() {
	local first_evidence="${test_root}/evidence-first"
	local second_evidence="${test_root}/evidence-second"
	mkdir -p "${first_evidence}" "${second_evidence}"
	chmod -R a+rwx "${first_evidence}" "${second_evidence}"

	for evidence_root in "${first_evidence}" "${second_evidence}"; do
		docker run --rm \
			-v "${strict_support_root}:/opt/runtime" \
			-v "${strict_tenant_root}:/opt/project" \
			-v "${test_root}/empty-external:/opt/extra_addons" \
			-v "${evidence_root}:/opt/launchplane/evidence" \
			--entrypoint /bin/bash \
			"${image_reference}" -lc "set -euo pipefail; odoo-python-sync.sh prod >/dev/null"
	done

	cmp \
		"${first_evidence}/dependency-provenance.json" \
		"${second_evidence}/dependency-provenance.json"
}

run_legacy_sync_check() {
	local sync_mode="$1"

	docker run --rm \
		-e ODOO_ADDON_REPOSITORIES="${downstream_helper_repo}@${downstream_helper_ref}" \
		-v "${legacy_root}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "
set -euo pipefail
odoo-fetch-addons.sh
odoo-python-sync.sh ${sync_mode}
test -d /opt/extra_addons/${downstream_helper_repo##*/}
test -f /opt/extra_addons/${downstream_helper_repo##*/}/__manifest__.py
/venv/bin/python - <<'PY'
import importlib.util
import json
from pathlib import Path
from test_local_pkg import VALUE

assert VALUE == 'legacy-local-package-installed'
assert importlib.util.find_spec('slugify') is not None
humanize_spec = importlib.util.find_spec('humanize')
if '${sync_mode}' == 'dev':
    assert humanize_spec is not None
else:
    assert humanize_spec is None
evidence = json.loads(Path('/opt/launchplane/evidence/dependency-provenance.json').read_text())
assert evidence['layout'] == 'legacy_single_root'
assert evidence['publishable'] is False
PY
"
}

run_strict_sync_check() {
	local sync_mode="$1"
	local external_root="$2"

	docker run --rm \
		-v "${strict_support_root}:/opt/runtime" \
		-v "${strict_tenant_root}:/opt/project" \
		-v "${external_root}:/opt/extra_addons" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "
set -euo pipefail
odoo-python-sync.sh ${sync_mode}
/venv/bin/python - <<'PY'
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
from pathlib import Path
from test_local_pkg import VALUE
from test_shared_pkg import VALUE as shared_value

assert VALUE == 'strict-local-package-installed'
assert shared_value == 'strict-shared-package-installed'
assert importlib.util.find_spec('slugify') is not None
assert importlib.util.find_spec('humanize') is not None
assert importlib.metadata.version('requests') == '2.34.2'
boltons_spec = importlib.util.find_spec('boltons')
if '${sync_mode}' == 'dev' or '${external_root}' != '${test_root}/empty-external':
    assert boltons_spec is not None
else:
    assert boltons_spec is None

evidence = json.loads(Path('/opt/launchplane/evidence/dependency-provenance.json').read_text())
assert evidence['layout'] == 'two_lock'
assert evidence['publishable'] is True
architecture = {
    'aarch64': 'arm64',
    'amd64': 'amd64',
    'arm64': 'arm64',
    'ppc64le': 'ppc64le',
    'x86_64': 'amd64',
}[platform.machine().lower()]
assert evidence['target_platform'] == f'linux/{architecture}'
assert [item['scope'] for item in evidence['uv_locks']] == ['support_runtime', 'tenant']
assert evidence['uv_locks'][0]['source_ref'] == '${support_ref}'
assert evidence['uv_locks'][1]['source_ref'] == '${tenant_ref}'
assert evidence['uv_locks'][0]['path'] == 'docker/runtime-python/uv.lock'
assert evidence['uv_locks'][1]['path'] == 'uv.lock'
environment = evidence['python_environment']
assert environment['package_count'] == len(environment['packages'])
canonical = json.dumps(
    environment['packages'],
    ensure_ascii=True,
    separators=(',', ':'),
    sort_keys=True,
)
assert environment['packages_sha256'] == hashlib.sha256(canonical.encode()).hexdigest()
packages = {item['name']: item for item in environment['packages']}
assert packages['test-local-pkg']['source'] == {
    'kind': 'vcs',
    'repository': 'example/tenant-runtime',
    'commit': '${tenant_ref}',
}
assert packages['test-shared-pkg']['source'] == {
    'kind': 'vcs',
    'repository': 'example/shared-addons',
    'commit': '${shared_ref}',
}
if '${external_root}' == '${strict_external_root}':
    from test_external_pkg import VALUE as external_value

    assert external_value == 'strict-external-package-installed'
    assert packages['test-external-pkg']['source'] == {
        'kind': 'vcs',
        'repository': 'example/external-addons',
        'commit': '${external_ref}',
    }
    assert evidence['external_compatibility_inputs'] == [
        {
            'dependency_file_path': 'pyproject.toml',
            'dependency_file_sha256': hashlib.sha256(
                Path('/opt/extra_addons/test_external/pyproject.toml').read_bytes()
            ).hexdigest(),
            'format': 'pyproject_toml',
            'resolution_posture': 'exact_source_unlocked',
            'source_ref': '${external_ref}',
            'source_repository': 'example/external-addons',
        },
        {
            'dependency_file_path': 'requirements.txt',
            'dependency_file_sha256': hashlib.sha256(
                Path('/opt/extra_addons/test_external/requirements.txt').read_bytes()
            ).hexdigest(),
            'format': 'requirements_txt',
            'resolution_posture': 'exact_source_unlocked',
            'source_ref': '${external_ref}',
            'source_repository': 'example/external-addons',
        },
    ]
else:
    assert evidence['external_compatibility_inputs'] == []
PY
"
}

assert_vcs_repository_canonicalization() {
	docker run --rm -i \
		--entrypoint /venv/bin/python \
		"${image_reference}" - <<'PY'
import json
import runpy

sync = runpy.run_path('/usr/local/lib/odoo-python-sync.py')
normalize = sync['normalize_vcs_repository']
package_source = sync['package_source']
sync_error = sync['SyncError']

assert normalize('https://github.com/OCA/openupgradelib.git') == 'OCA/openupgradelib'
assert normalize('git+https://github.com/sacherjj/simple_zpl2') == 'sacherjj/simple_zpl2'
assert normalize('ssh://git@github.com/OCA/openupgradelib.git') == 'OCA/openupgradelib'
assert normalize('https://git.example.com/group/project.git') == (
    'https://git.example.com/group/project'
)
assert normalize('ssh://git@git.example.com:2222/group/project.git') == (
    'ssh://git@git.example.com:2222/group/project'
)

for value in (
    'file:///tmp/project',
    'https://operator@example.com/owner/project',
    'ssh://git:secret@example.com/owner/project',
    'https://example.com/owner/project?token=secret',
    'https://example.com/owner/project#fragment',
    'https://example.com/owner/../project',
    'https://example.com/owner/%2e%2e/project',
):
    try:
        normalize(value)
    except sync_error:
        pass
    else:
        raise AssertionError(f'unsafe repository identity was accepted: {value}')


class DirectUrlDistribution:
    metadata = {'Name': 'simple-zpl2'}

    def read_text(self, filename: str) -> str | None:
        if filename != 'direct_url.json':
            return None
        return json.dumps(
            {
                'url': 'https://github.com/sacherjj/simple_zpl2',
                'vcs_info': {
                    'commit_id': '177de4500279034d7bf4b6a8ba0244730d9c1219',
                    'vcs': 'git',
                },
            }
        )


assert package_source(DirectUrlDistribution(), {}) == {
    'kind': 'vcs',
    'repository': 'sacherjj/simple_zpl2',
    'commit': '177de4500279034d7bf4b6a8ba0244730d9c1219',
}
PY
}

run_partial_strict_check() {
	local support_mount="$1"
	local tenant_mount="$2"
	local expected_scope="$3"

	docker run --rm \
		-v "${support_mount}:/opt/runtime" \
		-v "${tenant_mount}:/opt/project" \
		--entrypoint /bin/bash \
		"${image_reference}" -lc "
set -euo pipefail
odoo-python-sync.sh prod
/venv/bin/python - <<'PY'
import json
from pathlib import Path

evidence = json.loads(Path('/opt/launchplane/evidence/dependency-provenance.json').read_text())
assert evidence['publishable'] is False
assert [item['scope'] for item in evidence['uv_locks']] == ['${expected_scope}']
PY
"
}

mkdir -p "${test_root}/empty-external"
chmod -R a+rX "${test_root}/empty-external"

assert_missing_legacy_lock_fails
generate_lockfile "${legacy_root}" /opt/project
generate_lockfile "${strict_support_root}" /opt/runtime
generate_lockfile "${strict_tenant_root}" /opt/project

assert_strict_pair_mismatch_fails
assert_stale_tenant_lock_fails
assert_tampered_tenant_lock_fails
assert_owned_requirements_fail
assert_local_source_fails
assert_compact_direct_source_fails
assert_mutable_build_requirement_fails
assert_bare_external_archive_fails
assert_mutable_external_ref_fails
assert_external_repository_traversal_fails
assert_invalid_source_lock_path_fails
assert_external_duplicate_ref_fails
assert_external_symlink_escape_fails
assert_strict_skip_addons_fails
assert_target_platform_mismatch_fails
assert_vcs_repository_canonicalization

run_legacy_sync_check prod
run_legacy_sync_check dev
run_partial_strict_check "${strict_support_root}" "${empty_project_root}" support_runtime
run_partial_strict_check "${layout_only_root}" "${strict_tenant_root}" tenant
run_strict_sync_check prod "${strict_external_root}"
run_strict_sync_check dev "${test_root}/empty-external"
assert_base_package_override_fails
assert_layered_lock_conflict_fails
assert_external_conflict_fails
assert_external_same_version_replacement_fails
assert_cross_distribution_file_overlap_fails
assert_nested_external_marker_is_ignored
assert_final_environment_incompatibility_fails
assert_strict_evidence_is_deterministic

echo "downstream helper tests passed: ${image_reference}"
