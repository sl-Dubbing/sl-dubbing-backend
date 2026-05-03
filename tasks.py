# tasks.py — V5.0 (متوافق مع app.py + models.py)
import os
import logging
from datetime import datetime
from celery import Celery
import requests

logger = logging.getLogger("sl-tasks")

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


def _build_presigned_url(file_key, expires=7200):
    """يولّد رابط مؤقت من R2"""
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
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': file_key},
        ExpiresIn=expires
    )


# ==========================================
# 🎬 process_dub — للدبلجة
# ==========================================
@celery_app.task(bind=True, max_retries=2)
def process_dub(self, *args, **kwargs):
    from app import app, db
    from models import DubbingJob

    payload = args[0] if args and isinstance(args[0], dict) else kwargs
    job_id = payload.get('job_id')
    file_key = payload.get('file_key')
    media_url = payload.get('media_url')
    lang = payload.get('lang', 'ar')
    voice_id = payload.get('voice_id', 'source')
    sample_b64 = payload.get('sample_b64', '')
    engine = payload.get('engine', '')

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        try:
            job.status = 'processing'
            db.session.commit()

            # توليد URL إذا لم يصلنا
            if not media_url and (file_key or job.file_key):
                media_url = _build_presigned_url(file_key or job.file_key)

            if not media_url:
                raise Exception("No media_url or file_key available")

            modal_payload = {
                'media_url': media_url,
                'lang': lang,
                'voice_id': voice_id,
                'sample_b64': sample_b64,
                'engine': engine,
            }

            logger.info(f"[job={job_id}] → Modal {MODAL_DUBBING_URL}/upload-from-url")

            r = requests.post(
                f"{MODAL_DUBBING_URL}/upload-from-url",
                json=modal_payload,
                timeout=1500
            )

            if r.status_code != 200:
                raise Exception(f"Modal HTTP {r.status_code}: {r.text[:300]}")

            data = r.json()
            if not data.get('success'):
                raise Exception(data.get('error', 'Modal returned success=false'))

            # ✅ نجح — حفظ بأسماء الحقول الصحيحة
            job.status = 'completed'
            job.output_url = data.get('audio_url')
            job.engine = data.get('engine_used', engine or 'auto')
            job.completed_at = datetime.utcnow()
            db.session.commit()

            logger.info(f"[job={job_id}] ✅ done")

        except Exception as e:
            logger.exception(f"[job={job_id}] ❌ failed: {e}")
            try:
                job.status = 'failed'
                job.error_message = str(e)[:500]
                db.session.commit()
            except Exception:
                db.session.rollback()
            try:
                self.retry(exc=e, countdown=10)
            except Exception:
                pass


# ==========================================
# 🎙️ process_smart_tts — للـ TTS الذكي
# ==========================================
@celery_app.task(bind=True, max_retries=2)
def process_smart_tts(self, *args, **kwargs):
    from app import app, db
    from models import DubbingJob

    payload = args[0] if args and isinstance(args[0], dict) else kwargs
    job_id = payload.get('job_id')
    text = payload.get('text', '')
    lang = payload.get('lang', 'ar')
    sample_b64 = payload.get('sample_b64', '')
    voice_id = payload.get('voice_id', '')
    rate = payload.get('rate', '+0%')
    pitch = payload.get('pitch', '+0Hz')

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            logger.error(f"TTS Job {job_id} not found")
            return

        try:
            job.status = 'processing'
            db.session.commit()

            endpoint = '/cloned' if (sample_b64 or voice_id) else '/fast'
            modal_payload = {
                'text': text, 'lang': lang,
                'sample_b64': sample_b64, 'voice_id': voice_id,
                'rate': rate, 'pitch': pitch,
            }

            logger.info(f"[tts={job_id}] → Modal {MODAL_TTS_URL}{endpoint}")

            r = requests.post(f"{MODAL_TTS_URL}{endpoint}", json=modal_payload, timeout=600)
            if r.status_code != 200:
                raise Exception(f"Modal HTTP {r.status_code}: {r.text[:300]}")

            data = r.json()
            if not data.get('success'):
                raise Exception(data.get('error', 'Modal returned success=false'))

            job.status = 'completed'
            job.output_url = data.get('audio_url')
            job.completed_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"[tts={job_id}] ✅ done")

        except Exception as e:
            logger.exception(f"[tts={job_id}] ❌ failed: {e}")
            try:
                job.status = 'failed'
                job.error_message = str(e)[:500]
                db.session.commit()
            except Exception:
                db.session.rollback()
            try:
                self.retry(exc=e, countdown=10)
            except Exception:
                pass


# ==========================================
# 🎙️ process_stt — تحويل الصوت لنص
# ==========================================
@celery_app.task(bind=True, max_retries=2)
def process_stt(self, *args, **kwargs):
    from app import app, db
    from models import DubbingJob

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
        if not job:
            logger.error(f"STT Job {job_id} not found")
            return

        try:
            job.status = 'processing'
            db.session.commit()

            if not media_url and (file_key or job.file_key):
                media_url = _build_presigned_url(file_key or job.file_key)

            if not media_url:
                raise Exception("No media_url or file_key")

            base_url = MODAL_STT_PRECISE_URL if mode == 'precise' else MODAL_STT_URL
            endpoint = '/transcribe-precise' if mode == 'precise' else '/transcribe-from-url'

            modal_payload = {
                'media_url': media_url,
                'language': language if language != 'auto' else None,
                'translate': translate,
                'diarize': diarize,
                'format': 'all',
            }

            logger.info(f"[stt={job_id}] → Modal {base_url}{endpoint}")

            r = requests.post(f"{base_url}{endpoint}", json=modal_payload, timeout=900)
            if r.status_code != 200:
                raise Exception(f"Modal HTTP {r.status_code}: {r.text[:300]}")

            data = r.json()
            if not data.get('success'):
                raise Exception(data.get('error', 'Unknown'))

            job.status = 'completed'
            job.output_url = data.get('json_url') or data.get('text_url')
            job.engine = data.get('engine', mode)
            job.completed_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"[stt={job_id}] ✅ done")

        except Exception as e:
            logger.exception(f"[stt={job_id}] ❌ failed: {e}")
            try:
                job.status = 'failed'
                job.error_message = str(e)[:500]
                db.session.commit()
            except Exception:
                db.session.rollback()
            try:
                self.retry(exc=e, countdown=10)
            except Exception:
                pass


# Backward compat alias
process_tts = process_smart_tts
