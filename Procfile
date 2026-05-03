web: gunicorn app:app --worker-class gthread --workers 2 --threads 50 --bind 0.0.0.0:$PORT --timeout 120
worker: celery -A tasks.celery_app worker --loglevel=info --concurrency=20 --pool=gevent
