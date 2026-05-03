# tasks.py — V6.2 (User-Controlled LipSync Phase)
import os
import requests
import logging
from celery import Celery
from datetime import datetime

celery_app = Celery('sl_dubbing', broker=os.environ.get('REDIS_URL'), backend=os.environ.get('REDIS_URL'))

def _build_url(file_key):
    import boto3
    s3 = boto3.client('s3', endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
                      aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
                      aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'))
    return s3.generate_presigned_url('get_object', Params={'Bucket': os.environ.get('R2_BUCKET_NAME'), 'Key': file_key}, ExpiresIn=7200)

@celery_app.task(bind=True, max_retries=2)
def process_dub(self, payload):
    from app import app, db
    from models import DubbingJob
    
    job_id = payload.get('job_id')
    with_lipsync = payload.get('with_lipsync', False) # ✅ استقبال خيار المستخدم
    
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        try:
            job.status = 'processing'
            db.session.commit()
            
            media_url = _build_url(job.file_key)
            
            # --- المرحلة 1: الدبلجة الصوتية (دائماً تعمل) ---
            engine = 'f5tts' if payload.get('voice_id') == 'source' else payload.get('engine', 'xtts')
            
            r = requests.post(f"{os.environ.get('MODAL_DUBBING_URL')}/upload-from-url", 
                              json={'media_url': media_url, 'lang': payload.get('lang'), 
                                    'voice_id': payload.get('voice_id'), 'engine': engine}, timeout=1500)
            
            dub_data = r.json()
            if not dub_data.get('success'): raise Exception(dub_data.get('error'))
            
            final_url = dub_data.get('audio_url')

            # --- المرحلة 2: مزامنة الشفاه (تعمل فقط إذا اختار المستخدم) ---
            if with_lipsync and os.environ.get('MODAL_LIPSYNC_URL'):
                ls_r = requests.post(f"{os.environ.get('MODAL_LIPSYNC_URL')}/dub-video", 
                                     json={'media_url': media_url, 'dubbed_audio_url': final_url, 
                                           'auto_lipsync': True, 'preserve_background': True}, timeout=1800)
                ls_data = ls_r.json()
                if ls_data.get('success'):
                    final_url = ls_data.get('output_url')

            job.status = 'completed'
            job.output_url = final_url
            job.completed_at = datetime.utcnow()
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error_message = str(e)
            db.session.commit()
