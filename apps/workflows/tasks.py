from celery import shared_task


@shared_task(name="apps.workflows.tasks.expire_waiting_runs", ignore_result=True)
def expire_waiting_runs():
    """Questions nobody answered in time: follow their timeout path, or end the run."""
    from .engine import expire_waiting

    return expire_waiting()
