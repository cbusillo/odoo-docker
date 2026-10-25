import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

BUILD_WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/build.yml"


def load_workflow(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as workflow_file:
        return yaml.safe_load(workflow_file)


class PublicationPromotionTest(unittest.TestCase):
    def test_both_checked_images_are_required_before_any_alias_changes(self) -> None:
        workflow = load_workflow(BUILD_WORKFLOW)
        step = next(step for step in workflow["jobs"]["trigger_enterprise"]["steps"] if "run" in step)
        for valid_pair in (False, True):
            with self.subTest(valid_pair=valid_pair), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                publications = root / "published-digests"
                publications.mkdir()
                expected = set()
                for suffix, character in (("runtime", "a"), ("devtools", "b")):
                    digest = f"sha256:{character * 64}"
                    expected.add(f"ghcr.io/example/images@{digest}")
                    (publications / f"{suffix}.json").write_text(
                        json.dumps(
                            {
                                "digest": digest if valid_pair or suffix == "runtime" else "invalid",
                                "aliases": f"ghcr.io/example/images:fixture-{suffix}",
                            }
                        )
                    )
                docker = root / "docker"
                docker.write_text("""#!/bin/bash
set -euo pipefail
if [ "$3" = create ]; then
    printf '%s\\n' "${!#}" >> "$MUTATIONS"
    printf '%s\\n' "${!#}" | sed 's/.*@//' > "$STATE"
else
    printf 'Digest: %s\\n' "$(cat "$STATE")"
fi
""")
                docker.chmod(0o755)
                mutations = root / "mutations"
                result = subprocess.run(
                    ["bash", "-c", step["run"]],
                    env={
                        "PATH": f"{root}:{os.environ['PATH']}",
                        "RUNNER_TEMP": str(root),
                        "GITHUB_REPOSITORY": "example/images",
                        "MUTATIONS": str(mutations),
                        "STATE": str(root / "state"),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if valid_pair:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(set(mutations.read_text().splitlines()), expected)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(mutations.exists())


if __name__ == "__main__":
    unittest.main()
