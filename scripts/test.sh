#!/usr/bin/env sh
# Fast local test run: sqlite, no Redis, no real network.
export DB_ENGINE=sqlite
export CELERY_TASK_ALWAYS_EAGER=True
export CHANNELS_IN_MEMORY=True
export OUTBOUND_COMMS_MODE=suppress
python manage.py test tests "$@"
