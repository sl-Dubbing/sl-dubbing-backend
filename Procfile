web: gunicorn server:app --bind 0.0.0.0:$PORT --workers 4 --threads 2 --timeout 120
worker: celery -A tasks.app worker --loglevel=info --concurrency=2
