# tasks.py (احتياطي - Celery worker)
import os
import time
import logging
from pathlib import Path
from celery import Celery
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)
DEBUG = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
celery_app = Celery('sl_dubbing_tasks', broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    task_time_limit=600,
    task_soft_time_limit=480,
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    result_expires=3600,
)

# Cloudinary optional import
try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_AVAILABLE = True
    CLOUDINARY_NAME = os.getenv('CLOUDINARY_NAME')
    CLOUDINARY_API_KEY = os.getenv('CLOUDINARY_API_KEY')
    CLOUDINARY_API_SECRET = os.getenv('CLOUDINARY_API_SECRET')
    if CLOUDINARY_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
        cloudinary.config(cloud_name=CLOUDINARY_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET, secure=True)
    else:
        CLOUDINARY_AVAILABLE = False
        logger.warning("Cloudinary credentials missing; will fallback to local storage.")
except Exception:
    CLOUDINARY_AVAILABLE = False
    logger.warning("Cloudinary library not installed; uploads will fallback to local storage.")

from tts_backend import synthesize_text
from models import db, User, DubbingJob, CreditTransaction
from flask import Flask

flask_app = Flask('sl_dubbing_worker')
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(flask_app)

@celery_app.task(name='tasks.process_tts', bind=True, max_retries=2, default_retry_delay=60)
def process_tts(self, payload):
    job_id = payload.get('job_id')
    user_id = payload.get('user_id')
    logger.info(f"[{job_id}] Worker started for user {user_id}")
    with flask_app.app_context():
        try:
            job = DubbingJob.query.get(job_id)
            user = User.query.get(user_id)
            if not job or not user:
                raise ValueError("Job or user not found")
            text = (payload.get('text') or '').strip()
            srt = (payload.get('srt') or '').strip()
            if not text and srt:
                text = srt
            mp_path = synthesize_text(text=text, lang=payload.get('lang','ar'),
                                      voice_mode=payload.get('voice_mode','xtts'),
                                      voice_id=payload.get('voice_id',''),
                                      voice_url=payload.get('voice_url',''))
            # upload or move local
            if CLOUDINARY_AVAILABLE:
                resp = cloudinary.uploader.upload(mp_path, resource_type='auto', folder='sl-dubbing/audio', public_id=f"tts_{job_id}", overwrite=True)
                audio_url = resp.get('secure_url') or resp.get('url')
            else:
                dest = Path('/tmp/sl_audio') / f"dub_{job_id}.mp3"
                Path(mp_path).rename(dest)
                audio_url = f"file://{dest}"
            job.output_url = audio_url
            job.status = 'completed'
            db.session.add(job)
            db.session.commit()
            return {"status":"done","job_id":job_id,"audio_url":audio_url}
        except Exception as e:
            logger.exception("Worker failed")
            # handle refund etc.
            return {"status":"error","job_id":job_id,"error":str(e)}
