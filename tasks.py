# tasks.py — V3.4 Fully Synced
import os
import logging
from datetime import datetime
from celery import Celery
import requests

logger = logging.getLogger("sl-dubbing-tasks")

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
celery_app = Celery('sl_dubbing', broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    task_track_started=True,
    task_time_limit=1800,  
    task_soft_time_limit=1500,
)

MODAL_DUBBING_URL = os.environ.get('MODAL_DUBBING_URL')
MODAL_TTS_URL = os.environ.get('MODAL_TTS_URL')
MODAL_STT_URL = os.environ.get('MODAL_STT_URL')
MODAL_STT_PRECISE_URL = os.environ.get('MODAL_STT_PRECISE_URL', MODAL_STT_URL)

@celery_app.task(bind=True, max_retries=2)
def process_dub(self, payload_or_job_id, file_key=None, lang=None, voice_id=None, sample_b64=None):
    from app import app, db
    from models import DubbingJob, User
    
    # دعم التمرير عبر Dictionary (كما يفعل app.py الجديد) أو المتغيرات المباشرة (كما كان قديماً)
    if isinstance(payload_or_job_id, dict):
        job_id = payload_or_job_id.get('job_id')
        file_key = payload_or_job_id.get('file_key')
        lang = payload_or_job_id.get('lang')
        voice_id = payload_or_job_id.get('voice_id')
        sample_b64 = payload_or_job_id.get('sample_b64')
    else:
        job_id = payload_or_job_id

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return
        try:
            job.status = 'processing'
            db.session.commit()

            import boto3
            from botocore.client import Config
            s3 = boto3.client('s3', 
                              endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
                              aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
                              aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
                              config=Config(signature_version='s3v4'), 
                              region_name='auto')
            R2_BUCKET = os.environ.get('R2_BUCKET_NAME')
            
            # إذا لم يتم تمرير media_url، نقوم بتوليده
            media_url = s3.generate_presigned_url('get_object', Params={'Bucket': R2_BUCKET, 'Key': file_key}, ExpiresIn=7200) if file_key else None

            payload = {
                'media_url': media_url, 'lang': lang, 'voice_id': voice_id,
                'sample_b64': sample_b64, 'engine': 'auto',
            }
            r = requests.post(f"{MODAL_DUBBING_URL}/upload-from-url", json=payload, timeout=1500)
            if r.status_code != 200: raise Exception(f"Modal Error: {r.text[:200]}")
            
            data = r.json()
            if not data.get('success'): raise Exception(data.get('error', 'Unknown error'))

            job.status = 'completed'
            job.output_url = data.get('audio_url')
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error_message = str(e)[:500]
            db.session.commit()
            try: self.retry(exc=e, countdown=10)
            except Exception: pass

# ✅ تم تغيير الاسم من process_smart_tts إلى process_tts ليتوافق مع السيرفر
@celery_app.task(bind=True, max_retries=2)
def process_tts(self, payload):
    from app import app, db
    from models import DubbingJob, User
    
    job_id = payload.get('job_id')
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return
        try:
            job.status = 'processing'
            db.session.commit()

            sample_b64 = payload.get('sample_b64')
            voice_id = payload.get('voice_id')
            endpoint = '/cloned' if (sample_b64 or voice_id) else '/fast'
            
            r = requests.post(f"{MODAL_TTS_URL}{endpoint}", json=payload, timeout=600)
            if r.status_code != 200: raise Exception(f"Modal Error {r.status_code}")
            
            data = r.json()
            if not data.get('success'): raise Exception(data.get('error'))

            job.status = 'completed'
            job.output_url = data.get('audio_url')
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error_message = str(e)[:500]
            db.session.commit()
            try: self.retry(exc=e, countdown=10)
            except Exception: pass

@celery_app.task(bind=True, max_retries=2)
def process_stt(self, job_id, media_url, language, mode, diarize, translate):
    from app import app, db
    from models import DubbingJob, User
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return
        try:
            job.status = 'processing'
            db.session.commit()

            base_url = MODAL_STT_PRECISE_URL if mode == 'precise' else MODAL_STT_URL
            endpoint = '/transcribe-precise' if mode == 'precise' else '/transcribe-from-url'

            payload = {
                'media_url': media_url, 'language': language,
                'translate': translate, 'diarize': diarize, 'format': 'all',
            }
            r = requests.post(f"{base_url}{endpoint}", json=payload, timeout=900)
            if r.status_code != 200: raise Exception(f"Modal Error {r.status_code}")
            
            data = r.json()
            if not data.get('success'): raise Exception(data.get('error', 'Unknown'))

            job.status = 'completed'
            job.output_url = data.get('json_url') or data.get('text_url')
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error_message = str(e)[:500]
            db.session.commit()
            try: self.retry(exc=e, countdown=10)
            except Exception: pass
