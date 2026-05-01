# tasks.py — V3.6 (Fixed Imports & Supabase Compatible)
import os
import time
import logging
import tempfile
import uuid
import requests
import base64
import boto3
# 🛠️ التعديل الجذري هنا: فصل الاستدعاءات لتجنب ImportError
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError 
from celery import Celery
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("sl-dubbing-tasks")
logger.setLevel(logging.INFO)

# Celery config
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
celery_app = Celery('sl_dubbing_tasks', broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_time_limit=int(os.environ.get('TASK_TIME_LIMIT', 1800)),
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='UTC'
)

from models import db, User, DubbingJob, CreditTransaction
from flask import Flask

# Flask app context for worker DB access
flask_app = Flask('sl_dubbing_worker')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(flask_app)

# R2 client
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
R2_ENDPOINT = os.environ.get('R2_ENDPOINT_URL')
s3_client = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)

MODAL_DUBBING_URL = os.environ.get("MODAL_DUBBING_URL", "").rstrip('/')
R2_PUBLIC_BASE = os.environ.get('R2_PUBLIC_BASE', '').rstrip('/')

# Helper to ensure database is ready
def _refund_and_fail(job, error_msg):
    try:
        logger.error("Refunding and failing job_id=%s user_id=%s reason=%s", job.id, job.user_id, error_msg)
        u = User.query.get(job.user_id)
        # التأكد من وجود رصيد مستخدم قبل الاسترجاع
        credits_to_refund = getattr(job, 'credits_used', 100) # الافتراضي 100 إذا لم يحدد
        if u:
            u.credits = (u.credits or 0) + credits_to_refund
            db.session.add(CreditTransaction(
                user_id=u.id,
                transaction_type='refund',
                amount=credits_to_refund,
                reason=f'Failure: {str(error_msg)[:200]}',
                job_id=job.id
            ))
        job.status = 'failed'
        db.session.commit()
    except Exception as e:
        logger.exception("Refund/Fail update error for job_id=%s: %s", getattr(job, 'id', None), e)
        db.session.rollback()

def _download_from_r2(bucket, key, dest_path, max_retries=3):
    attempt = 0
    while attempt < max_retries:
        try:
            s3_client.download_file(bucket, key, dest_path)
            return
        except (BotoCoreError, ClientError) as e:
            attempt += 1
            logger.warning("R2 download attempt %s/%s failed for key=%s: %s", attempt, max_retries, key, e)
            time.sleep(2 ** attempt)
    raise Exception(f"Failed to download {key} after {max_retries} attempts")

@celery_app.task(name='tasks.process_dub', bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=600)
def process_dub(self, payload):
    job_id = payload.get('job_id')
    file_key = payload.get('file_key')
    temp_path = None

    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            logger.error("Job not found job_id=%s", job_id)
            return {"error": "Job not found"}

        start_ts = time.time()
        try:
            temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.tmp")
            _download_from_r2(R2_BUCKET_NAME, file_key, temp_path)

            url = f"{MODAL_DUBBING_URL}/upload"
            with open(temp_path, 'rb') as f:
                files = {'media_file': f}
                data = {
                    'lang': payload.get('lang', 'en'),
                    'voice_id': payload.get('voice_id', 'source'),
                    'sample_b64': payload.get('sample_b64', '')
                }
                resp = requests.post(url, files=files, data=data, timeout=int(os.environ.get('MODAL_TIMEOUT', 1800)))
            
            if resp.status_code != 200:
                raise Exception(f"Modal Gateway Error: {resp.status_code}")

            res_json = resp.json()
            if not res_json.get("success"):
                raise Exception(res_json.get('error', 'Dubbing Logic Error'))

            job.output_url = res_json.get("audio_url")
            job.status = 'completed'
            job.processing_time = round(time.time() - start_ts, 2)
            db.session.commit()

            return {"status": "done", "url": job.output_url}

        except Exception as e:
            logger.exception("Processing failed for job_id=%s", job_id)
            _refund_and_fail(job, str(e))
            return {"error": str(e)}
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

@celery_app.task(name='tasks.process_smart_tts', bind=True, max_retries=2, autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=600)
def process_smart_tts(self, payload):
    job_id = payload.get('job_id')

    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return {"error": "Job not found"}

        start_ts = time.time()
        try:
            # التحقق من وجود Modal SDK
            import modal
            tts_func = modal.Function.lookup("sl-tts-factory", "process_tts")

            result = tts_func.remote({
                'text': payload.get('text', ''),
                'lang': payload.get('lang', 'en'),
                'mode': 'quality' if payload.get('sample_b64') else 'fast',
                'sample_b64': payload.get('sample_b64', ''),
                'rate': payload.get('rate', '+0%'),
                'pitch': payload.get('pitch', '+0Hz')
            })

            if not result or not result.get("success"):
                raise Exception(result.get('error', 'Modal TTS Failure'))

            audio_data = base64.b64decode(result["audio_base64"])
            out_key = f"results/{job_id}.mp3"

            s3_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=out_key,
                Body=audio_data,
                ContentType='audio/mpeg'
            )

            job.output_url = f"{R2_PUBLIC_BASE}/{out_key}"
            job.status = 'completed'
            job.processing_time = round(time.time() - start_ts, 2)
            db.session.commit()

            return {"status": "done", "url": job.output_url}

        except Exception as e:
            logger.exception("Smart TTS failed for job_id=%s", job_id)
            _refund_and_fail(job, str(e))
            return {"error": str(e)}
