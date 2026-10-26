import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

ORDER_ANNOTATION = "io.github.cbusillo.build-run-number"
TARGETS = ("runtime", "devtools")


def run_command(arguments: list[str]) -> str:
    return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=120).stdout


def require_digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Invalid image digest")
    return value


def parse_order(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("Invalid publication run number")
    return int(value)


def inspect_order(reference: str, digest: str | None = None, allow_legacy: bool = False) -> int:
    try:
        raw = run_command(["docker", "buildx", "imagetools", "inspect", "--raw", reference])
    except subprocess.CalledProcessError as error:
        if allow_legacy and error.returncode == 1 and error.stderr.strip() == f"ERROR: {reference}: not found":
            return 0
        raise
    if digest is not None and f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}" != digest:
        raise ValueError(f"Registry binding changed: {reference}")
    annotations = json.loads(raw).get("annotations", {})
    if allow_legacy and ORDER_ANNOTATION not in annotations:
        return 0
    return parse_order(annotations[ORDER_ANNOTATION])


def alias_family(tag: str) -> str:
    if tag == "19.0":
        return "stable"
    if tag == "nightly":
        return "nightly"
    if re.fullmatch(r"nightly-[0-9]{8}", tag):
        return tag
    if re.fullmatch(r"build-[0-9]+-[0-9a-f]+", tag):
        return "build"
    if re.fullmatch(r"sha-[0-9a-f-]+", tag):
        return "source"
    raise ValueError(f"Unsupported publication alias: {tag}")


def read_publications(directory: Path, image_root: str) -> tuple[dict[str, str], dict[str, dict[str, list[str]]]]:
    digests: dict[str, str] = {}
    families: dict[str, dict[str, list[str]]] = {}
    for target in TARGETS:
        publication = json.loads((directory / f"{target}.json").read_text())
        digests[target] = require_digest(publication["digest"])
        aliases = publication["aliases"].splitlines()
        if not aliases or len(aliases) != len(set(aliases)):
            raise ValueError("Publication must contain unique aliases")
        for alias in aliases:
            if not alias.startswith(f"{image_root}:") or not alias.endswith(f"-{target}"):
                raise ValueError("Alias is outside its checked image target")
            tag = alias.removeprefix(f"{image_root}:").removesuffix(f"-{target}")
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", f"{tag}-{target}"):
                raise ValueError("Invalid publication tag")
            families.setdefault(alias_family(tag), {}).setdefault(target, []).append(alias)
    for family, targets in families.items():
        if set(targets) != set(TARGETS):
            raise ValueError("Incomplete alias family")
        if family != "source" and {alias.removesuffix("-runtime") for alias in targets["runtime"]} != {
            alias.removesuffix("-devtools") for alias in targets["devtools"]
        }:
            raise ValueError("Alias family differs between image targets")
    return digests, families


def promote(repository: str, directory: Path, order: int) -> dict[str, bool]:
    image_root = f"ghcr.io/{repository.lower()}"
    digests, families = read_publications(directory, image_root)
    for digest in digests.values():
        if inspect_order(f"{image_root}@{digest}", digest) != order:
            raise ValueError("Candidate does not belong to this checked workflow run")
    selected: dict[str, list[str]] = {target: [] for target in TARGETS}
    delivered: dict[str, bool] = {}
    for family, targets in families.items():
        superseded = False
        for aliases in targets.values():
            for alias in aliases:
                if inspect_order(alias, allow_legacy=True) > order:
                    superseded = True
        delivered[family] = not superseded
        if superseded:
            print(f"::notice::Skipping superseded {family} alias pair")
            continue
        for target, aliases in targets.items():
            selected[target].extend(aliases)
    for dry_run in (True, False):
        for target, aliases in selected.items():
            if not aliases:
                continue
            arguments = ["docker", "buildx", "imagetools", "create"]
            if dry_run:
                arguments.append("--dry-run")
            for alias in aliases:
                arguments.extend(["--tag", alias])
            run_command([*arguments, f"{image_root}@{digests[target]}"])
    for target, aliases in selected.items():
        for alias in aliases:
            inspect_order(alias, digests[target])
    delivered["dispatch"] = (
        delivered.get("stable", False)
        or delivered.get("nightly", False)
        or ("stable" not in families and "nightly" not in families and delivered.get("source", False))
    )
    return delivered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    order = parse_order(os.environ["GITHUB_RUN_NUMBER"])
    delivered = promote(os.environ["GITHUB_REPOSITORY"], arguments.directory, order)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for family in ("stable", "nightly", "dispatch"):
            output.write(f"{family}={str(delivered.get(family, False)).lower()}\n")


if __name__ == "__main__":
    main()
