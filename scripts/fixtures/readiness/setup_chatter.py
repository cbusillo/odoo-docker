from odoo import Command
from odoo.api import Environment


def setup_chatter(env: Environment) -> None:
    users = (
        env["res.users"]
        .with_context(no_reset_password=True)
        .create(
            [
                {
                    "name": f"Chatter fixture {name}",
                    "login": f"fixture-{name}",
                    "password": "isolated-chatter-fixture",
                    "chatter_position": position,
                    "sidebar_type": sidebar_type,
                    "group_ids": [Command.set(env.ref("base.group_user").ids)],
                }
                for name, position, sidebar_type in (
                    ("bottom", "bottom", "large"),
                    ("side", "side", "large"),
                    ("invisible", "side", "invisible"),
                    ("read-only", "bottom", "small"),
                )
            ],
        )
    )
    partner = (
        env["res.partner"]
        .with_context(mail_create_nosubscribe=True, mail_activity_quick_update=True)
        .create(
            {"name": "Chatter geometry fixture"},
        )
    )
    partner.activity_schedule(
        "mail.mail_activity_data_todo", user_id=users[0].id
    ).action_feedback("Completed fixture activity")
    partner.activity_schedule(
        "mail.mail_activity_data_todo",
        summary="Pending fixture activity",
        user_id=users[0].id,
    )
    for number in range(16):
        partner.message_post(
            body=f"Chatter fixture note {number}", subtype_xmlid="mail.mt_note"
        )
    env.cr.commit()


setup_chatter(globals()["env"])
