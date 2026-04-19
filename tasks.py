# tasks.py
import os
import time
import logging
import base64
import requests
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

try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_NAME = os.getenv('CLOUDINARY_NAME')
    CLOUDINARY_API_KEY = os.getenv('CLOUDINARY_API_KEY')
    CLOUDINARY_API_SECRET = os.getenv('CLOUDINARY_API_SECRET')
    if CLOUDINARY_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
        cloudinary.config(cloud_name=CLOUDINARY_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET, secure=True)
        CLOUDINARY_AVAILABLE = True
    else:
        CLOUDINARY_AVAILABLE = False
        logger.warning("Cloudinary credentials missing; will fallback to local storage.")
except Exception:
    CLOUDINARY_AVAILABLE = False
    logger.warning("Cloudinary library not installed; uploads will fallback to local storage.")

from models import db, User, DubbingJob, CreditTransaction
from flask import Flask

flask_app = Flask('sl_dubbing_worker')
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(flask_app)

AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

def cloudinary_upload_with_retries(local_path, public_id, folder="sl-dubbing/audio", max_attempts=3):
    attempt = 0
    last_exc = None
    while attempt < max_attempts:
        try:
            resp = cloudinary.uploader.upload(local_path, resource_type='auto', folder=folder, public_id=public_id, overwrite=True, use_filename=False)
            return resp
        except Exception as e:
            last_exc = e
            attempt += 1
            time.sleep(2 ** attempt)
    raise last_exc

@celery_app.task(name='tasks.process_tts', bind=True, max_retries=2, default_retry_delay=60)
def process_tts(self, payload):
    job_id = payload.get('job_id')
    user_id = payload.get('user_id')
    logger.info(f"[{job_id}] Worker started for user {user_id}")

    with flask_app.app_context():
        job = None
        try:
            job = DubbingJob.query.get(job_id)
            user = User.query.get(user_id)
            if not job or not user:
                raise ValueError("Job or user not found")

            file_path = payload.get('file_path')
            if not file_path or not os.path.exists(file_path):
                raise ValueError("لم يتم العثور على الملف المرفوع.")

            logger.info(f"[{job_id}] Encoding file and sending to Modal GPU Factory via Celery...")

            with open(file_path, "rb") as f:
                file_b64 = base64.b64encode(f.read()).decode('utf-8')

            MODAL_URL = os.environ.get("MODAL_URL") or "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/"

            response = requests.post(MODAL_URL, json={
                "file_b64": file_b64,
                "filename": payload.get('filename'),
                "lang": payload.get('lang', 'ar'),
                "voice_mode": payload.get('voice_mode', 'xtts'),
                "voice_url": payload.get('voice_url', ''),
                "openai_key": os.environ.get("OPENAI_API_KEY", "")
            }, timeout=600)

            logger.debug(f"[{job_id}] Modal status: {response.status_code}")
            logger.debug(f"[{job_id}] Modal text: {response.text}")

            if response.status_code != 200:
                raise Exception(f"Modal returned status {response.status_code}: {response.text}")

            result_data = response.json()
            if not result_data.get("success"):
                raise Exception(f"خطأ في المصنع: {result_data.get('error')}")

            audio_base64 = result_data.get("audio_base64")
            audio_bytes = base64.b64decode(audio_base64)

            mp_path = AUDIO_DIR / f"dub_{job_id}.mp3"
            with open(mp_path, "wb") as f:
                f.write(audio_bytes)

            if CLOUDINARY_AVAILABLE:
                upload_resp = cloudinary_upload_with_retries(str(mp_path), public_id=f"dub_{job_id}")
                audio_url = upload_resp.get('secure_url') or upload_resp.get('url')
            else:
                PUBLIC_HOST = os.environ.get("PUBLIC_HOST")
                if PUBLIC_HOST:
                    audio_url = f"https://{PUBLIC_HOST}/api/file/dub_{job_id}.mp3"
                else:
                    audio_url = str(mp_path)

            job.output_url = audio_url
            job.status = 'completed'
            job.method = payload.get('voice_mode', 'xtts')
            db.session.add(job)
            db.session.commit()

            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                if os.path.exists(mp_path):
                    os.remove(mp_path)
            except Exception as e:
                logger.warning(f"Cleanup failed: {e}")

            return {"status":"done", "job_id":job_id, "audio_url":audio_url}

        except Exception as e:
            logger.exception("Worker failed")
            try:
                if job:
                    job.status = 'failed'
                    db.session.add(job)
                if job and job.credits_used:
                    u = User.query.get(job.user_id)
                    if u:
                        u.credits += job.credits_used
                        db.session.add(CreditTransaction(user_id=u.id, transaction_type='refund', amount=job.credits_used, reason='Dubbing failed'))
                db.session.commit()
            except Exception as refund_exc:
                logger.error(f"Refund failed: {refund_exc}")
                db.session.rollback()

            return {"status":"error", "job_id":job_id, "error":str(e)}
