from __future__ import annotations

from typing import Any

from odoo import SUPERUSER_ID, Command, api
from odoo.exceptions import UserError

NESTED_OBSERVATIONS: dict[str, Any] = {}


class RevertNestedOperation(Exception):
    pass


def setup_sentinels(case: Any) -> dict[str, Any]:
    with case.registry.cursor() as cursor:
        environment = api.Environment(cursor, SUPERUSER_ID, {})
        users = (
            environment["res.users"]
            .with_context(no_reset_password=True)
            .create(
                [
                    {
                        "name": "Nested profile " + role,
                        "login": "nested-profile-" + role,
                        "group_ids": [Command.set(environment.ref(group).ids)],
                        "company_id": environment.company.id,
                        "company_ids": [Command.set(environment.companies.ids)],
                    }
                    for role, group in [
                        ("internal", "base.group_user"),
                        ("portal", "base.group_portal"),
                        ("other", "base.group_user"),
                    ]
                ]
            )
        )
        internal, portal, other = users
        public = environment.ref("base.public_user")
        roles = {"internal": internal.id, "portal": portal.id, "public": public.id}
        environment.flush_all()
        keys = {}
        for role, user_id in {**roles, "other": other.id}.items():
            cursor.execute(
                "INSERT INTO res_users_apikeys (name, user_id, scope, index, key) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                ("Nested sentinel " + role, user_id, "rpc", "fixture0", "unusable-fixture-placeholder"),
            )
            keys[role] = cursor.fetchone()[0]
        return {"roles": roles, "keys": keys, "created_users": users.ids, "created_partners": users.partner_id.ids}


def snapshot_keys(cursor: Any, identifiers: list[int]) -> list[Any]:
    cursor.execute(
        "SELECT id, name, user_id, scope, expiration_date FROM res_users_apikeys WHERE id=ANY(%s) ORDER BY id", (identifiers,)
    )
    return cursor.fetchall()


def observe_as_first_principal(case: Any, role: str, user_id: int, setup: dict[str, Any]) -> dict[str, Any]:
    with case.registry.cursor() as cursor:
        environment = api.Environment(cursor, user_id, {})
        case.assertEqual(environment.transaction.default_env.uid, user_id)
        case.assertFalse(environment.transaction.default_env.su)
        own = environment.user
        case.assertFalse(environment.su)
        case.assertNotEqual(environment.uid, SUPERUSER_ID)
        membership = {
            name: own.has_group(name)
            for name in (
                "base.group_user",
                "base.group_portal",
                "base.group_public",
                "base.group_partner_manager",
                "base.group_erp_manager",
                "base.group_system",
            )
        }
        case.assertTrue(membership["base.group_" + ("user" if role == "internal" else role)])
        case.assertFalse(
            any(membership[name] for name in ("base.group_partner_manager", "base.group_erp_manager", "base.group_system"))
        )
        keys = setup["keys"]
        foreign = keys["other"]
        commands = {
            "foreign_update": Command.update(foreign, {"name": "Nested foreign update"}),
            "foreign_link": Command.link(foreign),
            "foreign_unlink": Command.unlink(foreign),
            "foreign_delete": Command.delete(foreign),
            "foreign_set": Command.set([foreign]),
            "own_clear": Command.clear(),
        }
        before = snapshot_keys(cursor, list(keys.values()))
        operations = {}
        for name, command in commands.items():
            observation = {
                "allowed": False,
                "error": None,
                "uid": environment.uid,
                "su": environment.su,
                "default_env_uid": environment.transaction.default_env.uid,
                "default_env_su": environment.transaction.default_env.su,
            }
            try:
                with cursor.savepoint():
                    observation["allowed"] = bool(own.write({"api_key_ids": [command]}))
                    environment.flush_all()
                    observation["sentinels_changed"] = snapshot_keys(cursor, list(keys.values())) != before
                    raise RevertNestedOperation
            except UserError as error:
                observation["allowed"] = False
                observation["error"] = {"type": type(error).__name__, "message": str(error)}
            except RevertNestedOperation:
                pass
            case.assertEqual(snapshot_keys(cursor, list(keys.values())), before)
            case.assertEqual(environment.transaction.default_env.uid, user_id)
            case.assertFalse(environment.transaction.default_env.su)
            observation["rollback_verified"] = True
            operations[name] = observation
        return {
            "actor_id": user_id,
            "membership": membership,
            "operations": operations,
            "allow_sudo_commands": environment["res.users.apikeys"]._allow_sudo_commands,
            "first_environment_verified": True,
        }


def cleanup_sentinels(case: Any, setup: dict[str, Any]) -> None:
    with case.registry.cursor() as cursor:
        environment = api.Environment(cursor, SUPERUSER_ID, {})
        cursor.execute("DELETE FROM res_users_apikeys WHERE id=ANY(%s)", (list(setup["keys"].values()),))
        environment["res.users"].browse(setup["created_users"]).unlink()
        environment["res.partner"].browse(setup["created_partners"]).exists().unlink()
        environment.flush_all()
        cursor.execute("SELECT id FROM res_users_apikeys WHERE id=ANY(%s)", (list(setup["keys"].values()),))
        case.assertFalse(cursor.fetchall())
        case.assertFalse(environment["res.users"].browse(setup["created_users"]).exists())
        case.assertFalse(environment["res.partner"].browse(setup["created_partners"]).exists())


def observe_nested_commands(case: Any) -> None:
    setup = setup_sentinels(case)
    NESTED_OBSERVATIONS.clear()
    try:
        for role, user_id in setup["roles"].items():
            NESTED_OBSERVATIONS[role] = observe_as_first_principal(case, role, user_id, setup)
    finally:
        cleanup_sentinels(case, setup)
    NESTED_OBSERVATIONS["cleanup_verified"] = True
