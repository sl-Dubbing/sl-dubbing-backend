web: gunicorn server:app --bind 0.0.0.0:$PORT --workers 4 --threads 2 --timeout 120
worker: celery -A server.celery_app worker --loglevel=info
