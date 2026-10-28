from __future__ import annotations

import ast
import csv
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

SNAPSHOT_KEY = "native_zero_access_replay"
SOURCE_FILE = Path(os.environ["NATIVE_ACCESS_SOURCE"])


def source_policies() -> dict[str, dict[str, str]]:
    with SOURCE_FILE.open(newline="") as stream:
        return {row["xmlid"]: row for row in csv.DictReader(stream)}


def all_policies(environment: Any) -> dict[str, Any]:
    policies = (
        environment["ir.access"].with_context(active_test=False).search([], order="id")
    )
    aliases = environment["ir.model.data"].search(
        [("model", "=", "ir.access"), ("res_id", "in", policies.ids)], order="id"
    )
    by_policy = defaultdict(list)
    for alias in aliases:
        by_policy[alias.res_id].append(
            (
                alias.id,
                alias.module,
                alias.name,
                alias.model,
                alias.res_id,
                alias.noupdate,
                alias.create_uid.id,
                str(alias.create_date),
            )
        )
    result = {
        str(policy.id): {
            "name": policy.name,
            "model": policy.model_id.model,
            "model_id": policy.model_id.id,
            "group_id": policy.group_id.id,
            "operation": policy.operation,
            "domain": policy.domain,
            "active": policy.active,
            "note": policy.note,
            "create_uid": policy.create_uid.id,
            "create_date": str(policy.create_date),
            "aliases": by_policy[policy.id],
        }
        for policy in policies
    }
    return json.loads(json.dumps(result))


def retained_policies(environment: Any, policies: dict[str, Any]) -> dict[str, Any]:
    from odoo import fields

    source = source_policies()
    aliases = {
        f"{alias[1]}.{alias[2]}": identifier
        for identifier, row in policies.items()
        for alias in row["aliases"]
    }
    group_ids = {
        name: environment.ref(name).id
        for name in {row["group"] for row in source.values()}
        if name
    }
    result = {}
    for alias, original in source.items():
        if alias not in aliases:
            raise RuntimeError(f"Missing retained native policy: {alias}")
        identifier = aliases[alias]
        row = policies[identifier]
        if row["model"] != original["model"] or row["group_id"] != group_ids.get(
            original["group"], False
        ):
            raise RuntimeError(f"Retained native model/group changed: {alias}")
        domain = (
            fields.Domain(ast.literal_eval(row["domain"]))
            if row["domain"]
            else fields.Domain.TRUE
        )
        if (row["group_id"] and not domain.is_false()) or (
            not row["group_id"] and not domain.is_true()
        ):
            raise RuntimeError(f"Retained native policy is not neutral: {alias}")
        result[alias] = {"id": identifier, **row}
    if not result or len({row["id"] for row in result.values()}) != len(result):
        raise RuntimeError(
            "Retained source policies do not have distinct native records"
        )
    return result


def capture_controls(environment: Any, parameters: Any) -> None:
    saved = json.loads(parameters.get_str(SNAPSHOT_KEY) or "{}")
    current = all_policies(environment)
    retained = retained_policies(environment, current)
    source = source_policies()
    before = saved.get("before")
    if before is None or any(
        current.get(identifier) != row for identifier, row in before.items()
    ):
        raise RuntimeError("Native owner update changed a pre-existing policy or alias")
    if set(current) - set(before) != {row["id"] for row in retained.values()} - set(
        before
    ):
        raise RuntimeError("Native owner update created an unexpected policy")
    if any(row["name"] != source[alias]["name"] for alias, row in retained.items()):
        raise RuntimeError(
            "The first native source update changed a retained source name"
        )
    base_alias = next(alias for alias in retained if alias.startswith("base."))
    other_alias = next(alias for alias in retained if not alias.startswith("base."))
    base = environment.ref(base_alias)
    metadata = environment["ir.model.data"].search(
        [("module", "=", "base"), ("name", "=", base_alias.split(".", 1)[1])]
    )
    if len(metadata) != 1:
        raise RuntimeError("The base protection control is ambiguous")
    metadata.noupdate = True
    base.name += " retained protected name"
    base.active = False
    environment.ref(other_alias).active = False
    environment["ir.model.data"].create(
        {
            "module": "__custom__",
            "name": "retained_zero_reference",
            "model": base._name,
            "res_id": base.id,
            "noupdate": True,
        }
    )
    owners = sorted({row["owner"] for row in source.values()})
    snapshot = {
        "policies": all_policies(environment),
        "owners": environment["ir.module.module"]
        .search([("name", "in", owners)], order="name")
        .read(["name", "state", "latest_version"]),
        "base_inactive": base_alias,
        "nonbase_inactive": other_alias,
    }
    parameters.set_str(SNAPSHOT_KEY, json.dumps(snapshot, sort_keys=True))
    environment.cr.commit()
    print(
        json.dumps(
            {
                "phase": "capture",
                "retained_policies": len(retained),
                "inactive_and_protected_controls": True,
            }
        )
    )


def verify_replay(environment: Any, parameters: Any) -> None:
    saved = json.loads(parameters.get_str(SNAPSHOT_KEY) or "{}")
    current = all_policies(environment)
    retained = retained_policies(environment, current)
    if not saved or saved["policies"] != current:
        raise RuntimeError(
            "Normal native owner replay changed policy/alias identity, protection, state or create audit"
        )
    owners = sorted({row["owner"] for row in source_policies().values()})
    module_state = (
        environment["ir.module.module"]
        .search([("name", "in", owners)], order="name")
        .read(["name", "state", "latest_version"])
    )
    if module_state != saved["owners"]:
        raise RuntimeError("Normal owner replay changed module identity/state/version")
    if (
        environment.ref(saved["base_inactive"]).active
        or environment.ref(saved["nonbase_inactive"]).active
    ):
        raise RuntimeError("Inactive native replay controls did not remain inactive")
    print(
        json.dumps(
            {
                "phase": "verify",
                "retained_policies": len(retained),
                "native_policy_and_alias_replay_equal": True,
                "original_19_migration_qualified": False,
                "legacy_owner_module_lifecycle_qualified": False,
            }
        )
    )


def main(environment: Any, phase: str) -> None:
    if not environment.registry.ready:
        raise RuntimeError("The native registry is incomplete")
    parameters = environment["ir.config_parameter"]
    if phase == "before":
        if parameters.get_str(SNAPSHOT_KEY):
            raise RuntimeError("The native zero-policy snapshot already exists")
        parameters.set_str(
            SNAPSHOT_KEY,
            json.dumps({"before": all_policies(environment)}, sort_keys=True),
        )
        environment.cr.commit()
        return
    if phase == "capture":
        capture_controls(environment, parameters)
        return
    if phase == "verify":
        verify_replay(environment, parameters)
        return
    raise RuntimeError(f"Unsupported native zero-policy phase: {phase}")


phase = os.environ["NATIVE_ACCESS_PHASE"]
if phase == "owners":
    print(",".join(sorted({row["owner"] for row in source_policies().values()})))
else:
    main(globals()["env"], phase)
