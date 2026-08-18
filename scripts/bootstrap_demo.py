"""Create a first login, workspace and a couple of quick replies.

    python manage.py shell < scripts/bootstrap_demo.py

Safe to run more than once.
"""

from django.contrib.auth import get_user_model

from apps.library.models import QuickSnippet
from apps.workspaces.models import BusinessHours, Workspace, WorkspaceMembership

User = get_user_model()

user, created = User.objects.get_or_create(
    username="admin",
    defaults={"email": "admin@example.com", "is_staff": True, "is_superuser": True},
)
if created:
    user.set_password("changeme123")
    user.save()
    print("created login: admin / changeme123  (change it immediately)")

workspace, _ = Workspace.objects.get_or_create(
    slug="cinagi-support",
    defaults={"name": "Cinagi Support", "description": "Main support line"},
)
WorkspaceMembership.objects.get_or_create(
    user=user, workspace=workspace, defaults={"role": WorkspaceMembership.Role.OWNER}
)
for weekday in range(7):
    BusinessHours.objects.get_or_create(
        workspace=workspace, weekday=weekday, defaults={"is_closed": weekday >= 5}
    )

for title, shortcut, body in [
    ("Office hours", "hours", "We are open Monday to Friday, 08:00 to 17:00."),
    ("Send documents", "docs", "Hi {{contact.first_name}}, please send a clear photo of the document here in the chat."),
    ("We are looking into it", "wait", "Thanks {{contact.first_name}} - we are checking and will come back to you shortly."),
]:
    QuickSnippet.objects.get_or_create(
        workspace=workspace, title=title, defaults={"shortcut": shortcut, "body": body}
    )

print("workspace ready:", workspace.name)
