from __future__ import annotations

from typing import Any

from odoo import SUPERUSER_ID, Command, api
from odoo.exceptions import UserError

DELEGATED_OBSERVATIONS: dict[str, Any] = {}


class RevertDelegatedOperation(Exception):
    pass


def setup_delegated_users(case: Any) -> dict[str, Any]:
    with case.registry.cursor() as cursor:
        environment = api.Environment(cursor, SUPERUSER_ID, {})
        users = (
            environment["res.users"]
            .with_context(no_reset_password=True)
            .create(
                [
                    {
                        "name": "Delegated boundary " + role,
                        "login": "delegated-boundary-" + role,
                        "group_ids": [Command.set(environment.ref(group).ids)],
                        "company_id": environment.company.id,
                        "company_ids": [Command.set(environment.companies.ids)],
                    }
                    for role, group in (
                        ("erp", "base.group_erp_manager"),
                        ("internal", "base.group_user"),
                    )
                ]
            )
        )
        partner = environment["res.partner"].create({"name": "Delegated existing partner"})
        return {
            "actor_id": users[0].id,
            "other_id": users[1].id,
            "company_id": environment.company.id,
            "partner_id": partner.id,
            "created_users": users.ids,
            "created_partners": (users.partner_id | partner).ids,
        }


def snapshot_delegated(cursor: Any) -> list[Any]:
    cursor.execute("SELECT id, login, partner_id, active, write_uid, write_date FROM res_users ORDER BY id")
    users = cursor.fetchall()
    cursor.execute("SELECT id, name, company_id, active, write_uid, write_date FROM res_partner ORDER BY id")
    partners = cursor.fetchall()
    cursor.execute("SELECT uid, gid FROM res_groups_users_rel ORDER BY uid, gid")
    return [users, partners, cursor.fetchall()]


def observe_operation(case: Any, cursor: Any, environment: Any, operation: Any) -> dict[str, Any]:
    observation = {"allowed": False, "error": None, "uid": environment.uid, "su": environment.su}
    before = snapshot_delegated(cursor)
    try:
        with cursor.savepoint():
            observation["allowed"] = bool(operation())
            environment.flush_all()
            observation["flushed_change"] = snapshot_delegated(cursor) != before
            raise RevertDelegatedOperation
    except UserError as error:
        observation["allowed"] = False
        observation["error"] = {"type": type(error).__name__, "message": str(error)}
    except RevertDelegatedOperation:
        pass
    case.assertFalse(environment.su)
    case.assertEqual(environment.transaction.default_env.uid, environment.uid)
    case.assertFalse(environment.transaction.default_env.su)
    case.assertEqual(snapshot_delegated(cursor), before)
    observation["rollback_verified"] = True
    return observation


def observe_cache_order(case: Any, setup: dict[str, Any], parent_first: bool) -> dict[str, Any]:
    with case.registry.cursor() as cursor:
        environment = api.Environment(cursor, setup["actor_id"], {"allowed_company_ids": [setup["company_id"]]})
        case.assertFalse(environment.su)
        case.assertEqual(environment.transaction.default_env.uid, setup["actor_id"])
        actor = environment["res.users"].browse(setup["actor_id"])
        other = environment["res.users"].browse(setup["other_id"])
        membership = {
            name: actor.has_group(name)
            for name in ("base.group_user", "base.group_erp_manager", "base.group_partner_manager", "base.group_system")
        }
        case.assertTrue(membership["base.group_user"])
        case.assertTrue(membership["base.group_erp_manager"])
        case.assertFalse(membership["base.group_partner_manager"])
        case.assertFalse(membership["base.group_system"])
        operations = {
            "own_user_write": lambda: actor.write({"login": "delegated-own-login"}),
            "other_user_write": lambda: other.write({"login": "delegated-other-login"}),
            "own_partner_write": lambda: actor.partner_id.write({"name": "Delegated own partner changed"}),
            "other_partner_write": lambda: other.partner_id.write({"name": "Delegated other partner changed"}),
            "create_existing_partner": lambda: (
                environment["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "login": "delegated-existing-partner",
                        "partner_id": setup["partner_id"],
                        "group_ids": [Command.set(environment.ref("base.group_user").ids)],
                    }
                )
            ),
            "create_new_partner": lambda: (
                environment["res.users"]
                .with_context(no_reset_password=True)
                .create(
                    {
                        "name": "Delegated new partner",
                        "login": "delegated-new-partner",
                        "group_ids": [Command.set(environment.ref("base.group_user").ids)],
                    }
                )
            ),
            "other_user_unlink": other.unlink,
        }
        order = list(operations)
        if parent_first:
            order = ["own_partner_write", "other_partner_write"] + [name for name in order if "partner_write" not in name]
        if hasattr(environment.transaction, "invalidate_ormcache"):
            environment.transaction.invalidate_ormcache()
        else:
            environment.registry.clear_cache()
        probes = {"user": other, "parent": other.partner_id}
        probe_order = ("parent", "user") if parent_first else ("user", "parent")
        cache_probes = {name: probes[name].has_access("write") for name in probe_order}
        case.assertTrue(cache_probes["user"])
        case.assertFalse(cache_probes["parent"])
        observations = {name: observe_operation(case, cursor, environment, operations[name]) for name in order}
        for name in ("own_user_write", "other_user_write"):
            case.assertTrue(observations[name]["allowed"], name)
            case.assertTrue(observations[name]["flushed_change"], name)
        for name in ("own_partner_write", "other_partner_write", "create_new_partner"):
            case.assertFalse(observations[name]["allowed"], name)
            case.assertEqual(observations[name]["error"]["type"], "AccessError", name)
        return {
            "membership": membership,
            "operations": observations,
            "cache_probes": cache_probes,
            "first_environment_verified": True,
        }


def observe_write_restriction(case: Any, setup: dict[str, Any]) -> dict[str, Any]:
    with case.registry.cursor() as cursor:
        environment = api.Environment(cursor, setup["actor_id"], {"allowed_company_ids": [setup["company_id"]]})
        other = environment["res.users"].browse(setup["other_id"])
        root = environment(su=True)
        policies = root["ir.access" if "ir.access" in environment else "ir.rule"]
        before = policies.search([]).ids
        domain = repr([("id", "!=", other.sudo().partner_id.id)])
        result = {}
        try:
            with cursor.savepoint():
                result = observe_restricted_operations(case, cursor, environment, root, other, domain)
                raise RevertDelegatedOperation
        except RevertDelegatedOperation:
            pass
        case.assertEqual(policies.search([]).ids, before)
        return result


def observe_restricted_operations(case: Any, cursor: Any, environment: Any, root: Any, other: Any, domain: str) -> dict[str, Any]:
    if "ir.access" in environment:
        root["ir.access"].create(
            {
                "name": "Delegated write-only restriction",
                "model_id": root["ir.model"]._get_id("res.partner"),
                "operation": "u",
                "domain": domain,
            }
        )
    else:
        root["ir.rule"].create(
            {
                "name": "Delegated write-only restriction",
                "model_id": root["ir.model"]._get_id("res.partner"),
                "domain_force": domain,
                "perm_read": False,
                "perm_create": False,
                "perm_write": True,
                "perm_unlink": False,
            }
        )
    read = observe_operation(case, cursor, environment, lambda: other.read(["login"]))
    write = observe_operation(case, cursor, environment, lambda: other.write({"login": "restricted-other-login"}))
    case.assertTrue(read["allowed"])
    case.assertFalse(write["allowed"])
    case.assertEqual(write["error"]["type"], "AccessError")
    return {"read": read, "write": write}


def observe_delegated_access(case: Any) -> None:
    setup = setup_delegated_users(case)
    DELEGATED_OBSERVATIONS.clear()
    try:
        DELEGATED_OBSERVATIONS["user_first"] = observe_cache_order(case, setup, False)
        DELEGATED_OBSERVATIONS["parent_first"] = observe_cache_order(case, setup, True)
        DELEGATED_OBSERVATIONS["write_only_restriction"] = observe_write_restriction(case, setup)
    finally:
        with case.registry.cursor() as cursor:
            environment = api.Environment(cursor, SUPERUSER_ID, {})
            environment["res.users"].browse(setup["created_users"]).unlink()
            environment["res.partner"].browse(setup["created_partners"]).exists().unlink()
            environment.flush_all()
            case.assertFalse(environment["res.users"].browse(setup["created_users"]).exists())
            case.assertFalse(environment["res.partner"].browse(setup["created_partners"]).exists())
    DELEGATED_OBSERVATIONS["cleanup_verified"] = True
