web: gunicorn server:app --worker-class gthread --workers 2 --threads 25 --bind 0.0.0.0:$PORT --timeout 60
worker: celery -A tasks.celery_app worker --loglevel=info --concurrency=10 --pool=gevent
