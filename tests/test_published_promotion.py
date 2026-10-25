import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HELPER_PATH = Path(__file__).resolve().parents[1] / "scripts/promote-published-images.py"
SPEC = importlib.util.spec_from_file_location("promotion", HELPER_PATH)
promotion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promotion)
ROOT = "ghcr.io/example/images"


class Registry:
    def __init__(self) -> None:
        self.manifests: dict[str, str] = {}
        self.aliases: dict[str, str] = {}
        self.mutations: list[str] = []
        self.unavailable: set[str] = set()
        self.planned: set[str] = set()
        self.fail_apply = ""

    def add_manifest(self, order: str | None, target: str) -> str:
        annotations = {} if order is None else {promotion.ORDER_ANNOTATION: order}
        raw = json.dumps({"annotations": annotations, "target": target})
        digest = f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}"
        self.manifests[digest] = raw
        return digest

    def bind(self, tag: str, order: str | None, target: str) -> None:
        self.aliases[f"{ROOT}:{tag}-{target}"] = self.add_manifest(order, target)

    def run(self, arguments: list[str]) -> str:
        if arguments == ["gh", "api", "repos/example/images"]:
            return json.dumps({"owner": {"type": "User"}})
        if arguments == ["gh", "api", "users/example/packages/container/images/versions?per_page=100", "--paginate", "--slurp"]:
            return json.dumps(
                [
                    [
                        {"name": digest, "metadata": {"container": {"tags": [alias.split(":")[1]]}}}
                        for alias, digest in self.aliases.items()
                    ]
                ]
            )
        if arguments[:5] == ["docker", "buildx", "imagetools", "inspect", "--raw"]:
            reference = arguments[5]
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
            self.digests[target] = self.registry.add_manifest("20.2", target)
            record = {
                "digest": self.digests[target],
                "aliases": "\n".join(f"{ROOT}:{tag}-{target}" for tag in ("19.0", "sha-a", "nightly", "nightly-20261025")),
            }
            (self.path / f"{target}.json").write_text(json.dumps(record))
        self.patch = patch.object(promotion, "run_command", side_effect=self.registry.run)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def promote(self) -> dict[str, bool]:
        return promotion.promote("example/images", self.path, (20, 2))

    def test_publishes_complete_checked_pairs_and_dispatches(self) -> None:
        result = self.promote()
        self.assertTrue(result["dispatch"])
        for target, digest in self.digests.items():
            for tag in ("19.0", "sha-a", "nightly", "nightly-20261025"):
                self.assertEqual(self.registry.aliases[f"{ROOT}:{tag}-{target}"], digest)

    def test_newer_alias_protects_its_pair_without_starving_other_channels(self) -> None:
        for family, newer in (("stable", "21.1"), ("stable", "20.3"), ("nightly", "21.1"), ("source", "21.1")):
            with self.subTest(family=family, newer=newer):
                tag = {"stable": "19.0", "nightly": "nightly", "source": "sha-a"}[family]
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
        self.registry.bind("19.0", "20.2", "runtime")
        self.registry.bind("19.0", None, "devtools")
        self.assertTrue(self.promote()["stable"])
        self.assertEqual(self.registry.aliases[f"{ROOT}:19.0-devtools"], self.digests["devtools"])

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
                    record["aliases"] = f"{ROOT}:19.0-runtime"
                elif fault == "syntax":
                    record["aliases"] = f"{ROOT}:invalid tag-devtools"
                elif fault == "order":
                    record["digest"] = self.registry.add_manifest("20.1", "devtools")
                else:
                    self.registry.unavailable.add(record["digest"])
                path.write_text(json.dumps(record))
                with self.assertRaises((ValueError, RuntimeError)):
                    self.promote()
                self.assertFalse(self.registry.mutations)
                path.write_text(original)
                self.registry.unavailable.clear()

    def test_malformed_existing_order_stops_before_writes(self) -> None:
        for order in ("", "invalid", "-1.1", "20", "0.1"):
            with self.subTest(order=order):
                self.registry.bind("19.0", order, "devtools")
                with self.assertRaises(ValueError):
                    self.promote()
                self.assertFalse(self.registry.mutations)

    def test_partial_apply_failure_does_not_report_delivery_and_retry_repairs(self) -> None:
        self.registry.fail_apply = self.digests["devtools"]
        with self.assertRaises(RuntimeError):
            self.promote()
        self.registry.fail_apply = ""
        self.assertTrue(self.promote()["stable"])
        self.assertEqual(self.registry.aliases[f"{ROOT}:19.0-devtools"], self.digests["devtools"])

    def test_changed_registry_binding_stops_before_writes(self) -> None:
        self.registry.bind("19.0", "19.1", "runtime")
        original = self.registry.run

        def change_binding(arguments: list[str]) -> str:
            result = original(arguments)
            if "--slurp" in arguments:
                self.registry.bind("19.0", "21.1", "runtime")
            return result

        with patch.object(promotion, "run_command", side_effect=change_binding), self.assertRaises(ValueError):
            self.promote()
        self.assertFalse(self.registry.mutations)

    def test_superseded_stable_does_not_dispatch_old_bases_as_a_source_only_run(self) -> None:
        for target in promotion.TARGETS:
            path = self.path / f"{target}.json"
            record = json.loads(path.read_text())
            record["aliases"] = "\n".join(f"{ROOT}:{tag}-{target}" for tag in ("19.0", "sha-a"))
            path.write_text(json.dumps(record))
            self.registry.bind("19.0", "21.1", target)
        result = self.promote()
        self.assertTrue(result["source"])
        self.assertFalse(result["dispatch"])

    def test_unavailable_or_ambiguous_inventory_stops_before_writes(self) -> None:
        original = self.registry.run
        for fault in ("unavailable", "malformed", "duplicate"):
            with self.subTest(fault=fault):
                self.registry.bind("19.0", "19.1", "runtime")

                def invalid_inventory(arguments: list[str], failure: str = fault) -> str:
                    result = original(arguments)
                    if "--slurp" not in arguments:
                        return result
                    if failure == "unavailable":
                        raise RuntimeError("Package API unavailable")
                    if failure == "malformed":
                        return "{}"
                    return json.dumps(json.loads(result) * 2)

                with patch.object(promotion, "run_command", side_effect=invalid_inventory):
                    with self.assertRaises((ValueError, RuntimeError)):
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


if __name__ == "__main__":
    unittest.main()
