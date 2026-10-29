from __future__ import annotations

from typing import Any

from odoo import SUPERUSER_ID, Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

PROFILE_OBSERVATIONS: dict[str, Any] = {}


class RevertOperation(Exception):
    pass


@tagged("post_install", "-at_install")
class TestOwnProfile(TransactionCase):
    def _snapshot(self, users: Any) -> dict[str, Any]:
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT id, login, signature, action_id, company_id, partner_id, active, create_uid, write_uid, create_date, write_date "
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

    def test_profile_and_direct_contact_operations(self) -> None:
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
        PROFILE_OBSERVATIONS.clear()
        if hasattr(user_model, "_self_accessible_fields"):
            self_writeable = user_model._self_accessible_fields()[1]
            field_source = "_self_accessible_fields"
        else:
            self_writeable = {name for name, field in user_model._fields.items() if getattr(field, "user_writeable", False)}
            field_source = "user_writeable"
        PROFILE_OBSERVATIONS.update(self_writeable_fields=sorted(self_writeable), self_writeable_source=field_source, roles={})
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
            observations = {name: self._observe_write(target, values, users) for name, (target, values) in cases.items()}
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
