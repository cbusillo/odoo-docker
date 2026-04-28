#!/usr/bin/env python3
"""Fail when local requirement overrides are stale for the target Python."""

from __future__ import annotations

import argparse
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def active_exact_pins(requirements_path: Path, python_version: str) -> dict[str, Version]:
    environment = {key: str(value) for key, value in default_environment().items()}
    environment["python_version"] = python_version
    environment["python_full_version"] = f"{python_version}.0"

    pins: dict[str, Version] = {}
    for line_number, raw_line in enumerate(requirements_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue

        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise SystemExit(f"{requirements_path}:{line_number}: invalid requirement: {raw_line}\n{exc}") from exc

        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue

        exact_versions = [Version(specifier.version) for specifier in requirement.specifier if specifier.operator == "=="]
        if exact_versions:
            pins[canonicalize_name(requirement.name)] = exact_versions[-1]

    return pins


def override_pins(overrides_path: Path) -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for line_number, raw_line in enumerate(overrides_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise SystemExit(f"{overrides_path}:{line_number}: invalid requirement: {raw_line}\n{exc}") from exc

        exact_versions = [Version(specifier.version) for specifier in requirement.specifier if specifier.operator == "=="]
        if len(exact_versions) != 1:
            raise SystemExit(f"{overrides_path}:{line_number}: overrides must use one exact == pin: {raw_line}")

        pins[canonicalize_name(requirement.name)] = exact_versions[0]

    return pins


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--overrides", required=True, type=Path)
    parser.add_argument("--python-version", required=True)
    args = parser.parse_args()

    upstream_pins = active_exact_pins(args.requirements, args.python_version)
    local_overrides = override_pins(args.overrides)

    failures: list[str] = []
    for name, override_version in sorted(local_overrides.items()):
        upstream_version = upstream_pins.get(name)
        if upstream_version is None:
            failures.append(f"{name}: no active upstream pin for Python {args.python_version}")
            continue
        if upstream_version >= override_version:
            failures.append(
                f"{name}: upstream pins {upstream_version}, override pins {override_version}; "
                "remove or raise the override"
            )

    if failures:
        print("Stale requirements overrides detected:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"requirements overrides are still ahead of upstream for Python {args.python_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
