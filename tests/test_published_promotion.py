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
    def test_complete_alias_pairs_are_planned_before_any_changes(self) -> None:
        workflow = load_workflow(BUILD_WORKFLOW)
        step = next(step for step in workflow["jobs"]["trigger_enterprise"]["steps"] if "run" in step)
        for fault in (None, "digest", "empty", "target", "syntax", "source"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                publications = root / "published-digests"
                publications.mkdir()
                initial = {"ghcr.io/example/images:existing": f"sha256:{'c' * 64}"}
                expected = dict(initial)
                for suffix, character in (("runtime", "a"), ("devtools", "b")):
                    digest = f"sha256:{character * 64}"
                    aliases = [f"ghcr.io/example/images:fixture-{suffix}", f"ghcr.io/example/images:other-{suffix}"]
                    expected.update(dict.fromkeys(aliases, digest))
                    record = {"digest": digest, "aliases": "\n".join(aliases)}
                    if suffix == "devtools":
                        if fault == "digest":
                            record["digest"] = "invalid"
                        elif fault == "empty":
                            record["aliases"] = "\n\n"
                        elif fault == "target":
                            record["aliases"] = "ghcr.io/example/images:wrong-runtime"
                        elif fault == "syntax":
                            record["aliases"] = "ghcr.io/example/images:invalid tag-devtools"
                    (publications / f"{suffix}.json").write_text(json.dumps(record))
                docker = root / "docker"
                docker.write_text(r"""#!/usr/bin/env node
const fs = require("node:fs");
const args = process.argv.slice(2);
const state = JSON.parse(fs.readFileSync(process.env.STATE, "utf8"));
if (args[0] !== "buildx" || args[1] !== "imagetools") process.exit(1);
if (args[2] === "inspect") {
    if (!state[args[3]]) process.exit(1);
    process.stdout.write(`Digest: ${state[args[3]]}\n`);
} else if (args[2] === "create") {
    const aliases = [];
    for (let index = 3; index < args.length - 1; index++) {
        if (args[index] === "--tag") aliases.push(args[++index]);
        else if (args[index] !== "--dry-run") process.exit(1);
    }
    const digest = args[args.length - 1].split("@")[1];
    if (!aliases.length || aliases.some(alias => /\s/.test(alias))) process.exit(1);
    if (args.includes("--dry-run")) {
        if (digest === process.env.UNAVAILABLE_SOURCE) process.exit(1);
    } else {
        for (const alias of aliases) state[alias] = digest;
        fs.appendFileSync(process.env.MUTATIONS, JSON.stringify({aliases, digest}) + "\n");
        fs.writeFileSync(process.env.STATE, JSON.stringify(state));
    }
} else process.exit(1);
""")
                docker.chmod(0o755)
                mutations = root / "mutations"
                state = root / "state"
                state.write_text(json.dumps(initial))
                result = subprocess.run(
                    ["bash", "-c", step["run"]],
                    env={
                        "PATH": f"{root}:{os.environ['PATH']}",
                        "RUNNER_TEMP": str(root),
                        "GITHUB_REPOSITORY": "example/images",
                        "MUTATIONS": str(mutations),
                        "STATE": str(state),
                        "UNAVAILABLE_SOURCE": f"sha256:{'b' * 64}" if fault == "source" else "",
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                if fault is None:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(state.read_text()), expected)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(mutations.exists())
                    self.assertEqual(json.loads(state.read_text()), initial)


if __name__ == "__main__":
    unittest.main()
