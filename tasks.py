# tasks.py — Universal V4.0 (Bulletproof Version)
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
def process_dub(self, *args, **kwargs):
    # 1. الاستيراد الصحيح من app.py
    from app import app, db
    from models import DubbingJob, User

    # 2. مستخرج المتغيرات الذكي (يقبل جميع الطرق)
    payload = args[0] if args and isinstance(args[0], dict) else kwargs
    
    job_id = payload.get('job_id')
    file_key = payload.get('file_key')
    media_url = payload.get('media_url')
    lang = payload.get('lang', 'ar')
    voice_id = payload.get('voice_id', 'source')
    sample_b64 = payload.get('sample_b64', '')
    engine = payload.get('engine', 'auto')

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return
        try:
            job.status = 'processing'
            db.session.commit()

            # توليد الرابط إذا تم إرسال file_key فقط
            if not media_url and file_key:
                import boto3
                from botocore.client import Config
                s3 = boto3.client('s3', 
                                  endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
                                  aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
                                  aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
                                  config=Config(signature_version='s3v4'), 
                                  region_name='auto')
                R2_BUCKET = os.environ.get('R2_BUCKET_NAME')
                media_url = s3.generate_presigned_url('get_object', Params={'Bucket': R2_BUCKET, 'Key': file_key}, ExpiresIn=7200)

            modal_payload = {
                'media_url': media_url, 'lang': lang, 'voice_id': voice_id,
                'sample_b64': sample_b64, 'engine': engine,
            }
            r = requests.post(f"{MODAL_DUBBING_URL}/upload-from-url", json=modal_payload, timeout=1500)
            if r.status_code != 200: raise Exception(f"Modal Error: {r.text[:200]}")
            
            data = r.json()
            if not data.get('success'): raise Exception(data.get('error', 'Unknown error'))

            job.status = 'completed'
            # تخزين الرابط في قاعدة البيانات
            job.audio_url = data.get('audio_url')
            if hasattr(job, 'output_url'): job.output_url = data.get('audio_url') # احتياط للتوافق
            
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error = str(e)[:500]
            if hasattr(job, 'error_message'): job.error_message = str(e)[:500]
            db.session.commit()
            try: self.retry(exc=e, countdown=10)
            except Exception: pass

@celery_app.task(bind=True, max_retries=2)
def process_tts(self, *args, **kwargs):
    from app import app, db
    from models import DubbingJob, User

    payload = args[0] if args and isinstance(args[0], dict) else kwargs
    job_id = payload.get('job_id')
    text = payload.get('text')
    lang = payload.get('lang', 'ar')
    sample_b64 = payload.get('sample_b64', '')
    voice_id = payload.get('voice_id', '')
    rate = payload.get('rate', '+0%')
    pitch = payload.get('pitch', '+0Hz')

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return
        try:
            job.status = 'processing'
            db.session.commit()

            endpoint = '/cloned' if (sample_b64 or voice_id) else '/fast'
            modal_payload = {
                'text': text, 'lang': lang, 'sample_b64': sample_b64, 
                'voice_id': voice_id, 'rate': rate, 'pitch': pitch,
            }
            r = requests.post(f"{MODAL_TTS_URL}{endpoint}", json=modal_payload, timeout=600)
            if r.status_code != 200: raise Exception(f"Modal Error {r.status_code}")
            
            data = r.json()
            if not data.get('success'): raise Exception(data.get('error'))

            job.status = 'completed'
            job.audio_url = data.get('audio_url')
            if hasattr(job, 'output_url'): job.output_url = data.get('audio_url')
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error = str(e)[:500]
            if hasattr(job, 'error_message'): job.error_message = str(e)[:500]
            db.session.commit()
            try: self.retry(exc=e, countdown=10)
            except Exception: pass

@celery_app.task(bind=True, max_retries=2)
def process_stt(self, *args, **kwargs):
    from app import app, db
    from models import DubbingJob, User

    payload = args[0] if args and isinstance(args[0], dict) else kwargs
    job_id = payload.get('job_id')
    media_url = payload.get('media_url')
    file_key = payload.get('file_key')
    language = payload.get('language', 'auto')
    mode = payload.get('mode', 'fast')
    diarize = payload.get('diarize', False)
    translate = payload.get('translate', False)

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return
        try:
            job.status = 'processing'
            db.session.commit()

            if not media_url and file_key:
                import boto3
                from botocore.client import Config
                s3 = boto3.client('s3', 
                                  endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
                                  aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
                                  aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
                                  config=Config(signature_version='s3v4'), 
                                  region_name='auto')
                R2_BUCKET = os.environ.get('R2_BUCKET_NAME')
                media_url = s3.generate_presigned_url('get_object', Params={'Bucket': R2_BUCKET, 'Key': file_key}, ExpiresIn=7200)

            base_url = MODAL_STT_PRECISE_URL if mode == 'precise' else MODAL_STT_URL
            endpoint = '/transcribe-precise' if mode == 'precise' else '/transcribe-from-url'

            modal_payload = {
                'media_url': media_url, 'language': language,
                'translate': translate, 'diarize': diarize, 'format': 'all',
            }
            r = requests.post(f"{base_url}{endpoint}", json=modal_payload, timeout=900)
            if r.status_code != 200: raise Exception(f"Modal Error {r.status_code}")
            
            data = r.json()
            if not data.get('success'): raise Exception(data.get('error', 'Unknown'))

            job.status = 'completed'
            job.audio_url = data.get('json_url') or data.get('text_url')
            if hasattr(job, 'output_url'): job.output_url = data.get('json_url') or data.get('text_url')
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error = str(e)[:500]
            if hasattr(job, 'error_message'): job.error_message = str(e)[:500]
            db.session.commit()
            try: self.retry(exc=e, countdown=10)
            except Exception: pass
