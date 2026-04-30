web: gunicorn app:app --worker-class gthread --workers 2 --threads 4 --timeout 120
worker: celery -A tasks.celery_app worker --loglevel=info --pool=gevent --concurrency=10
monitor: celery -A tasks.celery_app flower --port=$PORT
