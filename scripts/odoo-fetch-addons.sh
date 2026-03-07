#!/usr/bin/env bash
set -euo pipefail

readonly target_root="/opt/extra_addons"
readonly checkout_root="${target_root}/_checkouts"
readonly repository_marker_file=".odoo-fetch-repository"
readonly repositories_raw="${ODOO_ADDON_REPOSITORIES:-}"
repositories="$(printf '%s' "${repositories_raw}" | tr -d '\n' | sed 's/[[:space:]]*,[[:space:]]*/,/g; s/^,//; s/,$//')"
readonly repositories

download_archive() {
	local repository_full_name="$1"
	local repository_ref="$2"
	local target_directory="$3"
	local archive_url="https://codeload.github.com/${repository_full_name}/tar.gz/${repository_ref}"
	local tmp_archive
	local tmp_extract_root
	local extracted_root

	tmp_archive="$(mktemp /tmp/odoo-addon-archive-XXXXXX)"
	tmp_extract_root="$(mktemp -d /tmp/odoo-addon-extract-XXXXXX)"

	echo "Fetching ${repository_full_name}@${repository_ref}"
	if [[ -n "${GITHUB_TOKEN:-}" ]]; then
		curl --fail --location --show-error --silent \
			-H "Authorization: Bearer ${GITHUB_TOKEN}" \
			-H "Accept: application/vnd.github+json" \
			"${archive_url}" \
			-o "${tmp_archive}"
	else
		curl --fail --location --show-error --silent "${archive_url}" -o "${tmp_archive}"
	fi

	tar -xzf "${tmp_archive}" -C "${tmp_extract_root}"
	extracted_root="$(find "${tmp_extract_root}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
	if [[ -z "${extracted_root}" ]]; then
		echo "Missing extracted repository directory for ${repository_full_name}@${repository_ref}" >&2
		rm -f "${tmp_archive}"
		rm -rf "${tmp_extract_root}"
		exit 1
	fi

	rm -rf "${target_directory}"
	mkdir -p "$(dirname "${target_directory}")"
	mv "${extracted_root}" "${target_directory}"

	rm -f "${tmp_archive}"
	rm -rf "${tmp_extract_root}"
}

resolve_checkout_directory() {
	local repository="$1"
	local repository_name="$2"
	local repository_checksum

	# Keep extracted checkouts outside the public addon namespace so downstream
	# symlinks stay stable across ref updates and only exposed addon names appear
	# under /opt/extra_addons.
	printf -v repository_checksum '%s' "$(printf '%s' "${repository}" | cksum | cut -d' ' -f1)"
	printf '%s' "${checkout_root}/${repository_name}-${repository_checksum}"
}

repository_root_is_addon() {
	local repository_root="$1"
	[[ -f "${repository_root}/__manifest__.py" || -f "${repository_root}/__openerp__.py" ]]
}

publish_single_addon_repository() {
	local repository_root="$1"
	local repository_name="$2"
	local repository_source="$3"
	local public_target="${target_root}/${repository_name}"
	local marker_path="${public_target}/${repository_marker_file}"
	local existing_source

	if [[ -e "${public_target}" ]]; then
		if [[ -f "${marker_path}" ]]; then
			existing_source="$(cat "${marker_path}")"
			if [[ "${existing_source}" != "${repository_source}" ]]; then
				echo "Addon path collision for ${repository_name} in ${target_root}; existing checkout belongs to ${existing_source}." >&2
				exit 1
			fi
		else
			echo "Addon path collision for ${repository_name} in ${target_root}; resolve duplicate repositories before building." >&2
			exit 1
		fi
		rm -rf "${public_target}"
	fi

	mv "${repository_root}" "${public_target}"
	printf '%s\n' "${repository_source}" >"${public_target}/${repository_marker_file}"
}

link_modules() {
	local repository_root="$1"
	local module_root="$2"
	local repository_name="$3"
	local scan_roots=()
	local scan_root
	local module_dir
	local module_name
	local link_path
	local link_target
	local single_link_path

	if repository_root_is_addon "${repository_root}"; then
		single_link_path="${module_root}/${repository_name}"

		if [[ -L "${single_link_path}" ]]; then
			if [[ "$(readlink "${single_link_path}")" == "${repository_root}" ]]; then
				return
			fi
		fi

		if [[ -e "${single_link_path}" ]]; then
			echo "Addon path collision for ${repository_name} in ${module_root}; resolve duplicate repositories before building." >&2
			exit 1
		fi

		ln -s "${repository_root}" "${single_link_path}"
		return
	fi

	if [[ -d "${repository_root}/enterprise" ]]; then
		scan_roots+=("${repository_root}/enterprise")
	fi
	if [[ -d "${repository_root}/addons" ]]; then
		scan_roots+=("${repository_root}/addons")
	fi
	if [[ -d "${repository_root}/odoo/addons" ]]; then
		scan_roots+=("${repository_root}/odoo/addons")
	fi
	if [[ "${#scan_roots[@]}" -eq 0 ]]; then
		scan_roots+=("${repository_root}")
	fi

	shopt -s nullglob
	for scan_root in "${scan_roots[@]}"; do
		for module_dir in "${scan_root}"/*; do
			[[ -d "${module_dir}" ]] || continue
			if [[ ! -f "${module_dir}/__manifest__.py" && ! -f "${module_dir}/__openerp__.py" ]]; then
				continue
			fi

			module_name="$(basename "${module_dir}")"
			link_path="${module_root}/${module_name}"
			if [[ -L "${link_path}" ]]; then
				link_target="$(readlink "${link_path}")"
				if [[ "${link_target}" == "${module_dir}" ]]; then
					continue
				fi
			fi
			if [[ -e "${link_path}" ]]; then
				echo "Addon path collision for ${module_name} in ${module_root}; resolve duplicate repositories before building." >&2
				exit 1
			fi

			ln -s "${module_dir}" "${link_path}"
		done
	done
	shopt -u nullglob
}

mkdir -p "${target_root}"
mkdir -p "${checkout_root}"

if [[ -z "${repositories}" ]]; then
	echo "ODOO_ADDON_REPOSITORIES is empty; skipping external addon fetch."
	exit 0
fi

IFS=',' read -r -a repository_entries <<<"${repositories}"
for raw_repository in "${repository_entries[@]}"; do
	repository="$(printf '%s' "${raw_repository}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
	[[ -n "${repository}" ]] || continue

	repository_ref="main"
	if [[ "${repository}" == *"@"* ]]; then
		repository_ref="${repository##*@}"
		repository="${repository%@*}"
	fi

	repository_name="${repository##*/}"
	repository_target="$(resolve_checkout_directory "${repository}" "${repository_name}")"
	download_archive "${repository}" "${repository_ref}" "${repository_target}"
	if repository_root_is_addon "${repository_target}"; then
		publish_single_addon_repository "${repository_target}" "${repository_name}" "${repository}@${repository_ref}"
	else
		link_modules "${repository_target}" "${target_root}" "${repository_name}"
	fi
done

echo "odoo-fetch-addons completed"
