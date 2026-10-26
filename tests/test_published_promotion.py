import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

HELPER_PATH = Path(__file__).resolve().parents[1] / "scripts/promote-published-images.py"
SPEC = importlib.util.spec_from_file_location("promotion", HELPER_PATH)
promotion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promotion)
ROOT = "ghcr.io/example/images"
VARIANTS = promotion.read_variants(HELPER_PATH.parents[1] / "image-variants.json")
LEGACY = next(variant for variant in VARIANTS if not variant.alias_prefix)
SOURCE_REVISION = "a" * 40


class Registry:
    def __init__(self) -> None:
        self.manifests: dict[str, str] = {}
        self.aliases: dict[str, str] = {}
        self.mutations: list[str] = []
        self.unavailable: set[str] = set()
        self.planned: set[str] = set()
        self.fail_apply = ""

    def add_manifest(
        self, order: str | None, target: str, variant: promotion.ImageVariant = LEGACY, source_revision: str = SOURCE_REVISION
    ) -> str:
        annotations = (
            {}
            if order is None
            else {
                promotion.ORDER_ANNOTATION: order,
                promotion.SERIES_ANNOTATION: variant.series,
                promotion.SOURCE_ANNOTATION: source_revision,
            }
        )
        raw = json.dumps({"annotations": annotations, "target": target})
        digest = f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}"
        self.manifests[digest] = raw
        return digest

    def bind(self, tag: str, order: str | None, target: str) -> None:
        self.aliases[f"{ROOT}:{tag}-{target}"] = self.add_manifest(order, target)

    def run(self, arguments: list[str]) -> str:
        if arguments[:5] == ["docker", "buildx", "imagetools", "inspect", "--raw"]:
            reference = arguments[5]
            if "@" not in reference and reference not in self.aliases:
                raise subprocess.CalledProcessError(1, arguments, stderr=f"ERROR: {reference}: not found\n")
            digest = reference.split("@")[1] if "@" in reference else self.aliases[reference]
            if digest in self.unavailable:
                raise RuntimeError("Manifest unavailable")
            return self.manifests[digest]
        if arguments[:4] != ["docker", "buildx", "imagetools", "create"]:
            raise AssertionError(arguments)
        digest = arguments[-1].split("@")[1]
        aliases = [arguments[index + 1] for index, argument in enumerate(arguments) if argument == "--tag"]
        if not aliases or digest in self.unavailable:
            raise RuntimeError("Invalid plan")
        if "--dry-run" in arguments:
            self.planned.add(digest)
            return self.manifests[digest]
        if len(self.planned) != 2:
            raise AssertionError("Both targets must be planned before any writes")
        if digest == self.fail_apply:
            raise RuntimeError("Publish failed")
        for alias in aliases:
            self.mutations.append(alias)
            self.aliases[alias] = digest
        return ""


class PublicationPromotionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = Registry()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.digests: dict[str, str] = {}
        for target in promotion.TARGETS:
            self.digests[target] = self.registry.add_manifest("20", target)
            record = {
                "digest": self.digests[target],
                "series": LEGACY.series,
                "odoo_source_revision": SOURCE_REVISION,
                "aliases": "\n".join(f"{ROOT}:{tag}-{target}" for tag in (LEGACY.series, "sha-a", "nightly", "nightly-20261025")),
            }
            (self.path / f"{target}.json").write_text(json.dumps(record))
        self.patch = patch.object(promotion, "run_command", side_effect=self.registry.run)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def promote(self) -> dict[str, bool]:
        return promotion.promote("example/images", self.path, 20, LEGACY, SOURCE_REVISION).delivered

    def test_publishes_complete_checked_pairs_and_dispatches(self) -> None:
        result = self.promote()
        self.assertTrue(result["dispatch"])
        for target, digest in self.digests.items():
            for tag in (LEGACY.series, "sha-a", "nightly", "nightly-20261025"):
                self.assertEqual(self.registry.aliases[f"{ROOT}:{tag}-{target}"], digest)

    def test_newer_alias_protects_its_pair_without_starving_other_channels(self) -> None:
        for family, newer in (("stable", "21"), ("nightly", "21"), ("source", "21")):
            with self.subTest(family=family, newer=newer):
                tag = {"stable": LEGACY.series, "nightly": "nightly", "source": "sha-a"}[family]
                self.registry.aliases.clear()
                self.registry.mutations.clear()
                self.registry.bind(tag, newer, "runtime")
                before = dict(self.registry.aliases)
                delivered = self.promote()
                self.assertFalse(delivered[family])
                self.assertTrue(delivered["nightly" if family == "stable" else "stable"])
                self.assertEqual(self.registry.aliases[f"{ROOT}:{tag}-runtime"], before[f"{ROOT}:{tag}-runtime"])
                self.assertNotIn(f"{ROOT}:{tag}-devtools", self.registry.aliases)

    def test_partial_pair_repairs_and_legacy_baseline_migrates(self) -> None:
        self.registry.bind(LEGACY.series, "20", "runtime")
        self.registry.bind(LEGACY.series, None, "devtools")
        self.assertTrue(self.promote()["stable"])
        self.assertEqual(self.registry.aliases[f"{ROOT}:{LEGACY.series}-devtools"], self.digests["devtools"])

    def test_invalid_or_unavailable_second_candidate_never_mutates(self) -> None:
        for fault in ("digest", "empty", "target", "syntax", "order", "unavailable"):
            with self.subTest(fault=fault):
                path = self.path / "devtools.json"
                original = path.read_text()
                record = json.loads(original)
                if fault == "digest":
                    record["digest"] = "invalid"
                elif fault == "empty":
                    record["aliases"] = ""
                elif fault == "target":
                    record["aliases"] = f"{ROOT}:{LEGACY.series}-runtime"
                elif fault == "syntax":
                    record["aliases"] = f"{ROOT}:invalid tag-devtools"
                elif fault == "order":
                    record["digest"] = self.registry.add_manifest("19", "devtools")
                else:
                    self.registry.unavailable.add(record["digest"])
                path.write_text(json.dumps(record))
                with self.assertRaises((ValueError, RuntimeError)):
                    self.promote()
                self.assertFalse(self.registry.mutations)
                path.write_text(original)
                self.registry.unavailable.clear()

    def test_malformed_existing_order_stops_before_writes(self) -> None:
        for order in ("", "invalid", "-1", "20.1", "0"):
            with self.subTest(order=order):
                self.registry.bind(LEGACY.series, order, "devtools")
                with self.assertRaises(ValueError):
                    self.promote()
                self.assertFalse(self.registry.mutations)

    def test_partial_apply_failure_does_not_report_delivery_and_retry_repairs(self) -> None:
        self.registry.fail_apply = self.digests["devtools"]
        with self.assertRaises(RuntimeError):
            self.promote()
        self.registry.fail_apply = ""
        self.assertTrue(self.promote()["stable"])
        self.assertEqual(self.registry.aliases[f"{ROOT}:{LEGACY.series}-devtools"], self.digests["devtools"])

    def test_incorrect_alias_readback_does_not_report_delivery(self) -> None:
        original = self.registry.run

        def corrupt_alias(arguments: list[str]) -> str:
            result = original(arguments)
            if arguments[:4] == ["docker", "buildx", "imagetools", "create"] and "--dry-run" not in arguments:
                self.registry.bind(LEGACY.series, "19", "runtime")
            return result

        with patch.object(promotion, "run_command", side_effect=corrupt_alias), self.assertRaises(ValueError):
            self.promote()

    def test_superseded_stable_does_not_dispatch_old_bases_as_a_source_only_run(self) -> None:
        for target in promotion.TARGETS:
            path = self.path / f"{target}.json"
            record = json.loads(path.read_text())
            record["aliases"] = "\n".join(f"{ROOT}:{tag}-{target}" for tag in (LEGACY.series, "sha-a"))
            path.write_text(json.dumps(record))
            self.registry.bind(LEGACY.series, "21", target)
        result = self.promote()
        self.assertTrue(result["source"])
        self.assertFalse(result["dispatch"])

    def test_registry_errors_other_than_exact_tag_absence_stop_before_writes(self) -> None:
        original = self.registry.run
        for message in ("unauthorized", "forbidden", "unexpected status 503", "other-reference: not found"):
            with self.subTest(message=message):

                def unavailable_registry(arguments: list[str], failure: str = message) -> str:
                    if arguments[:5] == ["docker", "buildx", "imagetools", "inspect", "--raw"] and "@" not in arguments[5]:
                        raise subprocess.CalledProcessError(1, arguments, stderr=failure)
                    return original(arguments)

                with patch.object(promotion, "run_command", side_effect=unavailable_registry):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.promote()
                self.assertFalse(self.registry.mutations)

    def test_failed_jobs_retry_can_reuse_candidates_from_different_attempts(self) -> None:
        for target, attempt in (("runtime", 1), ("devtools", 2)):
            digest = self.registry.add_manifest("20", f"{target}-attempt-{attempt}")
            self.digests[target] = digest
            path = self.path / f"{target}.json"
            record = json.loads(path.read_text())
            record["digest"] = digest
            path.write_text(json.dumps(record))
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "3"}):
            self.assertTrue(self.promote()["stable"])

    def test_unique_build_aliases_survive_shared_source_supersession(self) -> None:
        for target in promotion.TARGETS:
            path = self.path / f"{target}.json"
            record = json.loads(path.read_text())
            record["aliases"] = f"{ROOT}:sha-a-{target}\n{ROOT}:build-123-abcd-{target}"
            path.write_text(json.dumps(record))
            self.registry.bind("sha-a", "21", target)
        self.assertFalse(self.promote()["source"])
        for target, digest in self.digests.items():
            self.assertEqual(self.registry.aliases[f"{ROOT}:build-123-abcd-{target}"], digest)

    def test_enterprise_source_aliases_can_differ_between_targets(self) -> None:
        for target, base in (("runtime", "abc"), ("devtools", "def")):
            path = self.path / f"{target}.json"
            record = json.loads(path.read_text())
            record["aliases"] = f"{ROOT}:sha-abcd-{base}-{target}"
            path.write_text(json.dumps(record))
        self.assertTrue(self.promote()["source"])

    def test_floating_and_dated_nightly_pairs_do_not_suppress_each_other(self) -> None:
        for newer in ("nightly", "nightly-20261025"):
            with self.subTest(newer=newer):
                self.registry.aliases.clear()
                self.registry.bind(newer, "21", "runtime")
                result = self.promote()
                self.assertFalse(result[newer])
                other = "nightly-20261025" if newer == "nightly" else "nightly"
                self.assertTrue(result[other])
                for target, digest in self.digests.items():
                    self.assertEqual(self.registry.aliases[f"{ROOT}:{other}-{target}"], digest)

    def test_mismatched_dated_pairs_stop_before_writes(self) -> None:
        path = self.path / "devtools.json"
        record = json.loads(path.read_text())
        record["aliases"] = record["aliases"].replace("nightly-20261025", "nightly-20261026")
        path.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            self.promote()
        self.assertFalse(self.registry.mutations)

    def test_source_only_publication_can_dispatch_without_moving_channels(self) -> None:
        for target in promotion.TARGETS:
            path = self.path / f"{target}.json"
            record = json.loads(path.read_text())
            record["aliases"] = f"{ROOT}:sha-a-{target}"
            path.write_text(json.dumps(record))
        result = self.promote()
        self.assertTrue(result["dispatch"])
        self.assertNotIn("stable", result)
        self.assertNotIn("nightly", result)


class VersionedPromotionTest(unittest.TestCase):
    def test_each_series_promotes_only_its_pair_and_permitted_followup(self) -> None:
        for variant in VARIANTS:
            with self.subTest(series=variant.series), tempfile.TemporaryDirectory() as directory:
                registry = Registry()
                root = Path(directory)
                expected = {}
                for target in promotion.TARGETS:
                    digest = registry.add_manifest("23", target, variant)
                    expected[target] = digest
                    tags = (variant.series, f"{variant.alias_prefix}nightly", f"{variant.alias_prefix}sha-abc")
                    (root / f"{target}.json").write_text(
                        json.dumps(
                            {
                                "digest": digest,
                                "series": variant.series,
                                "odoo_source_revision": SOURCE_REVISION,
                                "aliases": "\n".join(f"{ROOT}:{tag}-{target}" for tag in tags),
                            }
                        )
                    )
                with patch.object(promotion, "run_command", side_effect=registry.run):
                    result = promotion.promote("example/images", root, 23, variant, SOURCE_REVISION)
                self.assertEqual(result.digests, expected)
                self.assertEqual(result.delivered["dispatch"], variant.enterprise_followup)
                for alias, digest in registry.aliases.items():
                    target = alias.rsplit("-", 1)[1]
                    self.assertEqual(digest, expected[target])

    def test_wrong_series_revision_or_alias_never_writes(self) -> None:
        for variant in VARIANTS:
            other = next(candidate for candidate in VARIANTS if candidate != variant)
            for fault in ("manifest-series", "manifest-source", "record-series", "record-source", "alias", "existing-series"):
                with self.subTest(series=variant.series, fault=fault), tempfile.TemporaryDirectory() as directory:
                    registry = Registry()
                    root = Path(directory)
                    for target in promotion.TARGETS:
                        wrong = target == "devtools"
                        digest = registry.add_manifest(
                            "23",
                            target,
                            other if wrong and fault == "manifest-series" else variant,
                            "b" * 40 if wrong and fault == "manifest-source" else SOURCE_REVISION,
                        )
                        record = {
                            "digest": digest,
                            "series": other.series if wrong and fault == "record-series" else variant.series,
                            "odoo_source_revision": "b" * 40 if wrong and fault == "record-source" else SOURCE_REVISION,
                            "aliases": f"{ROOT}:{other.series if wrong and fault == 'alias' else variant.series}-{target}",
                        }
                        (root / f"{target}.json").write_text(json.dumps(record))
                    if fault == "existing-series":
                        registry.aliases[f"{ROOT}:{variant.series}-devtools"] = registry.add_manifest("24", "devtools", other)
                    with patch.object(promotion, "run_command", side_effect=registry.run), self.assertRaises(ValueError):
                        promotion.promote("example/images", root, 23, variant, SOURCE_REVISION)
                    self.assertFalse(registry.mutations)

    def test_variant_configuration_rejects_empty_duplicate_or_ambiguous_namespaces(self) -> None:
        original = json.loads((HELPER_PATH.parents[1] / "image-variants.json").read_text())
        for records in (
            [],
            [original[0], original[0]],
            [{**original[0], "alias_prefix": "wrong-"}],
            [{**original[0], "enterprise_followup": "false"}],
        ):
            with self.subTest(records=records), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "variants.json"
                path.write_text(json.dumps(records))
                with self.assertRaises(ValueError):
                    promotion.read_variants(path)


class NightlyTagTest(unittest.TestCase):
    def test_publication_legs_and_later_attempts_use_the_resolved_date(self) -> None:
        workflow = yaml.safe_load((HELPER_PATH.parents[1] / ".github/workflows/build.yml").read_text())
        resolver = "resolve_source" if "resolve_source" in workflow["jobs"] else "resolve_enterprise_source"
        resolve_step = workflow["jobs"][resolver]["steps"][0]
        tag_step = next(step for step in workflow["jobs"]["publish"]["steps"] if step.get("id") == "tags")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, script in (("git", 'printf "%s refs/heads/fixture\\n" "$SOURCE_REV"'), ("date", 'echo "$CLOCK_DATE"')):
                executable = root / name
                executable.write_text(f"#!/bin/sh\n{script}\n")
                executable.chmod(0o755)
            output = root / "resolved"
            environment = {
                "PATH": f"{root}:{os.environ['PATH']}",
                "SOURCE_REV": "a" * 40,
                "CLOCK_DATE": "20261025",
                "GITHUB_OUTPUT": str(output),
                "ENTERPRISE_SOURCE_TOKEN": "fixture-token",
                "ODOO_ENTERPRISE_REPOSITORY": "example/source",
                "ODOO_ENTERPRISE_REF": "fixture",
            }
            subprocess.run(["bash", "-c", resolve_step["run"]], check=True, capture_output=True, text=True, env=environment)
            resolved = dict(line.split("=", 1) for line in output.read_text().splitlines())
            aliases: list[set[str]] = []
            for target, current_day in (("runtime", "20261025"), ("devtools", "20261026")):
                output.write_text("")
                result = subprocess.run(
                    ["bash", "-c", tag_step["run"].replace("${{ matrix.suffix }}", target)],
                    check=False,
                    capture_output=True,
                    text=True,
                    env={
                        **environment,
                        "CLOCK_DATE": current_day,
                        "NIGHTLY_DATE": resolved["nightly_date"],
                        "ODOO_SOURCE_REV": resolved["value"],
                        "ENTERPRISE_SOURCE_REV": resolved["value"],
                        "BASE_IMAGE_REF": f"ghcr.io/example/base@sha256:{'b' * 64}",
                        "DISPATCH_PUBLISH_STABLE": "false",
                        "DISPATCH_PUBLISH_NIGHTLY": "true",
                        "GITHUB_EVENT_NAME": "schedule" if resolver == "resolve_source" else "workflow_dispatch",
                        "GITHUB_REF": "refs/heads/main",
                        "GITHUB_SHA": "c" * 40,
                        "GITHUB_RUN_ID": "123",
                        "GITHUB_REPOSITORY": "example/images",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                aliases.append(
                    {
                        line.removesuffix(f"-{target}")
                        for line in output.read_text().splitlines()
                        if line.startswith(f"{ROOT}:nightly-")
                    }
                )
            self.assertTrue(aliases[0])
            self.assertEqual(aliases[0], aliases[1])


if __name__ == "__main__":
    unittest.main()
