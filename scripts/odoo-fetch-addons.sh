#!/usr/bin/env bash
set -euo pipefail

readonly target_root="/opt/extra_addons"
readonly checkout_root="${target_root}/_checkouts"
readonly repository_source_suffix=".odoo-source"
readonly repositories_raw="${ODOO_ADDON_REPOSITORIES:-}"
repositories="$(printf '%s' "${repositories_raw}" | tr -d '\n' | sed 's/[[:space:]]*,[[:space:]]*/,/g; s/^,//; s/,$//')"
readonly repositories
readonly exact_ref_pattern='^[0-9a-f]{40}$'
readonly repository_pattern='^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'

checkout_repository() {
	local repository_full_name="$1"
	local repository_ref="$2"
	local target_directory="$3"
	local repository_url="https://github.com/${repository_full_name}.git"
	local authorization_header
	local tmp_checkout
	local resolved_ref

	tmp_checkout="$(mktemp -d /tmp/odoo-addon-checkout-XXXXXX)"

	echo "Fetching ${repository_full_name}@${repository_ref}"
	git -C "${tmp_checkout}" init --quiet
	git -C "${tmp_checkout}" remote add origin "${repository_url}"
	if [[ -n "${GITHUB_TOKEN:-}" ]]; then
		authorization_header="$(printf 'x-access-token:%s' "${GITHUB_TOKEN}" | base64 | tr -d '\n')"
		git -c "http.extraHeader=Authorization: Basic ${authorization_header}" \
			-C "${tmp_checkout}" fetch --depth 1 origin "${repository_ref}"
	else
		git -C "${tmp_checkout}" fetch --depth 1 origin "${repository_ref}"
	fi
	resolved_ref="$(git -C "${tmp_checkout}" rev-parse FETCH_HEAD)"
	if [[ "${resolved_ref}" != "${repository_ref}" ]]; then
		echo "Resolved external addon commit ${resolved_ref} does not match requested ${repository_ref}." >&2
		rm -rf "${tmp_checkout}"
		exit 1
	fi
	git -C "${tmp_checkout}" checkout --quiet --detach FETCH_HEAD
	rm -rf "${tmp_checkout}/.git"

	rm -rf "${target_directory}"
	mkdir -p "$(dirname "${target_directory}")"
	mv "${tmp_checkout}" "${target_directory}"
}

resolve_checkout_directory() {
	local repository="$1"
	local repository_name="$2"
	local repository_checksum

	# Keep verified checkouts outside the public addon namespace so downstream
	# symlinks stay stable across ref updates and only exposed addon names appear
	# under /opt/extra_addons.
	printf -v repository_checksum '%s' "$(printf '%s' "${repository}" | sha256sum | cut -d' ' -f1)"
	printf '%s' "${checkout_root}/${repository_name}-${repository_checksum}"
}

repository_root_is_addon() {
	local repository_root="$1"
	[[ -f "${repository_root}/__manifest__.py" || -f "${repository_root}/__openerp__.py" ]]
}

validate_checkout_symlinks() {
	local repository_root="$1"
	local resolved_repository_root
	local symlink_path
	local resolved_symlink_path

	resolved_repository_root="$(realpath -e "${repository_root}")"
	while IFS= read -r -d '' symlink_path; do
		if ! resolved_symlink_path="$(realpath -e "${symlink_path}")"; then
			echo "External addon checkout contains a broken symlink: ${symlink_path}" >&2
			exit 1
		fi
		if [[ "${resolved_symlink_path}" != "${resolved_repository_root}" && "${resolved_symlink_path}" != "${resolved_repository_root}/"* ]]; then
			echo "External addon checkout symlink ${symlink_path} escapes ${repository_root}." >&2
			exit 1
		fi
	done < <(find "${repository_root}" -type l -print0)
}

publish_single_addon_repository() {
	local repository_root="$1"
	local repository_name="$2"
	local public_target="${target_root}/${repository_name}"

	if [[ -L "${public_target}" && "$(readlink "${public_target}")" == "${repository_root}" ]]; then
		return
	fi
	if [[ -e "${public_target}" || -L "${public_target}" ]]; then
		echo "Addon path collision for ${repository_name} in ${target_root}; resolve duplicate repositories before building." >&2
		exit 1
	fi

	ln -s "${repository_root}" "${public_target}"
}

link_modules() {
	local repository_root="$1"
	local module_root="$2"
	local repository_name="$3"
	local scan_roots=()
	local scan_root
	local module_dir
	local module_name
	local resolved_module_dir
	local resolved_repository_root
	local link_path
	local link_target
	local single_link_path

	resolved_repository_root="$(realpath -e "${repository_root}")"

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
			resolved_module_dir="$(realpath -e "${module_dir}")"
			if [[ "${resolved_module_dir}" != "${resolved_repository_root}" && "${resolved_module_dir}" != "${resolved_repository_root}/"* ]]; then
				echo "Addon module ${module_dir} escapes verified checkout ${repository_root}." >&2
				exit 1
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

			ln -s "${resolved_module_dir}" "${link_path}"
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
declare -A repository_refs=()
declare -a repositories_to_fetch=()
for raw_repository in "${repository_entries[@]}"; do
	repository="$(printf '%s' "${raw_repository}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
	[[ -n "${repository}" ]] || continue

	repository_ref="main"
	if [[ "${repository}" == *"@"* ]]; then
		repository_ref="${repository##*@}"
		repository="${repository%@*}"
	fi

	repository_name="${repository##*/}"
	repository_owner="${repository%%/*}"
	if [[ ! "${repository}" =~ ${repository_pattern} ]]; then
		echo "External addon repository must use owner/repository syntax: ${repository}" >&2
		exit 1
	fi
	if [[ "${repository_owner}" == "." || "${repository_owner}" == ".." || "${repository_name}" == "." || "${repository_name}" == ".." ]]; then
		echo "External addon repository cannot contain path traversal: ${repository}" >&2
		exit 1
	fi
	if [[ ! "${repository_ref}" =~ ${exact_ref_pattern} ]]; then
		echo "External addon repository ${repository} must use an exact lowercase 40-character git commit." >&2
		exit 1
	fi
	if [[ -n "${repository_refs[${repository}]:-}" ]]; then
		if [[ "${repository_refs[${repository}]}" != "${repository_ref}" ]]; then
			echo "External addon repository ${repository} cannot use multiple commits in one fetch." >&2
			exit 1
		fi
		continue
	fi
	repository_refs["${repository}"]="${repository_ref}"
	repositories_to_fetch+=("${repository}@${repository_ref}")
done

for repository_entry in "${repositories_to_fetch[@]}"; do
	repository_ref="${repository_entry##*@}"
	repository="${repository_entry%@*}"
	repository_name="${repository##*/}"
	repository_target="$(resolve_checkout_directory "${repository}" "${repository_name}")"
	checkout_repository "${repository}" "${repository_ref}" "${repository_target}"
	validate_checkout_symlinks "${repository_target}"
	printf '%s\n' "${repository}@${repository_ref}" >"${repository_target}${repository_source_suffix}"
	if repository_root_is_addon "${repository_target}"; then
		publish_single_addon_repository "${repository_target}" "${repository_name}"
	else
		link_modules "${repository_target}" "${target_root}" "${repository_name}"
	fi
done

echo "odoo-fetch-addons completed"
