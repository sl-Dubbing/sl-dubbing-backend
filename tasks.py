# tasks.py — V6.5 (UUID Compatible + Sync Fix)
import os
import logging
from datetime import datetime
from celery import Celery
import requests

logger = logging.getLogger("sl-tasks")

# تأكد من ربط Redis بشكل صحيح من متغيرات Railway
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
celery_app = Celery('sl_dubbing', broker=REDIS_URL, backend=REDIS_URL)

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    task_track_started=True,
    task_time_limit=2500, 
    task_soft_time_limit=2400,
)

# جلب الروابط من متغيرات البيئة
MODAL_DUBBING_URL = os.environ.get('MODAL_DUBBING_URL')
MODAL_LIPSYNC_URL = os.environ.get('MODAL_LIPSYNC_URL')

def _build_presigned_url(file_key, expires=7200):
    import boto3
    from botocore.client import Config
    s3 = boto3.client(
        's3',
        endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
        aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )
    bucket = os.environ.get('R2_BUCKET_NAME')
    return s3.generate_presigned_url('get_object', Params={'Bucket': bucket, 'Key': file_key}, ExpiresIn=expires)

@celery_app.task(bind=True, max_retries=2)
def process_dub(self, *args, **kwargs):
    from app import app, db
    from models import DubbingJob

    payload = args[0] if args and isinstance(args[0], dict) else kwargs
    job_id = payload.get('job_id')
    file_key = payload.get('file_key')
    media_url = payload.get('media_url')
    lang = payload.get('lang', 'ar')
    
    with_lipsync = payload.get('with_lipsync', False)
    video_output = payload.get('video_output', True)
    
    with app.app_context():
        # البحث عن المهمة (المعرف الآن نصي UUID)
        job = DubbingJob.query.get(job_id)
        if not job: 
            logger.error(f"Job {job_id} not found in database.")
            return

        try:
            job.status = 'processing'
            db.session.commit()

            if not media_url and (file_key or job.file_key):
                media_url = _build_presigned_url(file_key or job.file_key)

            if not media_url: raise Exception("No media_url available")

            # المرحلة 1: الدبلجة الصوتية
            modal_payload = {
                'media_url': media_url, 'lang': lang,
                'voice_id': payload.get('voice_id', 'source'),
                'sample_b64': payload.get('sample_b64', ''),
                'engine': payload.get('engine', 'f5tts'),
            }
            
            r = requests.post(f"{MODAL_DUBBING_URL}/upload-from-url", json=modal_payload, timeout=1500)
            if r.status_code != 200: raise Exception(f"Dubbing HTTP {r.status_code}")
            data = r.json()
            
            dubbed_audio_url = data.get('audio_url')
            final_output_url = dubbed_audio_url

            # المرحلة 2: معالجة الفيديو (LipSync)
            if video_output and MODAL_LIPSYNC_URL:
                lipsync_payload = {
                    "media_url": media_url,
                    "dubbed_audio_url": dubbed_audio_url,
                    "preserve_background": True,
                    "auto_lipsync": False, 
                    "force_lipsync": with_lipsync 
                }
                ls_r = requests.post(f"{MODAL_LIPSYNC_URL}/dub-video", json=lipsync_payload, timeout=1800)
                if ls_r.status_code == 200:
                    ls_data = ls_r.json()
                    if ls_data.get('success'):
                        final_output_url = ls_data.get('output_url')

            job.status = 'completed'
            job.output_url = final_output_url
            job.completed_at = datetime.utcnow()
            db.session.commit()

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}")
            job.status = 'failed'
            job.error_message = str(e)[:500]
            db.session.commit()
