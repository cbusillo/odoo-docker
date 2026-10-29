from __future__ import annotations

import json
import logging
from typing import Any

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .delegated_access import snapshot_delegated

GROUP_POLICY_OBSERVATIONS: dict[str, Any] = {}
_logger = logging.getLogger(__name__)


class RevertGroupOperation(Exception):
    pass


def observe_operation(
    case: Any, environment: Any, policy: bool, operation: Any, mutation: bool, target_id: int | None = None
) -> dict[str, Any]:
    environment.invalidate_all()
    cursor = environment.cr
    default = environment.transaction.default_env
    before = snapshot_delegated(cursor)
    result = {
        "policy_allowed": policy,
        "allowed": False,
        "error": None,
        "sql_changed": False,
        "target_row_changed": False,
        "target_before_present": False,
        "target_after_present": False,
    }
    try:
        with cursor.savepoint():
            value = operation()
            result["allowed"] = bool(value)
            environment.flush_all()
            after = snapshot_delegated(cursor)
            result["sql_changed"] = after != before
            if mutation and result["allowed"]:
                target = target_id if target_id is not None else value.id
                before_row = next((row for row in before[1] if row[0] == target), None)
                after_row = next((row for row in after[1] if row[0] == target), None)
                result.update(
                    target_id=target,
                    target_row_changed=before_row != after_row,
                    target_before_present=before_row is not None,
                    target_after_present=after_row is not None,
                )
                case.assertTrue(result["target_row_changed"])
            if result["allowed"]:
                case.assertEqual(result["sql_changed"], mutation)
            raise RevertGroupOperation
    except UserError as error:
        result["allowed"] = False
        result["error"] = {"type": type(error).__name__, "message": str(error)}
    except RevertGroupOperation:
        pass
    case.assertFalse(environment.su)
    case.assertIs(environment.transaction.default_env, default)
    case.assertEqual(snapshot_delegated(cursor), before)
    result.update(uid=environment.uid, su=environment.su, default_uid=default.uid, default_su=default.su, rollback_verified=True)
    return result


def cold_policy_cache(environment: Any) -> None:
    if hasattr(environment.transaction, "invalidate_ormcache"):
        environment.transaction.invalidate_ormcache()
    else:
        environment.registry.clear_cache()


@tagged("post_install", "-at_install")
class TestGroupPolicies(TransactionCase):
    def test_partner_manager_group_combinations(self) -> None:
        selected = self.env.company
        foreign = self.env["res.company"].create({"name": "Group policy foreign company"})
        self.assertFalse(foreign.parent_id)
        groups = {
            name: self.env.ref("base.group_" + name)
            for name in ("partner_manager", "user", "portal", "public", "erp_manager", "system")
        }
        roles = {
            "pm_only": ("partner_manager",),
            "portal_pm": ("portal", "partner_manager"),
            "public_pm": ("public", "partner_manager"),
            "internal_pm": ("user", "partner_manager"),
            "no_role": (),
        }
        users = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                [
                    {
                        "name": "Group policy " + role,
                        "login": "group-policy-" + role,
                        "group_ids": [Command.set([groups[name].id for name in assigned])],
                        "company_id": selected.id,
                        "company_ids": [Command.set(selected.ids)],
                    }
                    for role, assigned in roles.items()
                ]
            )
        )
        users.partner_id.write({"company_id": selected.id})
        common = self.env["res.partner"].create(
            [
                {"name": "Group policy companyless", "company_id": False},
                {"name": "Group policy selected", "company_id": selected.id},
                {"name": "Group policy foreign", "company_id": foreign.id},
            ]
        )
        children = self.env["res.partner"].create(
            [
                {"name": "Group policy child " + role, "parent_id": user.partner_id.id}
                for role, user in zip(roles, users, strict=True)
            ]
        )
        self.env.flush_all()
        self.assertTrue(all(partner.partner_share for partner in common))
        self.assertFalse(any(partner.user_ids for partner in common))
        country = self.env.ref("base.us")
        self.env.invalidate_all()
        GROUP_POLICY_OBSERVATIONS.clear()
        GROUP_POLICY_OBSERVATIONS.update(
            scope="ordinary scalar Partner/Country operations in a standard rooted TransactionCase; not first-principal nested commands",
            selected_company_id=selected.id,
            foreign_company_id=foreign.id,
            country_id=country.id,
            roles={},
        )
        for (role, assigned), user, child in zip(roles.items(), users, children, strict=True):
            environment = user.with_user(user).with_context(allowed_company_ids=selected.ids).env
            self.assertFalse(environment.su)
            self.assertEqual(environment.uid, user.id)
            self.assertNotEqual(environment.uid, self.env.uid)
            actor = environment["res.users"].browse(user.id)
            membership = {name: actor.has_group("base.group_" + name) for name in groups}
            self.assertEqual({name for name, member in membership.items() if member}, set(assigned))
            targets = {
                "commercial": user.partner_id,
                "child": child,
                "companyless": common[0],
                "selected": common[1],
                "foreign": common[2],
            }
            observation = {
                "actor_id": user.id,
                "uid": environment.uid,
                "su": environment.su,
                "membership": membership,
                "target_ids": {name: target.id for name, target in targets.items()},
                "model_access": {
                    model: {
                        operation: environment[model].has_access(operation) for operation in ("create", "read", "write", "unlink")
                    }
                    for model in ("res.partner", "res.country")
                },
                "operations": {},
            }
            GROUP_POLICY_OBSERVATIONS["roles"][role] = observation
            operations = observation["operations"]
            for name, target in targets.items():
                record = target.with_env(environment)
                read_policy = record.has_access("read")
                operations[name + "/read"] = observe_operation(
                    self, environment, read_policy, lambda record=record: record.read(["name"]), False
                )
                operations[name + "/search"] = observe_operation(
                    self, environment, read_policy, lambda record=record: record.search_count([("id", "=", record.id)]), False
                )
                operations[name + "/write"] = observe_operation(
                    self,
                    environment,
                    record.has_access("write"),
                    lambda record=record, name=name, role=role: record.write({"name": "Changed group policy " + role + "/" + name}),
                    True,
                    record.id,
                )
                if name != "commercial":
                    operations[name + "/unlink"] = observe_operation(
                        self, environment, record.has_access("unlink"), record.unlink, True, record.id
                    )
            for name, company in (("companyless", False), ("selected", selected.id), ("foreign", foreign.id)):
                operations[name + "/create"] = observe_operation(
                    self,
                    environment,
                    observation["model_access"]["res.partner"]["create"],
                    lambda company=company, environment=environment, role=role: environment["res.partner"].create(
                        {"name": "Created group policy " + role, "company_id": company}
                    ),
                    True,
                )
            country_record = country.with_env(environment)
            operations["country/read"] = observe_operation(
                self,
                environment,
                country_record.has_access("read"),
                lambda country_record=country_record: country_record.read(["name", "code"]),
                False,
            )
            operations["country/search"] = observe_operation(
                self,
                environment,
                country_record.has_access("read"),
                lambda country_record=country_record: country_record.search_count([("id", "=", country_record.id)]),
                False,
            )
            probe = common[0].with_env(environment)
            cache_orders = []
            for order in (("read", "write"), ("write", "read")):
                cold_policy_cache(environment)
                cache_orders.append({operation: probe.has_access(operation) for operation in order})
            self.assertEqual(cache_orders[0], cache_orders[1])
            observation["cache_orders"] = cache_orders
            cold_policy_cache(environment)
            observation["spoofed_read_policy"] = probe.with_context(access_operation="write").has_access("read")
            self.assertEqual(observation["spoofed_read_policy"], cache_orders[0]["read"])
        _logger.info("Native group-policy observations: %s", json.dumps(GROUP_POLICY_OBSERVATIONS, sort_keys=True))
        manager = GROUP_POLICY_OBSERVATIONS["roles"]["pm_only"]
        unassigned = GROUP_POLICY_OBSERVATIONS["roles"]["no_role"]
        for operation in ("companyless/read", "companyless/search", "country/read", "country/search"):
            self.assertTrue(manager["operations"][operation]["allowed"])
            self.assertFalse(unassigned["operations"][operation]["allowed"])
        self.assertFalse(manager["operations"]["foreign/read"]["allowed"])
        self.assertFalse(manager["membership"]["user"])
