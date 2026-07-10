#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d)"
trap 'find "${test_root}" -depth -delete' EXIT

requirements_file="${test_root}/requirements.txt"
overrides_file="${test_root}/overrides.txt"
output_file="${test_root}/output.txt"

run_check() {
	python3 "${repo_root}/scripts/check-requirements-overrides.py" \
		--requirements "${requirements_file}" \
		--overrides "${overrides_file}" \
		--python-version 3.13
}

printf 'lxml-html-clean\n' >"${requirements_file}"
printf 'lxml-html-clean==0.4.5\n' >"${overrides_file}"
run_check >"${output_file}"
grep -Fq 'requirements overrides remain valid' "${output_file}"

printf 'demo\n' >"${requirements_file}"
printf 'demo==1.2.0\n' >"${overrides_file}"
if run_check >"${output_file}"; then
	echo 'Expected an unregistered unpinned requirement to require review.' >&2
	exit 1
fi
grep -Fq 'upstream requirement is unpinned; exact override requires review' "${output_file}"

printf 'demo==1.1.0 ; python_version >= "3.13"\n' >"${requirements_file}"
run_check >"${output_file}"
grep -Fq 'requirements overrides remain valid' "${output_file}"

printf 'demo==1.2.0\n' >"${requirements_file}"
if run_check >"${output_file}"; then
	echo 'Expected an exact upstream pin to make the override stale.' >&2
	exit 1
fi
grep -Fq 'upstream pins 1.2.0, override pins 1.2.0' "${output_file}"

printf 'demo==1.3.0\n' >"${requirements_file}"
if run_check >"${output_file}"; then
	echo 'Expected a higher exact upstream pin to make the override stale.' >&2
	exit 1
fi
grep -Fq 'upstream pins 1.3.0, override pins 1.2.0' "${output_file}"

printf 'demo>=1.3.0\n' >"${requirements_file}"
if run_check >"${output_file}"; then
	echo 'Expected a non-exact upstream constraint to require review.' >&2
	exit 1
fi
grep -Fq 'upstream uses non-exact constraint >=1.3.0' "${output_file}"

printf 'demo==1.1.0,>=1.0.0\n' >"${requirements_file}"
if run_check >"${output_file}"; then
	echo 'Expected mixed exact and range constraints to require review.' >&2
	exit 1
fi
grep -Fq 'upstream uses non-exact constraint' "${output_file}"

printf 'other==1.0.0\n' >"${requirements_file}"
if run_check >"${output_file}"; then
	echo 'Expected a missing upstream requirement to reject the override.' >&2
	exit 1
fi
grep -Fq 'no active upstream requirement for Python 3.13' "${output_file}"

echo 'requirements override checks passed'
