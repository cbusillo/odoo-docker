from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import patch

from odoo import SUPERUSER_ID, Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from .delegated_access import DELEGATED_OBSERVATIONS, observe_delegated_access
from .nested_profile import NESTED_OBSERVATIONS, observe_nested_commands

PROFILE_OBSERVATIONS: dict[str, Any] = {}
USER_READ_OBSERVATIONS: dict[str, Any] = {}
_logger = logging.getLogger(__name__)


class RevertOperation(Exception):
    pass


@tagged("post_install", "-at_install")
class TestOwnProfile(TransactionCase):
    def test_delegated_user_operations_preserve_parent_restrictions(self) -> None:
        observe_delegated_access(self)
        _logger.info("Native delegated access observations: %s", json.dumps(DELEGATED_OBSERVATIONS, sort_keys=True))

    def test_user_read_and_search_boundaries(self) -> None:
        user_model = self.env["res.users"].with_context(no_reset_password=True)
        roles = {"internal": "base.group_user", "portal": "base.group_portal", "other": "base.group_portal"}
        users = user_model.create(
            [
                {
                    "name": "Read boundary " + role,
                    "login": "read-boundary-" + role,
                    "email": role + "@example.invalid",
                    "group_ids": [Command.set(self.env.ref(group).ids)],
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                }
                for role, group in roles.items()
            ]
        )
        internal, portal, other = users
        child = user_model.create(
            {
                "name": "Read boundary child",
                "login": "read-boundary-child",
                "group_ids": [Command.set(self.env.ref("base.group_portal").ids)],
                "company_id": self.env.company.id,
                "company_ids": [Command.set(self.env.company.ids)],
            }
        )
        child.partner_id.parent_id = portal.partner_id
        foreign_company = self.env["res.company"].create({"name": "Read boundary foreign company"})
        self.assertFalse(foreign_company.parent_id)
        other.write({"company_id": foreign_company.id, "company_ids": [Command.set((self.env.company | foreign_company).ids)]})
        other.partner_id.company_id = foreign_company
        public = self.env.ref("base.public_user")
        public_child = user_model.create(
            {
                "name": "Read boundary public child",
                "login": "read-boundary-public-child",
                "group_ids": [Command.set(self.env.ref("base.group_portal").ids)],
                "company_id": self.env.company.id,
                "company_ids": [Command.set(self.env.company.ids)],
            }
        )
        public_child.partner_id.parent_id = public.partner_id
        users |= child | public | public_child
        before = self._snapshot(users)
        USER_READ_OBSERVATIONS.clear()
        targets = {
            "internal": internal,
            "portal": portal,
            "other_partner_company": other,
            "portal_child": child,
            "public": public,
            "public_child": public_child,
        }
        for role, actor in (("internal", internal), ("portal", portal), ("public", public)):
            environment = actor.with_user(actor).with_context(allowed_company_ids=actor.company_ids.ids).env
            self.assertFalse(environment.su)
            self.assertNotEqual(environment.uid, SUPERUSER_ID)
            membership = {
                name: actor.has_group(name)
                for name in (
                    "base.group_user",
                    "base.group_portal",
                    "base.group_public",
                    "base.group_partner_manager",
                    "base.group_erp_manager",
                    "base.group_system",
                )
            }
            self.assertTrue(membership["base.group_" + ("user" if role == "internal" else role)])
            self.assertFalse(
                any(membership[name] for name in ("base.group_partner_manager", "base.group_erp_manager", "base.group_system"))
            )
            observations = {}
            USER_READ_OBSERVATIONS[role] = {
                "actor_id": actor.id,
                "uid": environment.uid,
                "su": environment.su,
                "membership": membership,
                "company_ids": actor.company_ids.ids,
                "other_user_company_ids": other.company_ids.ids,
                "other_user_company_id": other.company_id.id,
                "other_partner_company_id": other.partner_id.company_id.id,
                "other_partner_share": other.partner_id.partner_share,
                "other_partner_company_parent_id": foreign_company.parent_id.id,
                "other_partner_read": other.partner_id.with_env(environment).has_access("read"),
                "operations": observations,
            }
            for label, target in targets.items():
                record = target.with_env(environment)
                parent_read = target.partner_id.with_env(environment).has_access("read")
                child_read = role != "portal" or target.commercial_partner_id == portal.commercial_partner_id
                expected_read = parent_read and child_read
                for operation in ("read", "search"):
                    error = None
                    allowed = False
                    try:
                        if operation == "read":
                            allowed = bool(record.read(["name", "email", "login"]))
                        else:
                            allowed = record.id in record.search([("id", "=", record.id)]).ids
                    except UserError as denied:
                        error = {"type": type(denied).__name__, "message": str(denied)}
                    observations[label + "/" + operation] = {
                        "allowed": allowed,
                        "error": error,
                        "parent_read": parent_read,
                        "target_active": target.active,
                    }
                    expected = expected_read and (operation == "read" or target.active)
                    self.assertEqual(allowed, bool(expected), role + "/" + label + "/" + operation)
            if role == "internal":
                self.assertFalse(observations["other_partner_company/read"]["allowed"])
            self.assertEqual(self._snapshot(users), before)
        _logger.info("Native User read observations: %s", json.dumps(USER_READ_OBSERVATIONS, sort_keys=True))

    def test_own_profile_preserves_bounded_updates(self) -> None:
        self._observe_profile_operations()
        _logger.info("Native profile observations: %s", json.dumps(PROFILE_OBSERVATIONS, sort_keys=True))
        for actor in PROFILE_OBSERVATIONS["roles"].values():
            self.assertFalse(actor["partner_model_write"])
            for name, operation in actor["operations"].items():
                if name in {
                    "own_name",
                    "own_signature",
                    "own_action",
                    "unavailable_company",
                    "name_unavailable_company",
                    "own_mail_preference",
                    "own_warehouse_preference",
                }:
                    self.assertTrue(operation["allowed"], name)
                    self.assertTrue(operation["groups_unchanged"], name)
                    self.assertTrue(operation["company_membership_unchanged"], name)
                    if "company_id" in operation["fields"]:
                        self.assertFalse(operation["company_changed"], name)
                    if name != "unavailable_company":
                        self.assertTrue(operation["flushed_change"], name)
                else:
                    self.assertFalse(operation["allowed"], name)
                    self.assertEqual(operation["error"]["type"], "AccessError", name)

    def test_nested_api_key_commands_keep_the_original_principal(self) -> None:
        observe_nested_commands(self)
        _logger.info("Native nested command observations: %s", json.dumps(NESTED_OBSERVATIONS, sort_keys=True))
        self.assertTrue(NESTED_OBSERVATIONS["cleanup_verified"])
        for role, actor in NESTED_OBSERVATIONS.items():
            if role == "cleanup_verified":
                continue
            for name, operation in actor["operations"].items():
                self.assertFalse(operation["allowed"], role + "/" + name)
                self.assertEqual(operation["error"]["type"], "AccessError", role + "/" + name)

    def _snapshot(self, users: Any) -> dict[str, Any]:
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT id, login, signature, action_id, company_id, partner_id, active, create_uid, write_uid, create_date, write_date, "
            "to_jsonb(res_users)->'notification_type', to_jsonb(res_users)->'property_warehouse_id' "
            "FROM res_users WHERE id=ANY(%s) ORDER BY id",
            (users.ids,),
        )
        user_rows = self.env.cr.fetchall()
        self.env.cr.execute(
            "SELECT id, name, email, phone, lang, tz, company_id, active, create_uid, write_uid, create_date, write_date, street "
            "FROM res_partner WHERE id=ANY(%s) ORDER BY id",
            (users.partner_id.ids,),
        )
        partner_rows = self.env.cr.fetchall()
        self.env.cr.execute("SELECT uid, gid FROM res_groups_users_rel WHERE uid=ANY(%s) ORDER BY uid, gid", (users.ids,))
        group_rows = self.env.cr.fetchall()
        self.env.cr.execute(
            "SELECT user_id, cid FROM res_company_users_rel WHERE user_id=ANY(%s) ORDER BY user_id, cid", (users.ids,)
        )
        return {
            "users": user_rows,
            "partners": partner_rows,
            "groups": group_rows,
            "companies": self.env.cr.fetchall(),
        }

    def _observe_write(self, target: Any, values: dict[str, Any], users: Any) -> dict[str, Any]:
        self.assertFalse(target.env.su)
        self.assertNotEqual(target.env.uid, SUPERUSER_ID)
        before = self._snapshot(users)
        observation: dict[str, Any] = {
            "uid": target.env.uid,
            "su": target.env.su,
            "target_model": target._name,
            "target_ids": target.ids,
            "fields": sorted(values),
            "allowed": False,
            "error": None,
            "flushed_change": False,
        }
        try:
            with self.env.cr.savepoint():
                observation["allowed"] = bool(target.write(values.copy()))
                after = self._snapshot(users)
                observation["flushed_change"] = after != before
                observation["company_membership_unchanged"] = after["companies"] == before["companies"]
                observation["groups_unchanged"] = after["groups"] == before["groups"]
                user_rows = {row[0]: row for row in after["users"]}
                partner_rows = {row[0]: row for row in after["partners"]}
                for identifier in target.ids:
                    for field, value in values.items():
                        if field == "company_id":
                            continue
                        if target._name == "res.partner":
                            actual = partner_rows[identifier][1]
                        elif field == "name":
                            actual = partner_rows[user_rows[identifier][5]][1]
                        elif field == "street":
                            actual = partner_rows[user_rows[identifier][5]][-1]
                        elif field == "notification_type":
                            actual = user_rows[identifier][11]
                        elif field == "property_warehouse_id":
                            actual = user_rows[identifier][12].get(str(target.env.company.id))
                        else:
                            actual = user_rows[identifier][{"login": 1, "signature": 2, "action_id": 3}[field]]
                        self.assertEqual(actual, value)
                if "company_id" in values:
                    observation["company_changed"] = [row[4] for row in after["users"]] != [row[4] for row in before["users"]]
                raise RevertOperation
        except UserError as error:
            observation["allowed"] = False
            observation["error"] = {"type": type(error).__name__, "message": str(error)}
        except RevertOperation:
            pass
        self.assertEqual(self._snapshot(users), before)
        observation["rollback_verified"] = True
        return observation

    def _observe_marker_write(self, target: Any, users: Any) -> dict[str, Any]:
        field = target._fields["login"]
        with patch.object(field, "user_writeable", True, create=True):
            result = self._observe_write(target, {"login": "profile-marker-login"}, users)
            result["marker_enabled"] = field.user_writeable
        return result

    def _observe_profile_operations(self) -> None:
        user_model = self.env["res.users"].with_context(no_reset_password=True)
        group_field = "group_ids" if "group_ids" in user_model._fields else "groups_id"
        created = user_model.create(
            [
                {
                    "name": "Profile " + role,
                    "login": "profile-" + role,
                    group_field: [Command.set(self.env.ref(group).ids)],
                    "company_id": self.env.company.id,
                    "company_ids": [Command.set(self.env.company.ids)],
                }
                for role, group in (
                    ("internal", "base.group_user"),
                    ("portal", "base.group_portal"),
                    ("other", "base.group_user"),
                )
            ]
        )
        internal, portal, other = created
        public = self.env.ref("base.public_user")
        users = created | public
        unavailable_company = self.env["res.company"].create({"name": "Profile unavailable company"})
        action = self.env["ir.actions.act_window"].create(
            {"name": "Profile home action", "res_model": "res.partner", "view_mode": "list,form"}
        )
        warehouse = None
        if "property_warehouse_id" in user_model._fields:
            warehouse = self.env["stock.warehouse"].create(
                {"name": "Profile warehouse", "code": "PFILE", "company_id": self.env.company.id}
            )
        PROFILE_OBSERVATIONS.clear()
        if hasattr(user_model, "_self_accessible_fields"):
            self_writeable = user_model._self_accessible_fields()[1]
            field_source = "_self_accessible_fields"
        else:
            self_writeable = {name for name, field in user_model._fields.items() if getattr(field, "user_writeable", False)}
            field_source = "user_writeable"
        PROFILE_OBSERVATIONS.update(
            self_writeable_fields=sorted(self_writeable),
            self_writeable_source=field_source,
            self_sudo_fields=sorted(user_model.SELF_WRITEABLE_FIELDS),
            roles={},
        )
        for role, actor in (("internal", internal), ("portal", portal), ("public", public)):
            membership = {
                alias: actor.has_group(alias)
                for alias in (
                    "base.group_user",
                    "base.group_portal",
                    "base.group_public",
                    "base.group_partner_manager",
                    "base.group_erp_manager",
                    "base.group_system",
                )
            }
            self.assertTrue(membership["base.group_" + ("user" if role == "internal" else role)])
            self.assertFalse(
                any(membership[alias] for alias in ("base.group_partner_manager", "base.group_erp_manager", "base.group_system"))
            )
            self.assertNotIn(unavailable_company.id, actor.company_ids.ids)
            own = actor.with_user(actor).with_context(allowed_company_ids=actor.company_ids.ids)
            cases = {
                "own_name": (own, {"name": "Profile name witness"}),
                "own_signature": (own, {"signature": "<p>Profile signature witness</p>"}),
                "own_action": (own, {"action_id": action.id}),
                "own_login": (own, {"login": "profile-unsafe-login"}),
                "own_mixed": (own, {"name": "Profile mixed witness", "login": "profile-mixed-login"}),
                "own_delegated_mixed": (own, {"name": "Profile delegated witness", "street": "Profile street witness"}),
                "other_name": (other.with_env(own.env), {"name": "Profile other witness"}),
                "other_action": (other.with_env(own.env), {"action_id": action.id}),
                "multiple_name": ((actor | other).with_env(own.env), {"name": "Profile multiple witness"}),
                "unavailable_company": (own, {"company_id": unavailable_company.id}),
                "name_unavailable_company": (own, {"name": "Profile company witness", "company_id": unavailable_company.id}),
                "direct_own_contact": (actor.partner_id.with_env(own.env), {"name": "Profile direct own witness"}),
                "direct_other_contact": (other.partner_id.with_env(own.env), {"name": "Profile direct other witness"}),
            }
            if "notification_type" in user_model._fields:
                cases["own_mail_preference"] = (own, {"name": "Profile mail preference witness", "notification_type": "email"})
            if warehouse:
                cases["own_warehouse_preference"] = (
                    own,
                    {"name": "Profile warehouse preference witness", "property_warehouse_id": warehouse.id},
                )
            observations = {name: self._observe_write(target, values, users) for name, (target, values) in cases.items()}
            observations["own_marker_only"] = self._observe_marker_write(own, users)
            PROFILE_OBSERVATIONS["roles"][role] = {
                "actor_id": actor.id,
                "partner_id": actor.partner_id.id,
                "other_id": other.id,
                "company_ids": actor.company_ids.ids,
                "membership": membership,
                "effective_group_ids": sorted(actor._get_group_ids()),
                "partner_model_write": own.env["res.partner"].has_access("write"),
                "operations": observations,
            }
