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


def observe_group_sources(environment: Any, user_id: int) -> dict[str, Any]:
    cursor = environment.cr
    cursor.execute("SELECT active FROM res_users WHERE id=%s", (user_id,))
    user = cursor.fetchone()
    cursor.execute("SELECT gid FROM res_groups_users_rel WHERE uid=%s ORDER BY gid", (user_id,))
    groups = [row[0] for row in cursor.fetchall()]
    cursor.execute("SELECT pg_backend_pid(), txid_current_snapshot()::text")
    backend, snapshot = cursor.fetchone()
    root = environment(su=True)
    record = root["res.users"].browse(user_id)
    default = environment.transaction.default_env
    return {
        "user_exists": user is not None,
        "active": user[0] if user else None,
        "sql_group_ids": groups,
        "orm_group_ids": record.group_ids.ids if user else None,
        "orm_all_group_ids": record.all_group_ids.ids if user else None,
        "method_group_ids": list(record._get_group_ids()) if user else None,
        "group_xmlids": {name: root.ref(name).id for name in ("base.group_user", "base.group_portal", "base.group_public")},
        "uid": environment.uid,
        "su": environment.su,
        "default_env_uid": default.uid if default is not None else None,
        "default_env_su": default.su if default is not None else None,
        "cursor_type": type(cursor).__name__,
        "backend_pid": backend,
        "transaction_snapshot": snapshot,
    }


def observe_as_first_principal(case: Any, role: str, user_id: int, setup: dict[str, Any]) -> dict[str, Any]:
    with case.registry.cursor() as cursor:
        environment = api.Environment(cursor, user_id, {})
        case.assertEqual(environment.transaction.default_env.uid, user_id)
        case.assertFalse(environment.transaction.default_env.su)
        own = environment["res.users"].browse(user_id)
        case.assertFalse(own.env.su)
        case.assertNotEqual(own.env.uid, SUPERUSER_ID)
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
        actor = {"actor_id": user_id, "membership": membership, "operations": {}, "first_environment_verified": True}
        NESTED_OBSERVATIONS[role] = actor
        actor["group_sources"] = {
            "main": observe_group_sources(case.env, user_id),
            "fresh": observe_group_sources(environment, user_id),
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
        operations = actor["operations"]
        for name, command in commands.items():
            observation = {
                "allowed": False,
                "error": None,
                "uid": own.env.uid,
                "su": own.env.su,
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
        actor["allow_sudo_commands"] = environment["res.users.apikeys"]._allow_sudo_commands
        return actor


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
