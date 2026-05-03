# tasks.py — V3.2 Fixed Signatures
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
def process_dub(self, job_id, media_url, lang, voice_id, sample_b64, engine):
    from server import app, db
    from models import DubbingJob, User
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return
        try:
            job.status = 'processing'
            db.session.commit()

            payload = {
                'media_url': media_url, 'lang': lang, 'voice_id': voice_id,
                'sample_b64': sample_b64, 'engine': engine,
            }
            r = requests.post(f"{MODAL_DUBBING_URL}/upload-from-url", json=payload, timeout=1500)
            if r.status_code != 200: raise Exception(f"Modal Error: {r.text[:200]}")
            
            data = r.json()
            if not data.get('success'): raise Exception(data.get('error', 'Unknown error'))

            job.status = 'completed'
            job.audio_url = data.get('audio_url')
            job.engine = data.get('engine_used', engine)
            job.completed_at = datetime.utcnow()

            user = User.query.get(job.user_id)
            if user and user.credits > 0: user.credits -= 1
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error = str(e)[:500]
            db.session.commit()
            try: self.retry(exc=e, countdown=10)
            except Exception: pass

# ✅ الإصلاح هنا: جعل rate و pitch قيم اختيارية لتجنب خطأ Celery
@celery_app.task(bind=True, max_retries=2)
def process_tts(self, job_id, text, lang, sample_b64='', voice_id='', rate=1.0, pitch=1.0):
    from server import app, db
    from models import DubbingJob, User
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job: return
        try:
            job.status = 'processing'
            db.session.commit()

            endpoint = '/cloned' if (sample_b64 or voice_id) else '/fast'
            payload = {
                'text': text, 'lang': lang, 'sample_b64': sample_b64, 
                'voice_id': voice_id, 'rate': rate, 'pitch': pitch,
            }
            r = requests.post(f"{MODAL_TTS_URL}{endpoint}", json=payload, timeout=600)
            if r.status_code != 200: raise Exception(f"Modal Error {r.status_code}")
            
            data = r.json()
            if not data.get('success'): raise Exception(data.get('error'))

            job.status = 'completed'
            job.audio_url = data.get('audio_url')
            job.completed_at = datetime.utcnow()

            user = User.query.get(job.user_id)
            if user and user.credits > 0: user.credits -= 1
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error = str(e)[:500]
            db.session.commit()
            try: self.retry(exc=e, countdown=10)
            except Exception: pass

@celery_app.task(bind=True, max_retries=2)
def process_stt(self, job_id, media_url, language, mode, diarize, translate):
    from server import app, db
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
            job.audio_url = data.get('json_url') or data.get('text_url')
            job.engine = data.get('engine', mode)
            job.completed_at = datetime.utcnow()

            user = User.query.get(job.user_id)
            if user and user.credits > 0: user.credits -= 1
            db.session.commit()

        except Exception as e:
            job.status = 'failed'
            job.error = str(e)[:500]
            db.session.commit()
            try: self.retry(exc=e, countdown=10)
            except Exception: pass
