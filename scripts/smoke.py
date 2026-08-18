"""Render every page against a throwaway database.

    DB_ENGINE=sqlite CHANNELS_IN_MEMORY=True python scripts/smoke.py

Nothing touches the real database and no message can leave the building.
"""

import django, os
os.environ.setdefault("DJANGO_SETTINGS_MODULE","config.settings")
django.setup()
from django.test.utils import setup_test_environment, teardown_test_environment
from django.test.runner import DiscoverRunner
setup_test_environment()
runner = DiscoverRunner(verbosity=0)
old = runner.setup_databases()

from django.test import Client
from django.contrib.auth import get_user_model
from apps.workspaces.models import Workspace, WorkspaceMembership, BusinessHours
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Contact
from apps.inbox.models import Conversation, Message
from apps.library.models import QuickSnippet, MessageTemplate
from django.utils import timezone
import datetime as dt

U = get_user_model()
u = U.objects.create_user("arno","arno@example.com","pw12345678", is_staff=True)
ws = Workspace.objects.create(name="Cinagi Support", description="Main line")
WorkspaceMembership.objects.create(user=u, workspace=ws, role="owner")
for d in range(7): BusinessHours.objects.create(workspace=ws, weekday=d, is_closed=d>=5)
ch = WhatsAppChannel.objects.create(workspace=ws, display_name="Claims", phone_number="+27110000000",
                                    phone_number_id="123", waba_id="456", access_token="EAAtok", app_secret="sec",
                                    status="connected")
c = Contact.objects.create(workspace=ws, wa_id="27820000001", profile_name="Thandi")
conv = Conversation.objects.create(workspace=ws, channel=ch, contact=c,
    last_inbound_at=timezone.now(), window_expires_at=timezone.now()+dt.timedelta(hours=24))
Message.objects.create(workspace=ws, conversation=conv, direction="in", body="Hi, I need help with a claim")
Message.objects.create(workspace=ws, conversation=conv, direction="out", actor="agent", author=u, body="Sure, happy to help", wa_status="read")
QuickSnippet.objects.create(workspace=ws, title="Office hours", shortcut="hours", body="We are open 08:00-17:00.")
MessageTemplate.objects.create(workspace=ws, channel=ch, name="claim_update", language="en_US", status="APPROVED",
    components=[{"type":"BODY","text":"Hi {{1}}, your claim {{2}} was received."}])

# closed-window conversation
c2 = Contact.objects.create(workspace=ws, wa_id="27820000002", profile_name="Sipho")
conv2 = Conversation.objects.create(workspace=ws, channel=ch, contact=c2,
    last_inbound_at=timezone.now()-dt.timedelta(hours=30), window_expires_at=timezone.now()-dt.timedelta(hours=6))
Message.objects.create(workspace=ws, conversation=conv2, direction="in", body="Old question")

cl = Client(); cl.force_login(u)
pages = [
 ("dashboard","/"),("inbox","/inbox/"),("conversation",f"/inbox/{conv.pk}/"),
 ("closed-window conversation",f"/inbox/{conv2.pk}/"),
 ("thread fragment",f"/inbox/{conv.pk}/thread/"),("list fragment","/inbox/?fragment=list&view=open"),
 ("snippet search","/inbox/snippets/?q=hours"),
 ("numbers","/wa/numbers/"),("connect","/wa/numbers/connect/"),("verify",f"/wa/numbers/{ch.pk}/check/"),
 ("edit channel",f"/wa/numbers/{ch.pk}/edit/"),
 ("templates","/library/templates/"),("snippets","/library/replies/"),("new snippet","/library/replies/new/"),
 ("hours","/workspaces/hours/"),("team","/workspaces/team/"),("settings","/workspaces/settings/"),
 ("workspace list","/workspaces/"),("new workspace","/workspaces/new/"),
 ("my availability","/agents/me/"),("team availability","/agents/availability/"),
 ("send template form",f"/inbox/{conv2.pk}/send-template/?template_id={MessageTemplate.objects.first().pk}"),
 ("healthz","/healthz"),
]
fails=0
for name,url in pages:
    r = cl.get(url, HTTP_HOST="testserver")
    ok = r.status_code in (200,204)
    if not ok: fails+=1
    print(f"{'OK ' if ok else 'FAIL'} {r.status_code} {name:28s} {url}")
print("FAILURES:", fails)
runner.teardown_databases(old); teardown_test_environment()
