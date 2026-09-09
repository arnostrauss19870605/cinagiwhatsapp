from django.db import migrations
from django.db.models import F, Q


def fix_statuses(apps, schema_editor):
    """Chats opened by a campaign used to be marked 'with an agent' with no agent.

    Nobody has replied: back to automation, which is who actually opened them.
    Somebody has replied and nobody picked it up: into the queue.
    """
    Conversation = apps.get_model("inbox", "Conversation")
    orphaned = Conversation.objects.filter(status="assigned", assigned_to__isnull=True)
    orphaned.filter(Q(last_inbound_at__isnull=True) | Q(last_inbound_at__lt=F("last_outbound_at"))).update(status="bot")
    orphaned.update(status="queued")


class Migration(migrations.Migration):
    dependencies = [("inbox", "0003_bulk_send")]
    operations = [migrations.RunPython(fix_statuses, migrations.RunPython.noop)]
