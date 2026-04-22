import os
import time
import logging
import requests
from celery import Celery
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# إعداد Celery
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
celery_app = Celery('sl_dubbing_tasks', broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    task_time_limit=1800, # رفعنا الوقت ليتناسب مع مقاطع الفيديو الطويلة
    task_soft_time_limit=1700,
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
)

# إعداد Flask وقاعدة البيانات للـ Worker
from models import db, User, DubbingJob, CreditTransaction
from flask import Flask

flask_app = Flask('sl_dubbing_worker')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(flask_app)

MODAL_URL = os.environ.get("MODAL_URL", "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/")
if not MODAL_URL.endswith('/'):
    MODAL_URL += '/'

def _refund_and_fail(job, error_msg):
    """إعادة الرصيد للمستخدم وتحديث حالة المهمة للفشل"""
    try:
        logger.error(f"Job {job.id} failed: {error_msg}")
        u = User.query.get(job.user_id)
        if u and job.credits_used:
            u.credits += job.credits_used
            db.session.add(CreditTransaction(user_id=u.id, transaction_type='refund', amount=job.credits_used, reason='Processing failed'))
        job.status = 'failed'
        db.session.commit()
    except Exception as e:
        logger.error(f"Refund failed: {e}")
        db.session.rollback()

# 🎙️ المسار الأول: الدبلجة
@celery_app.task(name='tasks.process_dub', bind=True, max_retries=1)
def process_dub(self, payload):
    job_id = payload.get('job_id')
    logger.info(f"[{job_id}] Dubbing Worker started")

    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return {"status": "error", "error": "Job not found"}
        
        try:
            # نرسل الطلب للمصنع (الذي سيرفع النتيجة لـ Google Cloud مباشرة)
            response = requests.post(MODAL_URL, json=payload, timeout=1800)
            
            if response.status_code != 200: 
                raise Exception(f"Modal returned HTTP {response.status_code}")
            
            result_data = response.json()
            if not result_data.get("success"): 
                raise Exception(result_data.get('error', 'Unknown Factory Error'))

            # استلام الرابط المباشر من Google Cloud
            job.output_url = result_data.get("audio_url")
            job.extra_data = result_data.get("translated_text", "")
            job.status = 'completed'
            db.session.commit()

            return {"status": "done", "job_id": job_id, "audio_url": job.output_url}

        except Exception as e:
            _refund_and_fail(job, str(e))
            return {"status": "error", "job_id": job_id, "error": str(e)}

# 🌍 المسار الثاني: تحويل النص لصوت (TTS)
@celery_app.task(name='tasks.process_smart_tts', bind=True, max_retries=1)
def process_smart_tts(self, payload):
    job_id = payload.get('job_id')
    logger.info(f"[{job_id}] TTS Worker started")

    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return {"status": "error", "error": "Job not found"}

        try:
            tts_url = MODAL_URL + "tts"
            response = requests.post(tts_url, json=payload, timeout=1800)

            if response.status_code != 200: 
                raise Exception(f"Modal returned HTTP {response.status_code}")
            
            result_data = response.json()
            if not result_data.get("success"): 
                raise Exception(result_data.get('error', 'Unknown TTS Error'))

            # استلام الرابط من Google Cloud وحفظ النص النهائي في قاعدة البيانات
            job.output_url = result_data.get("audio_url")
            job.extra_data = result_data.get("final_text", "")
            job.status = 'completed'
            db.session.commit()

            return {"status": "done", "job_id": job_id, "audio_url": job.output_url}

        except Exception as e:
            _refund_and_fail(job, str(e))
            return {"status": "error", "job_id": job_id, "error": str(e)}
