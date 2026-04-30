# tasks.py — V3.3 (Audited & Production Ready)
import os
import time
import logging
import tempfile
import uuid
import requests
import base64
import boto3
from botocore.client import Config
from celery import Celery
from dotenv import load_dotenv
import modal 

load_dotenv()
logger = logging.getLogger(__name__)

# إعداد Celery مع Redis
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
celery_app = Celery('sl_dubbing_tasks', broker=REDIS_URL, backend=REDIS_URL)

celery_app.conf.update(
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_time_limit=1800,
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='UTC'
)

from models import db, User, DubbingJob, CreditTransaction
from flask import Flask

# إعداد Flask Context للوصول لقاعدة البيانات من الـ Worker
flask_app = Flask('sl_dubbing_worker')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(flask_app)

# إعداد Cloudflare R2 Client
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)

MODAL_DUBBING_URL = os.environ.get("MODAL_DUBBING_URL", "")

def _refund_and_fail(job, error_msg):
    """إعادة الرصيد للمستخدم في حال فشل العملية"""
    try:
        logger.error(f"Job {job.id} failed: {error_msg}")
        u = User.query.get(job.user_id)
        if u and job.credits_used:
            u.credits = (u.credits or 0) + job.credits_used
            db.session.add(CreditTransaction(
                user_id=u.id,
                transaction_type='refund',
                amount=job.credits_used,
                reason=f'Failure: {str(error_msg)[:100]}',
            ))
        job.status = 'failed'
        db.session.commit()
    except Exception as e:
        logger.error(f"Refund/Fail update error: {e}")
        db.session.rollback()

@celery_app.task(name='tasks.process_dub', bind=True, max_retries=1)
def process_dub(self, payload):
    job_id = payload.get('job_id')
    file_key = payload.get('file_key')
    temp_path = None

    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return {"error": "Job not found"}

        start_ts = time.time()
        try:
            # 1. تنزيل الملف من R2 للمعالجة
            temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.tmp")
            s3_client.download_file(R2_BUCKET_NAME, file_key, temp_path)

            # 2. الإرسال إلى Modal Dubbing Engine
            url = f"{MODAL_DUBBING_URL.rstrip('/')}/upload"
            with open(temp_path, 'rb') as f:
                files = {'media_file': f}
                data = {
                    'lang': payload.get('lang', 'en'),
                    'voice_id': payload.get('voice_id', 'source'),
                    'sample_b64': payload.get('sample_b64', '')
                }
                response = requests.post(url, files=files, data=data, timeout=1800)

            if response.status_code != 200:
                raise Exception(f"Modal Gateway Error: {response.status_code}")

            res_json = response.json()
            if not res_json.get("success"):
                raise Exception(res_json.get('error', 'Dubbing Logic Error'))

            # 3. تحديث البيانات
            job.output_url = res_json.get("audio_url")
            job.status = 'completed'
            job.processing_time = round(time.time() - start_ts, 2)
            db.session.commit()

            # تنظيف R2 من الملف الأصلي
            s3_client.delete_object(Bucket=R2_BUCKET_NAME, Key=file_key)
            return {"status": "done", "url": job.output_url}

        except Exception as e:
            _refund_and_fail(job, str(e))
            return {"error": str(e)}
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

@celery_app.task(name='tasks.process_smart_tts', bind=True, max_retries=1)
def process_smart_tts(self, payload):
    job_id = payload.get('job_id')
    
    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return {"error": "Job not found"}

        start_ts = time.time()
        try:
            # 1. الاتصال بـ Modal باستخدام الـ SDK مباشرة (أسرع)
            tts_func = modal.Function.lookup("sl-tts-factory", "process_tts")
            
            # تحديد الجودة بناءً على المدخلات
            mode = 'quality' if payload.get('sample_b64') else 'fast'
            
            result = tts_func.remote({
                'text': payload.get('text', ''),
                'lang': payload.get('lang', 'en'),
                'mode': mode,
                'sample_b64': payload.get('sample_b64', ''),
                'rate': payload.get('rate', '+0%'),
                'pitch': payload.get('pitch', '+0Hz')
            })

            if not result or not result.get("success"):
                raise Exception(result.get('error', 'Modal TTS Failure'))

            # 2. فك التشفير ورفع النتيجة لـ R2
            audio_data = base64.b64decode(result["audio_base64"])
            out_key = f"results/{job_id}.mp3"
            
            s3_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=out_key,
                Body=audio_data,
                ContentType='audio/mpeg'
            )

            # 3. بناء الرابط النهائي (هام: R2_PUBLIC_URL)
            public_base = os.environ.get('R2_PUBLIC_URL', '').rstrip('/')
            if not public_base: raise Exception("R2_PUBLIC_URL variable missing")
            
            job.output_url = f"{public_base}/{out_key}"
            job.status = 'completed'
            job.processing_time = round(time.time() - start_ts, 2)
            db.session.commit()

            return {"status": "done", "url": job.output_url}

        except Exception as e:
            _refund_and_fail(job, str(e))
            return {"error": str(e)}
