import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

ORDER_ANNOTATION = "io.github.cbusillo.build-order"
TARGETS = ("runtime", "devtools")


def run_command(arguments: list[str]) -> str:
    return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=120).stdout


def require_digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("Invalid image digest")
    return value


def parse_order(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*\.[1-9][0-9]*", value):
        raise ValueError("Invalid publication order")
    number, attempt = value.split(".")
    return int(number), int(attempt)


def inspect_order(reference: str, digest: str, allow_legacy: bool = False) -> tuple[int, int]:
    raw = run_command(["docker", "buildx", "imagetools", "inspect", "--raw", reference])
    if f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}" != digest:
        raise ValueError(f"Registry binding changed: {reference}")
    annotations = json.loads(raw).get("annotations", {})
    if allow_legacy and ORDER_ANNOTATION not in annotations:
        return 0, 0
    return parse_order(annotations[ORDER_ANNOTATION])


def read_inventory(repository: str) -> dict[str, str]:
    owner, package = repository.split("/")
    owner_type = json.loads(run_command(["gh", "api", f"repos/{repository}"]))["owner"]["type"]
    if owner_type not in ("User", "Organization"):
        raise ValueError("Unknown package owner type")
    namespace = "users" if owner_type == "User" else "orgs"
    pages = json.loads(
        run_command(
            ["gh", "api", f"{namespace}/{owner}/packages/container/{package}/versions?per_page=100", "--paginate", "--slurp"]
        )
    )
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError("Invalid package inventory")
    aliases: dict[str, str] = {}
    for page in pages:
        for version in page:
            digest = require_digest(version["name"])
            tags = version["metadata"]["container"]["tags"]
            if not isinstance(tags, list):
                raise ValueError("Invalid package tags")
            for tag in tags:
                if not isinstance(tag, str) or tag in aliases:
                    raise ValueError("Ambiguous package alias")
                aliases[tag] = digest
    return aliases


def alias_family(tag: str) -> str:
    if tag == "19.0":
        return "stable"
    if tag == "nightly" or re.fullmatch(r"nightly-[0-9]{8}", tag):
        return "nightly"
    if re.fullmatch(r"(?:sha|build)-[0-9a-f-]+", tag):
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


def promote(repository: str, directory: Path, order: tuple[int, int]) -> dict[str, bool]:
    image_root = f"ghcr.io/{repository.lower()}"
    digests, families = read_publications(directory, image_root)
    for digest in digests.values():
        if inspect_order(f"{image_root}@{digest}", digest) != order:
            raise ValueError("Candidate does not belong to this checked workflow attempt")
    inventory = read_inventory(repository.lower())
    selected: dict[str, list[str]] = {target: [] for target in TARGETS}
    delivered: dict[str, bool] = {}
    for family, targets in families.items():
        superseded = False
        for aliases in targets.values():
            for alias in aliases:
                tag = alias.removeprefix(f"{image_root}:")
                if tag in inventory and inspect_order(alias, inventory[tag], allow_legacy=True) > order:
                    superseded = True
        delivered[family] = not superseded
        if superseded:
            print(f"Skipping superseded {family} alias pair")
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
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    order = parse_order(f"{os.environ['GITHUB_RUN_NUMBER']}.{os.environ['GITHUB_RUN_ATTEMPT']}")
    delivered = promote(os.environ["GITHUB_REPOSITORY"], arguments.directory, order)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for family in ("stable", "nightly", "dispatch"):
            output.write(f"{family}={str(delivered.get(family, False)).lower()}\n")


if __name__ == "__main__":
    main()
