"""Prove the Microsoft Graph credentials work, before anyone sends 600 emails.

Needs no event, no guest list and no database rows. It asks Microsoft for a
token, reads the permissions Microsoft put inside that token, and looks up the
sender's mailbox. Nothing is sent.

    python manage.py check_graph

The permission check is the useful part. An app registration can have Mail.Send
listed and still not have it, because an administrator never granted consent -
and Microsoft does not tell you that until the first send fails. The roles are
written into the token itself, so this reads them there.
"""

import base64
import json

from django.core.management.base import BaseCommand, CommandError

REQUIRED = "Mail.Send"


def _claims(token):
    """Read the payload of a JWT without verifying it.

    Not verifying is correct here: Microsoft just handed us this token over
    TLS, we are only reading it to report to a human, and we are not making
    any trust decision based on it.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


class Command(BaseCommand):
    help = "Check the Microsoft Graph credentials and permissions. Sends nothing."

    def handle(self, *args, **options):
        from apps.integrations.msgraph import GraphClient, GraphError

        client = GraphClient()
        if not client.configured:
            raise CommandError(
                "Not configured. Set MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID, "
                "MS_GRAPH_CLIENT_SECRET and MS_GRAPH_SENDER in .env."
            )

        self.stdout.write(f"tenant  {client.tenant_id}")
        self.stdout.write(f"client  {client.client_id}")
        self.stdout.write(f"sender  {client.sender}\n")

        # 1. token
        try:
            token = client._access_token()
        except GraphError as exc:
            self.stdout.write(self.style.ERROR("token   FAILED"))
            raise CommandError(exc.friendly)
        self.stdout.write(self.style.SUCCESS("token   ok"))

        # 2. permissions, read out of the token
        claims = _claims(token)
        roles = claims.get("roles") or []
        app_name = claims.get("app_displayname") or "(unnamed app)"
        self.stdout.write(f"app     {app_name}")
        if roles:
            self.stdout.write(f"grants  {', '.join(sorted(roles))}")
        else:
            self.stdout.write(self.style.ERROR("grants  none"))

        if REQUIRED not in roles:
            self.stdout.write(
                self.style.ERROR(
                    f"\n{REQUIRED} is missing. Add it as an APPLICATION permission (not "
                    "delegated) in Entra > App registrations > API permissions, then click "
                    "'Grant admin consent'. Without the consent step the permission is "
                    "listed but not granted, and sending fails at the first message."
                )
            )
            raise CommandError("Not ready to send.")
        self.stdout.write(self.style.SUCCESS(f"        {REQUIRED} granted"))

        # 3. the mailbox itself
        try:
            mailbox = client.whoami()
        except GraphError as exc:
            self.stdout.write(self.style.ERROR("mailbox FAILED"))
            raise CommandError(exc.friendly)
        self.stdout.write(
            self.style.SUCCESS(
                f"mailbox ok - {mailbox.get('displayName')} <{mailbox.get('mail') or mailbox.get('userPrincipalName')}>"
            )
        )
        self.stdout.write(self.style.SUCCESS("\nGraph is ready. Nothing was sent."))
