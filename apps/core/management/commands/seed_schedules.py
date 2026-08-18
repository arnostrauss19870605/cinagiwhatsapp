"""Create the recurring background jobs. Safe to run repeatedly.

    python manage.py seed_schedules
"""

from django.core.management.base import BaseCommand
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask


class Command(BaseCommand):
    help = "Create or update the platform's scheduled background jobs."

    def handle(self, *args, **options):
        every_minute, _ = IntervalSchedule.objects.get_or_create(
            every=1, period=IntervalSchedule.MINUTES
        )
        nightly, _ = CrontabSchedule.objects.get_or_create(
            minute="15", hour="2", day_of_week="*", day_of_month="*", month_of_year="*"
        )

        PeriodicTask.objects.update_or_create(
            name="Give waiting chats to an available agent",
            defaults={
                "interval": every_minute,
                "task": "apps.channels_wa.tasks.sweep_unassigned",
                "queue": "default",
            },
        )
        PeriodicTask.objects.update_or_create(
            name="Refresh approved WhatsApp templates",
            defaults={
                "crontab": nightly,
                "task": "apps.channels_wa.tasks.sync_templates",
                "queue": "default",
            },
        )
        self.stdout.write(self.style.SUCCESS("Scheduled jobs are set up."))
