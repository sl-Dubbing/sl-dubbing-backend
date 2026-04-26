# tasks.py — V3.1 (Smart Routing + Cloudflare R2 Integration)
import os
import time
import logging
import tempfile
import uuid
import requests
import boto3
from botocore.client import Config
from celery import Celery
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
celery_app = Celery('sl_dubbing_tasks', broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    task_time_limit=1800,
    task_soft_time_limit=1700,
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
)

from models import db, User, DubbingJob, CreditTransaction
from flask import Flask

flask_app = Flask('sl_dubbing_worker')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(flask_app)

# إعداد Cloudflare R2 Client
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME', 'sl-dubbing-media')
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)

MODAL_DUBBING_URL = os.environ.get("MODAL_DUBBING_URL", "")
MODAL_TTS_FAST_URL = os.environ.get("MODAL_TTS_FAST_URL", "")
MODAL_TTS_CLONED_URL = os.environ.get("MODAL_TTS_CLONED_URL", "")

def _refund_and_fail(job, error_msg):
    try:
        logger.error(f"Job {job.id} failed: {error_msg}")
        u = User.query.get(job.user_id)
        if u and job.credits_used:
            u.credits = (u.credits or 0) + job.credits_used
            db.session.add(CreditTransaction(
                user_id=u.id,
                transaction_type='refund',
                amount=job.credits_used,
                reason=f'Processing failed: {str(error_msg)[:150]}',
            ))
        job.status = 'failed'
        db.session.commit()
    except Exception as e:
        logger.error(f"Refund failed: {e}")
        db.session.rollback()

def _safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"Could not remove {path}: {e}")

@celery_app.task(name='tasks.process_dub', bind=True, max_retries=1)
def process_dub(self, payload):
    job_id = payload.get('job_id')
    file_key = payload.get('file_key')
    logger.info(f"[{job_id}] Dubbing Worker started, fetching from R2...")

    temp_path = None

    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            return {"status": "error", "error": "Job not found"}

        start_ts = time.time()
        try:
            if not file_key:
                raise Exception("Media file key not found in payload")

            # 1️⃣ تنزيل الملف من Cloudflare R2 إلى Worker
            temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.tmp")
            s3_client.download_file(R2_BUCKET_NAME, file_key, temp_path)

            # 2️⃣ إرسال الملف إلى Modal
            url = f"{MODAL_DUBBING_URL.rstrip('/')}/upload"
            with open(temp_path, 'rb') as f:
                files = {'media_file': f}
                data = {
                    'lang': payload.get('lang', 'en'),
                    'voice_id': payload.get('voice_id', 'source'),
                    'sample_b64': payload.get('sample_b64', ''),
                    'edge_voice': payload.get('edge_voice', ''),
                }
                response = requests.post(url, files=files, data=data, timeout=1800)

            if response.status_code != 200:
                raise Exception(f"Modal HTTP {response.status_code}: {response.text[:300]}")

            result_data = response.json()
            if not result_data.get("success"):
                raise Exception(result_data.get('error', 'Unknown Dubbing Error'))

            job.output_url = result_data.get("audio_url")
            job.extra_data = result_data.get("translated_text", "")
            job.processing_time = round(time.time() - start_ts, 2)
            job.status = 'completed'
            db.session.commit()

            # 3️⃣ حذف الملف من التخزين السحابي لتوفير المساحة
            try:
                s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=file_key)
            except Exception as e:
                logger.warning(f"Could not delete {file_key} from R2: {e}")

            logger.info(f"[{job_id}] ✅ Completed in {job.processing_time}s")
            return {"status": "done", "job_id": job_id, "audio_url": job.output_url}

        except Exception as e:
            _refund_and_fail(job, str(e))
            return {"status": "error", "job_id": job_id, "error": str(e)}
        finally:
            _safe_remove(temp_path)

@celery_app.task(name='tasks.process_smart_tts', bind=True, max_retries=1)
def process_smart_tts(self, payload):
    job_id = payload.get('job_id')
    logger.info(f"[{job_id}] TTS Worker started")

    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            return {"status": "error", "error": "Job not found"}

        start_ts = time.time()
        try:
            voice_id = payload.get('voice_id', '')
            sample_b64 = payload.get('sample_b64', '')
            needs_cloning = bool(sample_b64) or (voice_id and voice_id not in ("source", "original", ""))

            if needs_cloning:
                tts_url = f"{MODAL_TTS_CLONED_URL.rstrip('/')}/tts"
            else:
                tts_url = f"{MODAL_TTS_FAST_URL.rstrip('/')}/tts"

            body = {
                'text': payload.get('text', ''),
                'lang': payload.get('lang', 'en'),
                'voice_id': voice_id,
                'sample_b64': sample_b64,
                'edge_voice': payload.get('edge_voice', ''),
                'translate': payload.get('translate', True),
                'rate': payload.get('rate', '+0%'),
                'pitch': payload.get('pitch', '+0Hz'),
            }

            timeout = 60 if not needs_cloning else 600
            response = requests.post(tts_url, json=body, timeout=timeout)

            if response.status_code != 200:
                raise Exception(f"Modal HTTP {response.status_code}: {response.text[:300]}")

            result_data = response.json()
            if not result_data.get("success"):
                raise Exception(result_data.get('error', 'Unknown TTS Error'))

            job.output_url = result_data.get("audio_url")
            job.extra_data = result_data.get("final_text", "")
            job.processing_time = round(time.time() - start_ts, 2)
            job.status = 'completed'
            db.session.commit()

            return {"status": "done", "job_id": job_id, "audio_url": job.output_url}

        except Exception as e:
            _refund_and_fail(job, str(e))
            return {"status": "error", "job_id": job_id, "error": str(e)}
