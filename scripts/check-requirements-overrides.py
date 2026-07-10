#!/usr/bin/env python3
"""Fail when local requirement overrides are stale for the target Python."""

from __future__ import annotations

import argparse
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


UNPINNED_UPSTREAM_OVERRIDE_ALLOWLIST = frozenset({"lxml-html-clean"})


def active_requirements(
    requirements_path: Path, python_version: str
) -> dict[str, Requirement]:
    environment = {key: str(value) for key, value in default_environment().items()}
    environment["python_version"] = python_version
    environment["python_full_version"] = f"{python_version}.0"

    requirements: dict[str, Requirement] = {}
    for line_number, raw_line in enumerate(
        requirements_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue

        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise SystemExit(
                f"{requirements_path}:{line_number}: invalid requirement: {raw_line}\n{exc}"
            ) from exc

        if requirement.marker is not None and not requirement.marker.evaluate(
            environment
        ):
            continue

        requirements[canonicalize_name(requirement.name)] = requirement

    return requirements


def override_pins(overrides_path: Path) -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for line_number, raw_line in enumerate(
        overrides_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise SystemExit(
                f"{overrides_path}:{line_number}: invalid requirement: {raw_line}\n{exc}"
            ) from exc

        exact_versions = [
            Version(specifier.version)
            for specifier in requirement.specifier
            if specifier.operator == "=="
        ]
        if len(exact_versions) != 1:
            raise SystemExit(
                f"{overrides_path}:{line_number}: overrides must use one exact == pin: {raw_line}"
            )

        pins[canonicalize_name(requirement.name)] = exact_versions[0]

    return pins


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--overrides", required=True, type=Path)
    parser.add_argument("--python-version", required=True)
    args = parser.parse_args()

    upstream_requirements = active_requirements(args.requirements, args.python_version)
    local_overrides = override_pins(args.overrides)

    failures: list[str] = []
    for name, override_version in sorted(local_overrides.items()):
        upstream_requirement = upstream_requirements.get(name)
        if upstream_requirement is None:
            failures.append(
                f"{name}: no active upstream requirement for Python {args.python_version}"
            )
            continue
        specifiers = list(upstream_requirement.specifier)
        if not specifiers:
            if name not in UNPINNED_UPSTREAM_OVERRIDE_ALLOWLIST:
                failures.append(
                    f"{name}: upstream requirement is unpinned; exact override requires review"
                )
            continue
        exact_versions = [
            Version(specifier.version)
            for specifier in specifiers
            if specifier.operator == "==" and "*" not in specifier.version
        ]
        if len(specifiers) == 1 and len(exact_versions) == 1:
            upstream_version = exact_versions[0]
            if upstream_version >= override_version:
                failures.append(
                    f"{name}: upstream pins {upstream_version}, override pins {override_version}; "
                    "remove or raise the override"
                )
            continue
        failures.append(
            f"{name}: upstream uses non-exact constraint "
            f"{upstream_requirement.specifier}; exact override requires review"
        )

    if failures:
        print("Stale requirements overrides detected:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"requirements overrides remain valid for Python {args.python_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
