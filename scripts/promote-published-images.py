import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ORDER_ANNOTATION = "io.github.cbusillo.build-run-number"
SERIES_ANNOTATION = "io.github.cbusillo.odoo-series"
SOURCE_ANNOTATION = "io.github.cbusillo.odoo-source-revision"
TARGETS = ("runtime", "devtools")


@dataclass(frozen=True)
class ImageVariant:
    series: str
    alias_prefix: str
    enterprise_followup: bool


@dataclass(frozen=True)
class Promotion:
    delivered: dict[str, bool]
    digests: dict[str, str]


def read_variants(path: Path) -> list[ImageVariant]:
    records = json.loads(path.read_text())
    if not isinstance(records, list) or not records:
        raise ValueError("Image variants must be a nonempty list")
    variants = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"series", "alias_prefix", "enterprise_followup"}:
            raise ValueError("Invalid image variant")
        series = record["series"]
        if not isinstance(series, str) or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", series):
            raise ValueError("Invalid Odoo series")
        if record["alias_prefix"] not in ("", f"{series}-") or type(record["enterprise_followup"]) is not bool:
            raise ValueError("Invalid image variant namespace")
        if record["enterprise_followup"] and record["alias_prefix"]:
            raise ValueError("Enterprise follow-up requires the legacy namespace")
        variants.append(ImageVariant(**record))
    if len({variant.series for variant in variants}) != len(variants) or sum(not variant.alias_prefix for variant in variants) != 1:
        raise ValueError("Image variants require unique series and one legacy namespace")
    return variants


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


def inspect_order(
    reference: str,
    variant: ImageVariant,
    digest: str | None = None,
    source_revision: str | None = None,
    allow_legacy: bool = False,
) -> int:
    try:
        raw = run_command(["docker", "buildx", "imagetools", "inspect", "--raw", reference])
    except subprocess.CalledProcessError as error:
        if allow_legacy and error.returncode == 1 and error.stderr.strip() == f"ERROR: {reference}: not found":
            return 0
        raise
    if digest is not None and f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}" != digest:
        raise ValueError(f"Registry binding changed: {reference}")
    annotations = json.loads(raw).get("annotations", {})
    if annotations.get(SERIES_ANNOTATION) != variant.series:
        if not (allow_legacy and not variant.alias_prefix and SERIES_ANNOTATION not in annotations):
            raise ValueError("Registry image belongs to another Odoo series")
    if source_revision is not None and annotations.get(SOURCE_ANNOTATION) != source_revision:
        raise ValueError("Registry image belongs to another Odoo source revision")
    if allow_legacy and not variant.alias_prefix and ORDER_ANNOTATION not in annotations:
        return 0
    return parse_order(annotations.get(ORDER_ANNOTATION))


def alias_family(tag: str, variant: ImageVariant) -> str:
    if tag == variant.series:
        return "stable"
    if not tag.startswith(variant.alias_prefix):
        raise ValueError("Publication alias belongs to another Odoo series")
    tag = tag.removeprefix(variant.alias_prefix)
    if tag == "nightly":
        return "nightly"
    if re.fullmatch(r"nightly-[0-9]{8}", tag):
        return tag
    if re.fullmatch(r"build-[0-9]+-[0-9a-f]+", tag):
        return "build"
    if re.fullmatch(r"sha-[0-9a-f-]+", tag):
        return "source"
    raise ValueError(f"Unsupported publication alias: {tag}")


def read_publications(
    directory: Path, image_root: str, variant: ImageVariant, source_revision: str
) -> tuple[dict[str, str], dict[str, dict[str, list[str]]]]:
    digests: dict[str, str] = {}
    families: dict[str, dict[str, list[str]]] = {}
    for target in TARGETS:
        publication = json.loads((directory / f"{target}.json").read_text())
        if publication.get("series") != variant.series or publication.get("odoo_source_revision") != source_revision:
            raise ValueError("Publication identity does not match resolved inputs")
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
            families.setdefault(alias_family(tag, variant), {}).setdefault(target, []).append(alias)
    for family, targets in families.items():
        if set(targets) != set(TARGETS):
            raise ValueError("Incomplete alias family")
        if family != "source" and {alias.removesuffix("-runtime") for alias in targets["runtime"]} != {
            alias.removesuffix("-devtools") for alias in targets["devtools"]
        }:
            raise ValueError("Alias family differs between image targets")
    return digests, families


def promote(repository: str, directory: Path, order: int, variant: ImageVariant, source_revision: str) -> Promotion:
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise ValueError("Invalid Odoo source revision")
    image_root = f"ghcr.io/{repository.lower()}"
    digests, families = read_publications(directory, image_root, variant, source_revision)
    for digest in digests.values():
        if inspect_order(f"{image_root}@{digest}", variant, digest, source_revision) != order:
            raise ValueError("Candidate does not belong to this checked workflow run")
    selected: dict[str, list[str]] = {target: [] for target in TARGETS}
    delivered: dict[str, bool] = {}
    for family, targets in families.items():
        superseded = False
        for aliases in targets.values():
            for alias in aliases:
                if inspect_order(alias, variant, allow_legacy=True) > order:
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
            inspect_order(alias, variant, digests[target], source_revision)
    delivered["dispatch"] = variant.enterprise_followup and (
        delivered.get("stable", False)
        or delivered.get("nightly", False)
        or ("stable" not in families and "nightly" not in families and delivered.get("source", False))
    )
    return Promotion(delivered, digests)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--series", required=True)
    parser.add_argument("--odoo-source-revision", required=True)
    arguments = parser.parse_args()
    variants = read_variants(Path(__file__).resolve().parents[1] / "image-variants.json")
    variant = next((variant for variant in variants if variant.series == arguments.series), None)
    if variant is None:
        parser.error("Unsupported Odoo series")
    order = parse_order(os.environ["GITHUB_RUN_NUMBER"])
    result = promote(os.environ["GITHUB_REPOSITORY"], arguments.directory, order, variant, arguments.odoo_source_revision)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for family in ("stable", "nightly", "dispatch"):
            output.write(f"{family}={str(result.delivered.get(family, False)).lower()}\n")
        for target, digest in result.digests.items():
            output.write(f"{target}_digest={digest}\n")


if __name__ == "__main__":
    main()
