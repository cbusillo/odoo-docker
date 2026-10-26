import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
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
    def test_new_series_cannot_adopt_an_existing_alias_without_publication_order(self) -> None:
        variant = next(variant for variant in VARIANTS if variant.alias_prefix)
        registry = Registry()
        digest = registry.add_manifest("23", "runtime", variant)
        manifest = json.loads(registry.manifests[digest])
        del manifest["annotations"][promotion.ORDER_ANNOTATION]
        raw = json.dumps(manifest)
        registry.aliases[f"{ROOT}:{variant.series}-runtime"] = digest = f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}"
        registry.manifests[digest] = raw
        with patch.object(promotion, "run_command", side_effect=registry.run), self.assertRaises(ValueError):
            promotion.inspect_order(f"{ROOT}:{variant.series}-runtime", variant, allow_legacy=True)
        self.assertFalse(registry.mutations)

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


class VerificationPairTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        workflow = yaml.safe_load((HELPER_PATH.parents[1] / ".github/workflows/build.yml").read_text())
        self.steps = {
            identifier: next(step["run"] for step in workflow["jobs"][job]["steps"] if step.get("id") == identifier)
            for identifier, job in (("verification_pass", "verify"), ("verified_pair", "publish"))
        }
        inputs = self.root / "dependency-health-inputs"
        inputs.mkdir()
        self.inputs = inputs / "comparison-inputs.json"
        self.inputs.write_text(json.dumps({"scanner": {"image": "fixture", "database": "pinned"}, "cutoff": "fixture"}))
        self.configuration = hashlib.sha256(self.inputs.read_bytes()).hexdigest()
        self.commit = "c" * 40

    def run_step(self, identifier: str, variant: promotion.ImageVariant, suffix: str) -> subprocess.CompletedProcess[str]:
        target = "runtime-devtools" if suffix == "devtools" else suffix
        key = f"{variant.series}-{suffix}"
        bindings = {
            "env.VARIANT_KEY": key,
            "github.event.pull_request.head.sha || github.sha": self.commit,
            "matrix.target": target,
            "needs.resolve_inputs.outputs.scan_configuration_sha256": self.configuration,
        }
        script = self.steps[identifier]
        for name, value in bindings.items():
            script = script.replace("${{ " + name + " }}", value)
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script],
            env={
                "PATH": os.environ["PATH"],
                "RUNNER_TEMP": str(self.root),
                "ODOO_SERIES": variant.series,
                "ODOO_SOURCE_REV": SOURCE_REVISION,
                "GITHUB_SHA": self.commit,
                "GITHUB_RUN_ID": "123",
            },
            capture_output=True,
            text=True,
        )

    def qualify(self, variant: promotion.ImageVariant) -> Path:
        selected = self.root / f"verified-{variant.series}-runtime"
        for suffix in promotion.TARGETS:
            key = f"{variant.series}-{suffix}"
            health = self.root / f"dependency-health-{key}"
            health.mkdir(exist_ok=True)
            provenance = health / "build-provenance.json"
            provenance.write_text(json.dumps({"resolved_inputs": json.loads(self.inputs.read_text())}))
            result = self.run_step("verification_pass", variant, suffix)
            self.assertEqual(result.returncode, 0, result.stderr)
            proof = selected / f"dependency-health-verify-{key}"
            proof.mkdir(parents=True, exist_ok=True)
            (proof / "build-provenance.json").write_bytes(provenance.read_bytes())
        return selected

    def test_each_series_requires_its_complete_pair_without_another_series(self) -> None:
        for variant in VARIANTS:
            with self.subTest(series=variant.series):
                self.qualify(variant)
                result = self.run_step("verified_pair", variant, "runtime")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_failed_or_misbound_second_target_stops_publication(self) -> None:
        variant = VARIANTS[-1]
        selected = self.qualify(variant)
        proof = selected / f"dependency-health-verify-{variant.series}-devtools/build-provenance.json"
        original = proof.read_text()
        mutations = {
            "status": "failed",
            "series": "unselected",
            "odoo_source_revision": "b" * 40,
            "source_commit": "d" * 40,
            "run_id": "122",
            "target": "runtime",
            "scan_configuration_sha256": "0" * 64,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                record = json.loads(original)
                record["verification"][field] = value
                proof.write_text(json.dumps(record))
                self.assertNotEqual(self.run_step("verified_pair", variant, "runtime").returncode, 0)
        record = json.loads(original)
        record["resolved_inputs"]["cutoff"] = "different"
        proof.write_text(json.dumps(record))
        self.assertNotEqual(self.run_step("verified_pair", variant, "runtime").returncode, 0)
        proof.unlink()
        self.assertNotEqual(self.run_step("verified_pair", variant, "runtime").returncode, 0)


class NightlyTagTest(unittest.TestCase):
    def test_publication_legs_and_later_attempts_use_the_resolved_date_and_source(self) -> None:
        root = HELPER_PATH.parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/build.yml").read_text())
        resolve_step = next(step for step in workflow["jobs"]["resolve_source"]["steps"] if step.get("id") == "odoo_source")
        tag_step = next(step for step in workflow["jobs"]["publish"]["steps"] if step.get("id") == "tags")
        revisions = {variant.series: f"{index:x}" * 40 for index, variant in enumerate(VARIANTS, 1)}
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            git_code = 'import json, os, sys; print(json.loads(os.environ["SOURCE_REVISIONS"])[sys.argv[-1].rsplit("/", 1)[1]])'
            git = tools / "git"
            git.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(git_code)} "$@"\n')
            git.chmod(0o755)
            clock = tools / "date"
            clock.write_text('#!/bin/sh\necho "$CLOCK_DATE"\n')
            clock.chmod(0o755)
            output = tools / "resolved"
            environment = {
                "PATH": f"{tools}:{os.environ['PATH']}",
                "SOURCE_REVISIONS": json.dumps(revisions),
                "CLOCK_DATE": "20261025",
                "GITHUB_OUTPUT": str(output),
            }
            subprocess.run(
                ["bash", "-c", resolve_step["run"]], cwd=root, check=True, capture_output=True, text=True, env=environment
            )
            resolved = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(json.loads(resolved["revisions"]), revisions)
            for variant in VARIANTS:
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
                            "ODOO_SOURCE_REV": json.loads(resolved["revisions"])[variant.series],
                            "ODOO_SERIES": variant.series,
                            "ALIAS_PREFIX": variant.alias_prefix,
                            "DISPATCH_PUBLISH_STABLE": "false",
                            "GITHUB_EVENT_NAME": "schedule",
                            "GITHUB_REF": "refs/heads/main",
                            "GITHUB_SHA": "c" * 40,
                            "GITHUB_RUN_ID": "123",
                            "GITHUB_REPOSITORY": "example/images",
                        },
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    tags = {
                        line.removesuffix(f"-{target}") for line in output.read_text().splitlines() if line.startswith(f"{ROOT}:")
                    }
                    self.assertIn(f"{ROOT}:{variant.alias_prefix}sha-{revisions[variant.series][:12]}", tags)
                    self.assertIn(f"{ROOT}:{variant.alias_prefix}nightly-{resolved['nightly_date']}", tags)
                    aliases.append(tags)
                self.assertEqual(aliases[0], aliases[1])


if __name__ == "__main__":
    unittest.main()
