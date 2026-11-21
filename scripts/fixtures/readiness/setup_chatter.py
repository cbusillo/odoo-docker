from odoo import Command
from odoo.api import Environment


def setup_chatter(env: Environment) -> None:
    users = (
        env["res.users"]
        .with_context(no_reset_password=True)
        .create(
            [
                {
                    "name": f"Chatter fixture {position}",
                    "login": f"fixture-{position}",
                    "password": "isolated-chatter-fixture",
                    "chatter_position": position,
                    "group_ids": [Command.set(env.ref("base.group_user").ids)],
                }
                for position in ("bottom", "side")
            ],
        )
    )
    partner = env["res.partner"].create({"name": "Chatter geometry fixture"})
    partner.activity_schedule("mail.mail_activity_data_todo", user_id=users[0].id).action_feedback("Completed fixture activity")
    partner.activity_schedule(
        "mail.mail_activity_data_todo",
        summary="Pending fixture activity",
        user_id=users[0].id,
    )
    for number in range(16):
        partner.message_post(body=f"Chatter fixture note {number}", subtype_xmlid="mail.mt_note")
    env.cr.commit()


setup_chatter(globals()["env"])
