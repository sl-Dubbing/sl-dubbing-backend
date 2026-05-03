web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 4
worker: celery -A tasks:celery_app worker --loglevel=info --concurrency=20 --pool=gevent --max-tasks-per-child=100
