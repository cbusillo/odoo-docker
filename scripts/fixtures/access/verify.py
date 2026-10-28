from __future__ import annotations

import csv
import importlib
import json
import os
from pathlib import Path
from typing import Any

from odoo import tools
from odoo.addons.base.models.ir_access import IN_SELECTION, NO_OPERATION
from odoo.tests import loader

SNAPSHOT_KEY = "native_empty_access_snapshot"
SOURCE_FILE = Path("/odoo/odoo/addons/test_base/tests/test_orm/zero_access_19.csv")


def base_snapshot(environment: Any) -> dict[str, Any]:
    with SOURCE_FILE.open(newline="") as stream:
        source_rows = list(csv.DictReader(stream))
    if not source_rows:
        raise RuntimeError("The original native access fixture is empty")
    snapshot = {}
    for source in source_rows:
        alias = "base." + source["id"]
        policy = environment.ref(alias)
        if policy.operation != NO_OPERATION or any(policy["for_" + mode] for mode in IN_SELECTION):
            raise RuntimeError(f"Expected the native empty bit vector for {alias}")
        aliases = environment["ir.model.data"].search([("model", "=", policy._name), ("res_id", "=", policy.id)], order="id")
        snapshot[alias] = {
            "id": policy.id,
            "name": policy.name,
            "model_id": policy.model_id.id,
            "group_id": policy.group_id.id,
            "active": policy.active,
            "operation": policy.operation,
            "domain": policy.domain,
            "create_uid": policy.create_uid.id,
            "create_date": str(policy.create_date),
            "aliases": [(row.id, row.module, row.name, row.model, row.res_id, row.noupdate) for row in aliases],
        }
    return snapshot


def run_native_cases() -> dict[str, Any]:
    module = importlib.import_module("odoo.addons.test_base.tests.test_orm.test_ir_rules")
    test_class = module.TestNoOperationAccess
    expected = {case.id() for case in loader.get_module_test_cases(module) if type(case) is test_class}
    if not expected:
        raise RuntimeError("No native empty-operation test cases were discovered")
    old_tags = tools.config["test_tags"]
    tools.config["test_tags"] = "/test_base:" + test_class.__name__
    try:
        suite = loader.make_suite(["test_base"], position="post_install")
        selected = [case.id() for case in suite]
        if set(selected) != expected or len(selected) != len(expected):
            raise RuntimeError(f"Unexpected native empty-operation selection: {selected}")
        result = loader.run_suite(suite)
    finally:
        tools.config["test_tags"] = old_tags
    actual = {str(name) for name in result.stats if str(name).rsplit(".", 1)[-1].startswith("test_")}
    if (
        not result.wasSuccessful()
        or result.failures_count
        or result.errors_count
        or result.skipped
        or result.testsRun != len(expected)
        or actual != expected
    ):
        raise RuntimeError(
            f"Unqualified native empty-operation result: {result.testsRun} tests, "
            f"{result.failures_count} failed, {result.errors_count} errors, "
            f"{len(result.skipped)} skipped, actual {sorted(actual)}"
        )
    return {
        "native_tests_run": result.testsRun,
        "native_failed": result.failures_count,
        "native_errors": result.errors_count,
        "native_skipped": len(result.skipped),
        "native_methods": sorted(actual),
    }


def main(environment: Any) -> None:
    phase = os.environ["NATIVE_ACCESS_PHASE"]
    parameters = environment["ir.config_parameter"]
    if phase == "capture":
        if parameters.get_param(SNAPSHOT_KEY):
            raise RuntimeError("Native empty-operation snapshot already exists")
        initial = base_snapshot(environment)
        first = environment["ir.access"].browse(next(iter(initial.values()))["id"])
        first.active = False
        environment["ir.model.data"].create(
            {"module": "__custom__", "name": "zero_replay", "model": first._name, "res_id": first.id, "noupdate": True}
        )
        snapshot = base_snapshot(environment)
        parameters.set_param(SNAPSHOT_KEY, json.dumps(snapshot, sort_keys=True))
        environment.cr.commit()
        print(
            json.dumps(
                {"phase": phase, "native_base_records": len(snapshot), "inactive_and_protected_alias_control": True}, sort_keys=True
            )
        )
        return
    if phase != "verify":
        raise RuntimeError(f"Unsupported native empty-operation phase: {phase}")
    expected = json.loads(parameters.get_param(SNAPSHOT_KEY) or "{}")
    actual = json.loads(json.dumps(base_snapshot(environment)))
    if not expected or actual != expected:
        raise RuntimeError("Native base update changed retained empty-operation records or aliases")
    qualified = run_native_cases()
    print(
        json.dumps(
            {
                "phase": phase,
                "native_base_records": len(actual),
                "actual_base_update_retained_records_and_aliases": True,
                **qualified,
            },
            sort_keys=True,
        )
    )


main(globals()["env"])
