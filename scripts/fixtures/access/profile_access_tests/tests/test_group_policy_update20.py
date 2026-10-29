from __future__ import annotations

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase
from odoo.tools.convert import convert_file


@tagged("post_install", "-at_install")
class TestGroupPolicyUpdate(TransactionCase):
    def test_own_field_read_exception_does_not_authorize_other_users(self) -> None:
        users = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                [
                    {"name": name, "login": name, "group_ids": [Command.set(self.env.ref("base.group_portal").ids)]}
                    for name in ("field-scope-actor", "field-scope-other")
                ]
            )
        )
        actor, other = users.with_user(users[0])
        user_model = actor.browse()
        for name in ("outgoing_mail_server_id", "outgoing_mail_server_type"):
            field = actor._fields[name]
            self.assertTrue(actor.has_field_access(field, "read"))
            actor.check_field_access(field, "read")
            actor[name]
            self.assertFalse(other.has_field_access(field, "read"))
            self.assertFalse(user_model.has_field_access(field, "read"))
            with self.assertRaises(AccessError):
                other.check_field_access(field, "read")
            with self.assertRaises(AccessError):
                other[name]
        self.assertFalse(actor.env.su)

    def test_base_group_update_removes_only_owned_obsolete_implications(self) -> None:
        manager = self.env.ref("base.group_partner_manager")
        internal = self.env.ref("base.group_user")
        access_manager = self.env.ref("base.group_erp_manager")
        system = self.env.ref("base.group_system")
        custom_child, custom_access_child, custom_parent = self.env["res.groups"].create(
            [{"name": name} for name in ("Custom contact child", "Custom access child", "Custom contact parent")]
        )
        manager.write({"implied_ids": [Command.link(internal.id), Command.link(custom_child.id)]})
        access_manager.write({"implied_ids": [Command.link(manager.id), Command.link(custom_access_child.id)]})
        custom_parent.write({"implied_ids": [Command.link(manager.id)]})
        actor = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create({"name": "Contact update actor", "login": "contact-update-actor", "group_ids": [Command.set(manager.ids)]})
        )
        assigned_groups = actor.group_ids.ids
        self.assertIn(internal, actor.all_group_ids)
        self.assertIn(manager, access_manager.implied_ids)
        first_edges = None
        for _ in range(2):
            convert_file(self.env, "base", "security/base_groups.xml", {}, "update", noupdate=False)
            self.env.flush_all()
            self.env.invalidate_all()
            self.assertNotIn(internal, manager.implied_ids)
            self.assertNotIn(manager, access_manager.implied_ids)
            self.assertIn(manager, system.implied_ids)
            self.assertIn(custom_child, manager.implied_ids)
            self.assertIn(custom_access_child, access_manager.implied_ids)
            self.assertIn(manager, custom_parent.implied_ids)
            self.assertEqual(actor.group_ids.ids, assigned_groups)
            self.assertNotIn(internal, actor.all_group_ids)
            self.assertIn(custom_child, actor.all_group_ids)
            edges = {group.id: sorted(group.implied_ids.ids) for group in manager | access_manager | system | custom_parent}
            if first_edges is None:
                first_edges = edges
            else:
                self.assertEqual(edges, first_edges)
