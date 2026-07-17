#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit


SUPPORT_ROOT = Path("/opt/runtime")
TENANT_ROOT = Path("/opt/project")
OWNED_ADDONS_ROOT = TENANT_ROOT / "addons"
EXTERNAL_ADDONS_ROOT = Path("/opt/extra_addons")
EVIDENCE_PATH = Path("/opt/launchplane/evidence/dependency-provenance.json")
LAYOUT_MARKER = SUPPORT_ROOT / ".odoo-python-sync-layout"
SOURCE_MARKER = ".odoo-python-source.json"
EXTERNAL_SOURCE_SUFFIX = ".odoo-source"
PYTHON_EXECUTABLE = "/venv/bin/python"
VENV_ROOT = Path("/venv")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
PACKAGE_VERSION_PATTERN = re.compile(r"^(?=.*[0-9])[A-Za-z0-9][A-Za-z0-9.!+_-]*$")
EXACT_BUILD_REQUIREMENT_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[[A-Za-z0-9._,-]+\])?\s*==\s*"
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
)
VCS_REFERENCE_PATTERN = re.compile(
    r"git\+(?:https|ssh)://[^\s]+@([^#\s]+)", re.IGNORECASE
)
DIRECT_REFERENCE_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:\[[A-Za-z0-9._,-]+\])?\s*@\s*(?P<url>\S+)",
    re.IGNORECASE,
)
SSH_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
UNSAFE_DIRECT_REFERENCE_PATTERN = re.compile(
    r"(?:^|\s)(?:-e\s+|--editable\s+|file:|\.\.?/|/)",
    re.IGNORECASE,
)


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceMarker:
    repository: str
    ref: str
    lock_path: str = "uv.lock"


@dataclass(frozen=True)
class ExternalInput:
    path: Path
    project_root: Path
    source: SourceMarker
    dependency_file_path: str
    dependency_file_sha256: str
    format: str
    package_name: str = ""


def run(command: list[str], *, cwd: Path | None = None) -> None:
    rendered = " ".join(command)
    print(f"+ {rendered}")
    subprocess.run(command, cwd=cwd, check=True)


def canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip()).lower()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_file_hashes_unchanged(hashes: dict[Path, str], *, label: str) -> None:
    changed = sorted(
        str(path) for path, digest in hashes.items() if sha256_file(path) != digest
    )
    if changed:
        raise SyncError(f"{label} changed during installation: {changed}")


def load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SyncError(f"Unable to read valid TOML from {path}: {error}") from error


def validate_pair(root: Path, *, label: str) -> bool:
    pyproject_path = root / "pyproject.toml"
    lock_path = root / "uv.lock"
    has_pyproject = pyproject_path.is_file()
    has_lock = lock_path.is_file()
    if has_pyproject != has_lock:
        missing = lock_path if has_pyproject else pyproject_path
        raise SyncError(f"{label} dependency root is incomplete; missing {missing}")
    return has_pyproject


def load_source_marker(root: Path, *, label: str) -> SourceMarker:
    marker_path = root / SOURCE_MARKER
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SyncError(
            f"{label} requires valid source marker {marker_path}: {error}"
        ) from error
    if not isinstance(payload, dict) or set(payload) not in (
        {"repository", "ref"},
        {"repository", "ref", "lock_path"},
    ):
        raise SyncError(f"{label} source marker must contain repository/ref and optional lock_path")
    repository = str(payload["repository"]).strip()
    ref = str(payload["ref"]).strip()
    if not REPOSITORY_SLUG_PATTERN.fullmatch(repository):
        raise SyncError(f"{label} source repository must use owner/repository syntax")
    if any(part in {".", ".."} for part in repository.split("/")):
        raise SyncError(f"{label} source repository cannot contain path traversal")
    if not GIT_COMMIT_PATTERN.fullmatch(ref):
        raise SyncError(
            f"{label} source ref must be an exact lowercase 40-character git commit"
        )
    lock_path = str(payload.get("lock_path", "uv.lock")).strip()
    normalized_lock_path = PurePosixPath(lock_path)
    if (
        normalized_lock_path.is_absolute()
        or normalized_lock_path.name != "uv.lock"
        or any(part in {"", ".", ".."} for part in normalized_lock_path.parts)
    ):
        raise SyncError(f"{label} source lock_path must be a repository-relative uv.lock path")
    return SourceMarker(repository=repository, ref=ref, lock_path=normalized_lock_path.as_posix())


def owned_project_source(project_root: Path, *, tenant_source: SourceMarker) -> SourceMarker:
    boundary = TENANT_ROOT.resolve()
    current = project_root.resolve()
    while current != boundary:
        if boundary not in current.parents:
            raise SyncError(f"Owned addon project escapes tenant root: {project_root}")
        if (current / SOURCE_MARKER).is_file():
            return load_source_marker(current, label=f"Owned addon project {project_root}")
        current = current.parent
    return tenant_source


def validate_git_references(value: str, *, label: str) -> None:
    stripped = value.strip()
    if not stripped:
        raise SyncError(f"{label} contains a blank dependency declaration")
    if UNSAFE_DIRECT_REFERENCE_PATTERN.search(stripped):
        raise SyncError(
            f"{label} cannot use editable, local-path, or non-VCS direct references"
        )
    direct_reference = DIRECT_REFERENCE_PATTERN.match(stripped)
    if direct_reference is not None and not direct_reference.group(
        "url"
    ).lower().startswith(("git+https://", "git+ssh://")):
        raise SyncError(
            f"{label} cannot use editable, local-path, or non-VCS direct references"
        )
    for match in VCS_REFERENCE_PATTERN.finditer(stripped):
        if not GIT_COMMIT_PATTERN.fullmatch(match.group(1)):
            raise SyncError(
                f"{label} VCS dependencies must use exact lowercase git commits"
            )
        normalize_vcs_repository(match.group(0).rsplit("@", 1)[0])
    if "git+" in stripped.lower() and VCS_REFERENCE_PATTERN.search(stripped) is None:
        raise SyncError(f"{label} contains an invalid VCS dependency")


def dependency_strings(pyproject: dict[str, Any]) -> list[str]:
    values: list[str] = []
    build_system = pyproject.get("build-system")
    if isinstance(build_system, dict):
        build_requirements = build_system.get("requires", [])
        if isinstance(build_requirements, list):
            values.extend(str(value) for value in build_requirements)
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return values
    dependencies = project.get("dependencies", [])
    if isinstance(dependencies, list):
        values.extend(str(value) for value in dependencies)
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for group in optional.values():
            if isinstance(group, list):
                values.extend(str(value) for value in group)
    dependency_groups = pyproject.get("dependency-groups", {})
    if isinstance(dependency_groups, dict):
        for group in dependency_groups.values():
            if isinstance(group, list):
                values.extend(str(value) for value in group if isinstance(value, str))
    return values


def validate_build_system(pyproject: dict[str, Any], *, path: Path) -> dict[str, str]:
    build_system = pyproject.get("build-system")
    if not isinstance(build_system, dict):
        raise SyncError(f"{path} requires an explicit build-system table")
    if set(build_system) != {"requires", "build-backend"}:
        raise SyncError(
            f"{path} build-system may contain only requires and build-backend"
        )
    backend = build_system.get("build-backend")
    requirements = build_system.get("requires")
    if not isinstance(backend, str) or not backend.strip():
        raise SyncError(f"{path} build-system requires a build-backend")
    if (
        not isinstance(requirements, list)
        or not requirements
        or not all(isinstance(value, str) for value in requirements)
    ):
        raise SyncError(f"{path} build-system requires a non-empty string list")
    project = pyproject.get("project")
    if isinstance(project, dict) and project.get("dynamic"):
        raise SyncError(f"{path} cannot use dynamic project metadata")
    pinned: dict[str, str] = {}
    for requirement in requirements:
        normalized = requirement.strip()
        match = EXACT_BUILD_REQUIREMENT_PATTERN.fullmatch(normalized)
        if match is None:
            raise SyncError(
                f"{path} build requirements must use exact registry versions"
            )
        name = canonical_package_name(match.group("name"))
        version = match.group("version")
        existing = pinned.get(name)
        if existing is not None and existing != version:
            raise SyncError(
                f"{path} contains conflicting build requirements for {name}"
            )
        pinned[name] = version
    return pinned


def validate_uv_sources(
    pyproject: dict[str, Any], *, path: Path, allow_workspace: bool
) -> None:
    tool = pyproject.get("tool")
    if not isinstance(tool, dict):
        return
    uv = tool.get("uv")
    if not isinstance(uv, dict):
        return
    sources = uv.get("sources", {})
    if not isinstance(sources, dict):
        raise SyncError(f"{path} tool.uv.sources must be a table")
    for package_name, raw_source in sources.items():
        source_values = raw_source if isinstance(raw_source, list) else [raw_source]
        for source in source_values:
            if not isinstance(source, dict):
                raise SyncError(f"{path} source for {package_name} must be a table")
            if source.get("workspace") is True:
                if allow_workspace and set(source) == {"workspace"}:
                    continue
                raise SyncError(
                    f"{path} has an invalid workspace source for {package_name}"
                )
            if "path" in source:
                raise SyncError(
                    f"{path} cannot use local path source for {package_name}"
                )
            if "git" in source:
                repository = str(source.get("git", ""))
                ref = str(source.get("rev", ""))
                validate_git_references(
                    f"{package_name} @ git+{repository}@{ref}", label=str(path)
                )
                if set(source) - {
                    "git",
                    "rev",
                    "tag",
                    "branch",
                    "subdirectory",
                    "marker",
                }:
                    raise SyncError(
                        f"{path} has unsupported git source fields for {package_name}"
                    )
                if "tag" in source or "branch" in source:
                    raise SyncError(
                        f"{path} VCS source for {package_name} cannot use tag or branch"
                    )
                continue
            raise SyncError(f"{path} contains unsupported source for {package_name}")


def validate_pyproject(path: Path, *, allow_workspace_sources: bool) -> dict[str, Any]:
    payload = load_toml(path)
    for dependency in dependency_strings(payload):
        validate_git_references(dependency, label=str(path))
    validate_uv_sources(payload, path=path, allow_workspace=allow_workspace_sources)
    return payload


def validate_dependency_catalog(pyproject: dict[str, Any], *, path: Path) -> None:
    if uv_config(pyproject).get("package") is not False:
        raise SyncError(f"{path} must set tool.uv.package = false")
    if "build-system" in pyproject:
        raise SyncError(f"{path} dependency catalog cannot define build-system")
    project = pyproject.get("project")
    if isinstance(project, dict) and project.get("dynamic"):
        raise SyncError(f"{path} dependency catalog cannot use dynamic metadata")


def project_name(pyproject: dict[str, Any], *, path: Path) -> str:
    project = pyproject.get("project")
    name = str(project.get("name", "")).strip() if isinstance(project, dict) else ""
    normalized = canonical_package_name(name)
    if not normalized or not PACKAGE_NAME_PATTERN.fullmatch(normalized):
        raise SyncError(f"{path} requires a valid project.name")
    return normalized


def uv_config(pyproject: dict[str, Any]) -> dict[str, Any]:
    tool = pyproject.get("tool")
    if not isinstance(tool, dict):
        return {}
    uv = tool.get("uv")
    return uv if isinstance(uv, dict) else {}


def workspace_members(root: Path, pyproject: dict[str, Any]) -> set[Path]:
    workspace = uv_config(pyproject).get("workspace")
    if not isinstance(workspace, dict):
        return set()
    raw_members = workspace.get("members", [])
    raw_exclude = workspace.get("exclude", [])
    if not isinstance(raw_members, list) or not all(
        isinstance(value, str) for value in raw_members
    ):
        raise SyncError(f"{root / 'pyproject.toml'} workspace members must be strings")
    if not isinstance(raw_exclude, list) or not all(
        isinstance(value, str) for value in raw_exclude
    ):
        raise SyncError(
            f"{root / 'pyproject.toml'} workspace exclude values must be strings"
        )
    for pattern in [*raw_members, *raw_exclude]:
        if pattern.startswith(("/", "~")) or ".." in Path(pattern).parts:
            raise SyncError(
                f"{root / 'pyproject.toml'} contains unsafe workspace pattern"
            )
    excluded: set[Path] = set()
    for pattern in raw_exclude:
        excluded.update(path.resolve() for path in root.glob(pattern) if path.is_dir())
    members: set[Path] = set()
    for pattern in raw_members:
        for path in root.glob(pattern):
            if path.is_dir() and (path / "pyproject.toml").is_file():
                resolved = path.resolve()
                if resolved not in excluded:
                    members.add(resolved)
    return members


def owned_projects(tenant_payload: dict[str, Any]) -> list[Path]:
    if uv_config(tenant_payload).get("package") is not False:
        raise SyncError(
            f"{TENANT_ROOT / 'pyproject.toml'} must set tool.uv.package = false"
        )
    members = workspace_members(TENANT_ROOT, tenant_payload)
    projects: list[Path] = []
    owned_root = OWNED_ADDONS_ROOT.resolve()
    if OWNED_ADDONS_ROOT.is_dir():
        for pyproject_path in sorted(OWNED_ADDONS_ROOT.rglob("pyproject.toml")):
            project_root = pyproject_path.parent.resolve()
            if project_root != owned_root and owned_root not in project_root.parents:
                raise SyncError(
                    f"Owned addon project escapes the tenant addon root: {project_root}"
                )
            if project_root not in members:
                raise SyncError(
                    f"Owned addon project is not a tenant workspace member: {project_root}"
                )
            payload = validate_pyproject(pyproject_path, allow_workspace_sources=True)
            validate_build_system(payload, path=pyproject_path)
            config = uv_config(payload)
            if config.get("managed") is False:
                raise SyncError(
                    f"{pyproject_path} cannot set tool.uv.managed = false in strict layout"
                )
            if config.get("package") is not False:
                raise SyncError(f"{pyproject_path} must set tool.uv.package = false")
            project_name(payload, path=pyproject_path)
            projects.append(project_root)
    project_set = set(projects)
    if members != project_set:
        extra_members = sorted(str(path) for path in members - project_set)
        missing_members = sorted(str(path) for path in project_set - members)
        raise SyncError(
            "Tenant workspace members must exactly match owned addon projects; "
            f"extra={extra_members}, missing={missing_members}"
        )
    for requirements_path in sorted(OWNED_ADDONS_ROOT.rglob("requirements*.txt")):
        raise SyncError(
            f"Owned addon requirements must move into tenant workspace metadata: {requirements_path}"
        )
    return projects


def parse_external_source_marker(path: Path) -> SourceMarker:
    raw_value = path.read_text(encoding="utf-8").strip()
    repository, separator, ref = raw_value.rpartition("@")
    if not separator or not REPOSITORY_SLUG_PATTERN.fullmatch(repository):
        raise SyncError(f"Invalid external addon source marker: {path}")
    if any(part in {".", ".."} for part in repository.split("/")):
        raise SyncError(f"External addon source marker contains path traversal: {path}")
    if not GIT_COMMIT_PATTERN.fullmatch(ref):
        raise SyncError(
            f"External addon source marker must use an exact git commit: {path}"
        )
    return SourceMarker(repository=repository, ref=ref)


def find_external_project_root(path: Path) -> tuple[Path, SourceMarker]:
    current = path.resolve()
    boundary = EXTERNAL_ADDONS_ROOT.resolve()
    checkout_boundary = boundary / "_checkouts"
    while current != boundary and boundary in current.parents:
        if current.parent in {boundary, checkout_boundary}:
            marker_path = current.with_name(f"{current.name}{EXTERNAL_SOURCE_SUFFIX}")
            if marker_path.is_file():
                return current, parse_external_source_marker(marker_path)
        current = current.parent
    raise SyncError(f"External compatibility input lacks exact source marker: {path}")


def requirement_lines(path: Path) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        line = pending + line
        pending = ""
        lines.append(line)
    if pending:
        raise SyncError(f"Incomplete requirement continuation in {path}")
    return lines


def validate_requirements(path: Path, *, generated: bool = False) -> None:
    for line in requirement_lines(path):
        if line.startswith(
            ("-r", "--requirement", "-c", "--constraint", "-e", "--editable")
        ):
            raise SyncError(
                f"{path} cannot contain nested or editable requirement directives"
            )
        if line.startswith(
            ("--index-url", "--extra-index-url", "--find-links", "--trusted-host")
        ):
            if generated:
                continue
            raise SyncError(f"{path} cannot contain package index directives")
        if line.startswith("--hash="):
            if generated:
                continue
            raise SyncError(f"{path} contains an orphaned hash directive")
        if line.startswith("--"):
            raise SyncError(f"{path} contains an unsupported requirement directive")
        requirement = re.sub(r"(?:\s+--hash=[^\s]+)+$", "", line).strip()
        lowered = requirement.lower()
        if lowered.startswith(
            ("http://", "https://", "file:", "git://", "ssh://", "/", "./", "../", "~")
        ):
            raise SyncError(f"{path} cannot contain a bare path or archive URL")
        first_token = requirement.split(maxsplit=1)[0]
        if "@git+" not in lowered and (
            "/" in first_token
            or "\\" in first_token
            or first_token.lower().endswith(
                (".whl", ".zip", ".tar", ".tar.gz", ".tar.bz2", ".tgz")
            )
        ):
            raise SyncError(f"{path} cannot contain a bare path or archive URL")
        validate_git_references(requirement, label=str(path))


def discover_external_inputs(sync_mode: str) -> tuple[list[ExternalInput], list[str]]:
    inputs: list[ExternalInput] = []
    install_args: list[str] = []
    seen_files: set[Path] = set()
    seen_projects: set[Path] = set()
    if not EXTERNAL_ADDONS_ROOT.is_dir():
        return inputs, install_args
    for public_path in sorted(EXTERNAL_ADDONS_ROOT.iterdir()):
        if (
            public_path.name == "_checkouts"
            or public_path.name.startswith(".")
            or not public_path.is_dir()
        ):
            continue
        candidates = [public_path / "requirements.txt", public_path / "pyproject.toml"]
        if sync_mode == "dev":
            candidates.append(public_path / "requirements-dev.txt")
        for candidate in candidates:
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            source_root, source = find_external_project_root(resolved.parent)
            relative_path = resolved.relative_to(source_root).as_posix()
            if candidate.name == "pyproject.toml":
                payload = validate_pyproject(resolved, allow_workspace_sources=False)
                package_name = project_name(payload, path=resolved)
                validate_build_system(payload, path=resolved)
                inputs.append(
                    ExternalInput(
                        path=resolved,
                        project_root=resolved.parent,
                        source=source,
                        dependency_file_path=relative_path,
                        dependency_file_sha256=sha256_file(resolved),
                        format="pyproject_toml",
                        package_name=package_name,
                    )
                )
                if resolved.parent not in seen_projects:
                    seen_projects.add(resolved.parent)
                    install_spec = str(resolved.parent)
                    optional = payload.get("project", {}).get(
                        "optional-dependencies", {}
                    )
                    if (
                        sync_mode == "dev"
                        and isinstance(optional, dict)
                        and "dev" in optional
                    ):
                        install_spec += "[dev]"
                    install_args.append(install_spec)
                continue
            validate_requirements(resolved)
            inputs.append(
                ExternalInput(
                    path=resolved,
                    project_root=source_root,
                    source=source,
                    dependency_file_path=relative_path,
                    dependency_file_sha256=sha256_file(resolved),
                    format="requirements_txt",
                )
            )
            install_args.extend(["-r", str(resolved)])
    inputs.sort(
        key=lambda item: (
            item.source.repository,
            item.source.ref,
            item.dependency_file_path,
        )
    )
    return inputs, install_args


def ensure_external_inputs_unchanged(inputs: list[ExternalInput]) -> None:
    changed = sorted(
        item.dependency_file_path
        for item in inputs
        if sha256_file(item.path) != item.dependency_file_sha256
    )
    if changed:
        raise SyncError(
            f"External dependency inputs changed during installation: {changed}"
        )


def has_dev_extra(root: Path, *, include_workspace: bool) -> bool:
    paths = [root / "pyproject.toml"]
    if include_workspace:
        payload = load_toml(root / "pyproject.toml")
        paths.extend(
            path / "pyproject.toml" for path in workspace_members(root, payload)
        )
    for path in paths:
        payload = load_toml(path)
        project = payload.get("project")
        optional = (
            project.get("optional-dependencies", {})
            if isinstance(project, dict)
            else {}
        )
        if isinstance(optional, dict) and "dev" in optional:
            return True
    return False


def export_lock(
    root: Path,
    *,
    scope: str,
    all_packages: bool,
    sync_mode: str,
    temporary_root: Path,
) -> Path:
    run(["uv", "lock", "--quiet", "--project", str(root), "--check"])
    output_path = temporary_root / f"{scope}.txt"
    command = [
        "uv",
        "export",
        "--quiet",
        "--project",
        str(root),
        "--frozen",
        "--format",
        "requirements.txt",
        "--no-emit-workspace",
        "--no-default-groups",
        "--output-file",
        str(output_path),
    ]
    if all_packages:
        command.append("--all-packages")
    if sync_mode == "dev" and has_dev_extra(root, include_workspace=all_packages):
        command.extend(["--extra", "dev"])
    run(command)
    validate_requirements(output_path, generated=True)
    return output_path


def exported_package_names(paths: list[Path]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        for line in requirement_lines(path):
            if line.startswith("--"):
                continue
            requirement = re.sub(r"(?:\s+--hash=[^\s]+)+$", "", line).strip()
            match = re.match(
                r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)",
                requirement,
            )
            if match is None:
                raise SyncError(
                    f"Unable to identify exported package in {path}: {line}"
                )
            names.add(canonical_package_name(match.group("name")))
    return names


def installed_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = canonical_package_name(distribution.metadata.get("Name", ""))
        version = distribution.version.strip()
        if not name:
            continue
        existing = versions.get(name)
        if existing is not None and existing != version:
            raise SyncError(
                f"Installed environment contains duplicate package versions for {name}"
            )
        versions[name] = version
    return dict(sorted(versions.items()))


def write_constraints(path: Path, versions: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{name}=={version}\n" for name, version in versions.items()),
        encoding="utf-8",
    )


def target_platform() -> str:
    machine = platform.machine().lower()
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "ppc64le": "ppc64le",
    }.get(machine)
    if architecture is None:
        raise SyncError(f"Unsupported Python evidence architecture: {machine}")
    detected = f"linux/{architecture}"
    configured = os.environ.get("TARGETPLATFORM", "").strip().lower()
    if not configured:
        return detected
    if not re.fullmatch(r"[a-z0-9._-]+/[a-z0-9._-]+(?:/[a-z0-9._-]+)?", configured):
        raise SyncError("TARGETPLATFORM contains an invalid OCI platform")
    configured_parts = configured.split("/")
    if configured_parts[:2] != detected.split("/"):
        raise SyncError(
            f"TARGETPLATFORM {configured} does not match runtime platform {detected}"
        )
    return configured


def normalize_vcs_repository(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("git+"):
        normalized = normalized[4:]
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
        raise SyncError("VCS package evidence requires an https or ssh repository")
    if parsed.password is not None or parsed.query or parsed.fragment:
        raise SyncError(
            "VCS package evidence cannot contain credentials, queries, or fragments"
        )
    username = parsed.username
    if username is not None and parsed.scheme != "ssh":
        raise SyncError("HTTPS VCS package evidence cannot contain userinfo")
    if username is not None and not SSH_USERNAME_PATTERN.fullmatch(username):
        raise SyncError("SSH VCS package evidence contains an invalid username")
    authority = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise SyncError(
            "VCS package evidence contains an invalid repository port"
        ) from error
    if port is not None:
        authority += f":{port}"
    if username is not None:
        authority = f"{username}@{authority}"
    path = parsed.path.rstrip("/")
    if not path or any(part in {"", ".", ".."} for part in path.strip("/").split("/")):
        raise SyncError("VCS package evidence contains an invalid repository path")
    return urlunsplit((parsed.scheme, authority, path, "", ""))


def marker_for_local_path(path: Path, roots: dict[Path, SourceMarker]) -> SourceMarker:
    resolved = path.resolve()
    candidates = sorted(roots, key=lambda root: len(root.parts), reverse=True)
    for root in candidates:
        if resolved == root or root in resolved.parents:
            return roots[root]
    raise SyncError(f"Installed package contains an unowned local direct URL: {path}")


def package_source(
    distribution: metadata.Distribution, roots: dict[Path, SourceMarker]
) -> dict[str, str]:
    raw_direct_url = distribution.read_text("direct_url.json")
    if not raw_direct_url:
        return {"kind": "registry", "repository": "", "commit": ""}
    try:
        direct_url = json.loads(raw_direct_url)
    except json.JSONDecodeError as error:
        raise SyncError(
            f"Invalid direct_url.json for {distribution.metadata.get('Name', '')}"
        ) from error
    vcs_info = direct_url.get("vcs_info")
    if isinstance(vcs_info, dict):
        commit = str(vcs_info.get("commit_id", "")).strip().lower()
        if vcs_info.get("vcs") != "git" or not GIT_COMMIT_PATTERN.fullmatch(commit):
            raise SyncError("VCS package evidence requires an exact git commit")
        return {
            "kind": "vcs",
            "repository": normalize_vcs_repository(str(direct_url.get("url", ""))),
            "commit": commit,
        }
    if isinstance(direct_url.get("dir_info"), dict):
        parsed = urlsplit(str(direct_url.get("url", "")))
        if parsed.scheme != "file":
            raise SyncError("Local package evidence requires a file URL")
        source = marker_for_local_path(Path(unquote(parsed.path)), roots)
        return {"kind": "vcs", "repository": source.repository, "commit": source.ref}
    raise SyncError(
        f"Installed package {distribution.metadata.get('Name', '')} uses unsupported direct URL evidence"
    )


def distribution_content_sha256(distribution: metadata.Distribution) -> str:
    contents: list[str] = []
    for file_name in ("METADATA", "RECORD"):
        value = distribution.read_text(file_name)
        if value is not None:
            contents.append(f"{file_name}\0{value}")
    if not contents:
        raise SyncError(
            f"Installed package {distribution.metadata.get('Name', '')} lacks metadata evidence"
        )
    return hashlib.sha256("\0".join(contents).encode("utf-8")).hexdigest()


def installed_package_identities(
    roots: dict[Path, SourceMarker],
) -> dict[str, dict[str, Any]]:
    identities: dict[str, dict[str, Any]] = {}
    for distribution in metadata.distributions():
        name = canonical_package_name(distribution.metadata.get("Name", ""))
        version = distribution.version.strip()
        if not name:
            continue
        if name in identities:
            raise SyncError(f"Installed environment contains duplicate package: {name}")
        identities[name] = {
            "version": version,
            "source": package_source(distribution, roots),
            "content_sha256": distribution_content_sha256(distribution),
        }
    return dict(sorted(identities.items()))


def ensure_packages_preserved(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    *,
    label: str,
) -> None:
    changed = sorted(
        name for name, identity in before.items() if after.get(name) != identity
    )
    if changed:
        raise SyncError(f"{label} changed existing package identities: {changed}")


def installed_file_owners() -> dict[Path, set[str]]:
    owners: dict[Path, set[str]] = {}
    venv_root = VENV_ROOT.resolve()
    for distribution in metadata.distributions():
        name = canonical_package_name(distribution.metadata.get("Name", ""))
        files = distribution.files
        if not name or files is None:
            continue
        for package_path in files:
            installed_path = Path(distribution.locate_file(package_path)).resolve()
            if installed_path != venv_root and venv_root not in installed_path.parents:
                raise SyncError(
                    f"Installed package {name} records a file outside /venv: {package_path}"
                )
            owners.setdefault(installed_path, set()).add(name)
    return owners


def ensure_no_file_owner_collisions() -> None:
    collisions = {
        str(path): sorted(owners)
        for path, owners in installed_file_owners().items()
        if len(owners) > 1
    }
    if collisions:
        raise SyncError(
            f"Installed distributions contain overlapping file ownership: {collisions}"
        )


def validate_installed_distribution_files(names: set[str]) -> None:
    for distribution in metadata.distributions():
        name = canonical_package_name(distribution.metadata.get("Name", ""))
        if name not in names:
            continue
        files = distribution.files
        if files is None:
            raise SyncError(f"Installed package {name} lacks RECORD file evidence")
        for package_path in files:
            installed_path = Path(distribution.locate_file(package_path))
            if not installed_path.is_file():
                raise SyncError(
                    f"Installed package {name} is missing recorded file: {package_path}"
                )
            if (
                package_path.size is not None
                and installed_path.stat().st_size != package_path.size
            ):
                raise SyncError(
                    f"Installed package {name} has modified file size: {package_path}"
                )
            if package_path.hash is None:
                continue
            try:
                digest = hashlib.new(package_path.hash.mode)
            except ValueError as error:
                raise SyncError(
                    f"Installed package {name} uses unsupported RECORD hash: {package_path.hash.mode}"
                ) from error
            with installed_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode()
            if actual != package_path.hash.value:
                raise SyncError(
                    f"Installed package {name} has modified file content: {package_path}"
                )


def ensure_build_requirements(
    project_roots: list[Path], locked_package_names: set[str]
) -> None:
    versions = installed_versions()
    for project_root in sorted(set(project_roots)):
        pyproject_path = project_root / "pyproject.toml"
        requirements = validate_build_system(
            load_toml(pyproject_path), path=pyproject_path
        )
        mismatched = {
            name: (version, versions.get(name))
            for name, version in requirements.items()
            if name not in locked_package_names or versions.get(name) != version
        }
        if mismatched:
            raise SyncError(
                f"{pyproject_path} build requirements are absent from locked layers: {mismatched}"
            )


def python_environment(roots: dict[Path, SourceMarker]) -> dict[str, Any]:
    packages: list[dict[str, Any]] = []
    names: set[str] = set()
    for distribution in metadata.distributions():
        name = canonical_package_name(distribution.metadata.get("Name", ""))
        version = distribution.version.strip()
        if not name:
            continue
        if not PACKAGE_NAME_PATTERN.fullmatch(name):
            raise SyncError(f"Installed package has invalid canonical name: {name}")
        if not PACKAGE_VERSION_PATTERN.fullmatch(version):
            raise SyncError(
                f"Installed package {name} has invalid exact version: {version}"
            )
        if name in names:
            raise SyncError(f"Installed environment contains duplicate package: {name}")
        names.add(name)
        packages.append(
            {
                "name": name,
                "version": version,
                "source": package_source(distribution, roots),
            }
        )
    packages.sort(key=lambda package: package["name"])
    canonical_json = json.dumps(
        packages,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "python_version": platform.python_version().lower(),
        "packages": packages,
        "package_count": len(packages),
        "packages_sha256": hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def strict_sync(sync_mode: str) -> None:
    try:
        layout_version = LAYOUT_MARKER.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise SyncError(
            f"Strict layout marker is unreadable: {LAYOUT_MARKER}"
        ) from error
    if layout_version != "2":
        raise SyncError(
            f"Unsupported Odoo Python sync layout version: {layout_version}"
        )
    if os.environ.get("ODOO_PYTHON_SYNC_SKIP_ADDONS", "").strip():
        raise SyncError(
            "ODOO_PYTHON_SYNC_SKIP_ADDONS is unsupported in strict layout; "
            "remove skipped projects from the tenant workspace"
        )
    evidence_platform = target_platform()

    has_support = validate_pair(SUPPORT_ROOT, label="Support/runtime")
    has_tenant = validate_pair(TENANT_ROOT, label="Tenant")
    if not has_support and not has_tenant:
        raise SyncError("Strict layout requires at least one dependency root")

    lock_sources: dict[Path, SourceMarker] = {}
    if has_support:
        support_payload = validate_pyproject(
            SUPPORT_ROOT / "pyproject.toml", allow_workspace_sources=False
        )
        validate_dependency_catalog(
            support_payload, path=SUPPORT_ROOT / "pyproject.toml"
        )
        lock_sources[SUPPORT_ROOT.resolve()] = load_source_marker(
            SUPPORT_ROOT, label="Support/runtime"
        )
    owned: list[Path] = []
    if has_tenant:
        tenant_payload = validate_pyproject(
            TENANT_ROOT / "pyproject.toml",
            allow_workspace_sources=True,
        )
        validate_dependency_catalog(tenant_payload, path=TENANT_ROOT / "pyproject.toml")
        lock_sources[TENANT_ROOT.resolve()] = load_source_marker(
            TENANT_ROOT, label="Tenant"
        )
        owned = owned_projects(tenant_payload)
    elif OWNED_ADDONS_ROOT.is_dir():
        dependency_inputs = sorted(
            set(OWNED_ADDONS_ROOT.rglob("pyproject.toml"))
            | set(OWNED_ADDONS_ROOT.rglob("requirements*.txt"))
        )
        if dependency_inputs:
            raise SyncError(
                "Owned addon dependency metadata requires a tenant dependency root: "
                f"{dependency_inputs}"
            )

    lock_hashes: dict[Path, str] = {}
    if has_support:
        lock_hashes[SUPPORT_ROOT / "uv.lock"] = sha256_file(SUPPORT_ROOT / "uv.lock")
    if has_tenant:
        lock_hashes[TENANT_ROOT / "uv.lock"] = sha256_file(TENANT_ROOT / "uv.lock")

    external_inputs, external_install_args = discover_external_inputs(sync_mode)
    external_projects = sorted(
        {
            item.project_root.resolve()
            for item in external_inputs
            if item.format == "pyproject_toml"
        }
    )
    external_package_roots = {
        item.project_root.resolve(): item.source
        for item in external_inputs
        if item.format == "pyproject_toml"
    }
    owned_package_roots = (
        {
            path.resolve(): owned_project_source(
                path,
                tenant_source=lock_sources[TENANT_ROOT.resolve()],
            )
            for path in owned
        }
        if has_tenant
        else {}
    )
    package_roots = {**external_package_roots, **owned_package_roots}
    base_identities = installed_package_identities({})
    validate_installed_distribution_files(set(base_identities))
    ensure_no_file_owner_collisions()
    base_versions = {
        name: identity["version"] for name, identity in base_identities.items()
    }

    with tempfile.TemporaryDirectory(prefix="odoo-python-sync-") as temporary_name:
        temporary_root = Path(temporary_name)
        exports: list[Path] = []
        if has_support:
            exports.append(
                export_lock(
                    SUPPORT_ROOT,
                    scope="support-runtime",
                    all_packages=False,
                    sync_mode=sync_mode,
                    temporary_root=temporary_root,
                )
            )
        if has_tenant:
            exports.append(
                export_lock(
                    TENANT_ROOT,
                    scope="tenant",
                    all_packages=True,
                    sync_mode=sync_mode,
                    temporary_root=temporary_root,
                )
            )
        locked_package_names = exported_package_names(exports)

        locked_install = [
            "uv",
            "pip",
            "install",
            "--python",
            PYTHON_EXECUTABLE,
            "--no-deps",
        ]
        base_constraints_path = temporary_root / "base-constraints.txt"
        write_constraints(base_constraints_path, base_versions)
        locked_install.extend(["--constraint", str(base_constraints_path)])
        for export_path in exports:
            locked_install.extend(["-r", str(export_path)])
        run(locked_install)
        locked_identities = installed_package_identities({})
        ensure_packages_preserved(
            base_identities,
            locked_identities,
            label="Locked dependency installation",
        )
        validate_installed_distribution_files(set(locked_identities))
        ensure_no_file_owner_collisions()

        ensure_build_requirements([*external_projects, *owned], locked_package_names)

        before_external = locked_identities
        if external_install_args:
            external_names = [
                item.package_name for item in external_inputs if item.package_name
            ]
            duplicate_external_names = sorted(
                {name for name in external_names if external_names.count(name) > 1}
            )
            if duplicate_external_names:
                raise SyncError(
                    "External addon projects contain duplicate package names: "
                    f"{duplicate_external_names}"
                )
            collisions = sorted(set(before_external) & set(external_names))
            if collisions:
                raise SyncError(
                    f"External addon package names collide with locked packages: {collisions}"
                )
            constraints_path = temporary_root / "pre-external-constraints.txt"
            write_constraints(
                constraints_path,
                {
                    name: identity["version"]
                    for name, identity in before_external.items()
                },
            )
            run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    PYTHON_EXECUTABLE,
                    "--constraint",
                    str(constraints_path),
                    "--no-build-isolation",
                    *external_install_args,
                ]
            )
            after_external = installed_package_identities(external_package_roots)
            ensure_packages_preserved(
                before_external,
                after_external,
                label="External compatibility installation",
            )
            validate_installed_distribution_files(set(after_external))
            ensure_no_file_owner_collisions()
            ensure_external_inputs_unchanged(external_inputs)
        else:
            after_external = before_external

        if owned:
            collisions = sorted(
                set(after_external)
                & {
                    project_name(
                        load_toml(path / "pyproject.toml"), path=path / "pyproject.toml"
                    )
                    for path in owned
                }
            )
            if collisions:
                raise SyncError(
                    f"Owned addon package names collide with installed packages: {collisions}"
                )
            run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    PYTHON_EXECUTABLE,
                    "--no-deps",
                    "--no-build-isolation",
                    *(str(path) for path in owned),
                ]
            )
            after_owned = installed_package_identities(package_roots)
            ensure_packages_preserved(
                after_external,
                after_owned,
                label="Owned addon metadata installation",
            )
            validate_installed_distribution_files(set(after_owned))
            ensure_no_file_owner_collisions()
            ensure_external_inputs_unchanged(external_inputs)

    run(["uv", "pip", "check", "--python", PYTHON_EXECUTABLE])
    final_identities = installed_package_identities(package_roots)
    validate_installed_distribution_files(set(final_identities))
    ensure_no_file_owner_collisions()
    ensure_external_inputs_unchanged(external_inputs)
    ensure_file_hashes_unchanged(lock_hashes, label="Dependency lock")

    uv_locks: list[dict[str, str]] = []
    if has_support:
        source = lock_sources[SUPPORT_ROOT.resolve()]
        uv_locks.append(
            {
                "scope": "support_runtime",
                "source_repository": source.repository,
                "source_ref": source.ref,
                "path": source.lock_path,
                "sha256": lock_hashes[SUPPORT_ROOT / "uv.lock"],
            }
        )
    if has_tenant:
        source = lock_sources[TENANT_ROOT.resolve()]
        uv_locks.append(
            {
                "scope": "tenant",
                "source_repository": source.repository,
                "source_ref": source.ref,
                "path": source.lock_path,
                "sha256": lock_hashes[TENANT_ROOT / "uv.lock"],
            }
        )
    external_evidence = [
        {
            "source_repository": item.source.repository,
            "source_ref": item.source.ref,
            "dependency_file_path": item.dependency_file_path,
            "dependency_file_sha256": item.dependency_file_sha256,
            "format": item.format,
            "resolution_posture": "exact_source_unlocked",
        }
        for item in external_inputs
    ]
    payload = {
        "schema_version": 1,
        "layout": "two_lock",
        "publishable": has_support and has_tenant,
        "target_platform": evidence_platform,
        "uv_locks": uv_locks,
        "python_environment": python_environment(package_roots),
        "external_compatibility_inputs": external_evidence,
    }
    write_json_atomic(EVIDENCE_PATH, payload)
    print(f"Wrote Odoo Python dependency evidence to {EVIDENCE_PATH}")


def legacy_evidence() -> None:
    locks: list[dict[str, str]] = []
    lock_path = TENANT_ROOT / "uv.lock"
    if lock_path.is_file():
        locks.append(
            {
                "scope": "legacy",
                "container_path": str(lock_path),
                "sha256": sha256_file(lock_path),
            }
        )
    write_json_atomic(
        EVIDENCE_PATH,
        {
            "schema_version": 1,
            "layout": "legacy_single_root",
            "publishable": False,
            "target_platform": target_platform(),
            "locks": locks,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("strict-sync", "legacy-evidence"))
    parser.add_argument("--mode", choices=("prod", "dev"), default="prod")
    arguments = parser.parse_args()
    try:
        if arguments.command == "strict-sync":
            strict_sync(arguments.mode)
        else:
            legacy_evidence()
    except (SyncError, OSError, UnicodeError, subprocess.CalledProcessError) as error:
        print(f"Odoo Python sync failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
