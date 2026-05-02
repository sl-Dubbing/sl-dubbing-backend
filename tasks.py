# tasks.py — V3.1 Direct URL pass-through to Modal
import os
import logging
from datetime import datetime
from celery import Celery
import requests

logger = logging.getLogger("sl-dubbing-tasks")

# Celery setup
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
celery_app = Celery('sl_dubbing', broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    task_track_started=True,
    task_time_limit=1800,  # 30 دقيقة
    task_soft_time_limit=1500,
)

# Modal endpoints
MODAL_DUBBING_URL = os.environ.get('MODAL_DUBBING_URL')
MODAL_TTS_URL = os.environ.get('MODAL_TTS_URL')


@celery_app.task(bind=True, max_retries=2)
def process_dub(self, job_id, media_url, lang, voice_id, sample_b64, engine):
    """يرسل URL لـ Modal — لا يمر بأي ملف عبر Railway"""
    from server import app, db
    from models import DubbingJob, User

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        try:
            job.status = 'processing'
            db.session.commit()

            # 🚀 Modal يقرأ من R2 مباشرة عبر URL
            payload = {
                'media_url': media_url,  # ← الـ URL من R2
                'lang': lang,
                'voice_id': voice_id,
                'sample_b64': sample_b64,
                'engine': engine,
            }

            logger.info(f"[job={job_id}] → Modal lang={lang} engine={engine}")

            r = requests.post(
                f"{MODAL_DUBBING_URL}/upload-from-url",  # endpoint جديد
                json=payload,
                timeout=1500
            )

            if r.status_code != 200:
                raise Exception(f"Modal returned {r.status_code}: {r.text[:200]}")

            data = r.json()
            if not data.get('success'):
                raise Exception(data.get('error', 'Unknown error'))

            # نجح — حدّث الـ job
            job.status = 'completed'
            job.audio_url = data.get('audio_url')
            job.engine = data.get('engine_used', engine)
            job.completed_at = datetime.utcnow()

            # خصم رصيد
            user = User.query.get(job.user_id)
            if user and user.credits > 0:
                user.credits -= 1

            db.session.commit()
            logger.info(f"[job={job_id}] ✅ done")

        except Exception as e:
            logger.exception(f"[job={job_id}] ❌ failed")
            job.status = 'failed'
            job.error = str(e)[:500]
            db.session.commit()

            # retry
            try:
                self.retry(exc=e, countdown=10)
            except Exception:
                pass


@celery_app.task(bind=True, max_retries=2)
def process_tts(self, job_id, text, lang, sample_b64, voice_id, rate, pitch):
    """TTS — يستخدم نفس Modal TTS factory"""
    from server import app, db
    from models import DubbingJob, User

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            return

        try:
            job.status = 'processing'
            db.session.commit()

            # Smart routing: مع cloning → ClonedTTS، بدون → FastTTS
            endpoint = '/cloned' if (sample_b64 or voice_id) else '/fast'

            payload = {
                'text': text, 'lang': lang,
                'sample_b64': sample_b64, 'voice_id': voice_id,
                'rate': rate, 'pitch': pitch,
            }

            r = requests.post(f"{MODAL_TTS_URL}{endpoint}", json=payload, timeout=600)
            if r.status_code != 200:
                raise Exception(f"Modal returned {r.status_code}")

            data = r.json()
            if not data.get('success'):
                raise Exception(data.get('error'))

            job.status = 'completed'
            job.audio_url = data.get('audio_url')
            job.completed_at = datetime.utcnow()

            user = User.query.get(job.user_id)
            if user and user.credits > 0:
                user.credits -= 1

            db.session.commit()

        except Exception as e:
            logger.exception(f"[tts {job_id}] failed")
            job.status = 'failed'
            job.error = str(e)[:500]
            db.session.commit()
            try:
                self.retry(exc=e, countdown=10)
            except Exception:
                pass
