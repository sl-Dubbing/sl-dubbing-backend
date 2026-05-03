# tasks.py — V5.1 (محسّن: متوافق مع app.py + models.py)
import os
import logging
from datetime import datetime
from celery import Celery
from celery.exceptions import MaxRetriesExceededError
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("sl-tasks")
logger.setLevel(logging.INFO)

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
    """يولّد رابط مؤقت من R2 (presigned GET)."""
    import boto3
    from botocore.client import Config

    endpoint = os.environ.get('R2_ENDPOINT_URL')
    access_key = os.environ.get('R2_ACCESS_KEY_ID')
    secret_key = os.environ.get('R2_SECRET_ACCESS_KEY')
    bucket = os.environ.get('R2_BUCKET_NAME')

    if not bucket:
        raise RuntimeError("R2_BUCKET_NAME not configured")

    s3 = boto3.client(
        's3',
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': file_key},
        ExpiresIn=expires
    )


def _make_session(retries=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504)):
    """إنشاء requests.Session مع سياسة إعادة المحاولة."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS'])
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def _post_json(session, url, payload, timeout):
    """POST JSON مع فحص الحالة وتحويل JSON آمن."""
    r = session.post(url, json=payload, timeout=timeout)
    try:
        r.raise_for_status()
    except Exception as e:
        # سجل نص الاستجابة للمساعدة في التشخيص
        logger.debug(f"HTTP error from {url}: status={r.status_code} text={r.text[:1000]}")
        raise
    try:
        return r.json()
    except ValueError:
        raise Exception(f"Invalid JSON response from {url}: {r.text[:1000]}")


# ==========================================
# 🎬 process_dub — للدبلجة
# ==========================================
@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
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

    if not MODAL_DUBBING_URL:
        logger.error("MODAL_DUBBING_URL not configured")
        return

    session = _make_session(retries=3, backoff_factor=1)

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            logger.error(f"[job={job_id}] not found")
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

            url = f"{MODAL_DUBBING_URL.rstrip('/')}/upload-from-url"
            logger.info(f"[job={job_id}] → Modal {url}")

            data = _post_json(session, url, modal_payload, timeout=1500)

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
            logger.exception(f"[job={job_id}] ❌ error: {e}")
            # تأكد من تنظيف جلسة DB قبل إعادة المحاولة
            try:
                db.session.rollback()
            except Exception:
                pass

            # حاول إعادة المحاولة؛ إذا استنفدت المحاولات، علم المهمة كـ failed
            try:
                raise self.retry(exc=e)
            except MaxRetriesExceededError:
                logger.error(f"[job={job_id}] max retries exceeded, marking failed")
                try:
                    job = DubbingJob.query.get(job_id)
                    if job:
                        job.status = 'failed'
                        job.error_message = str(e)[:500]
                        job.completed_at = datetime.utcnow()
                        db.session.commit()
                except Exception:
                    db.session.rollback()
                return


# ==========================================
# 🎙️ process_smart_tts — للـ TTS الذكي
# ==========================================
@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
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

    if not MODAL_TTS_URL:
        logger.error("MODAL_TTS_URL not configured")
        return

    session = _make_session(retries=3, backoff_factor=1)

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            logger.error(f"[tts={job_id}] not found")
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

            url = f"{MODAL_TTS_URL.rstrip('/')}{endpoint}"
            logger.info(f"[tts={job_id}] → Modal {url}")

            data = _post_json(session, url, modal_payload, timeout=600)

            if not data.get('success'):
                raise Exception(data.get('error', 'Modal returned success=false'))

            job.status = 'completed'
            job.output_url = data.get('audio_url')
            job.completed_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"[tts={job_id}] ✅ done")

        except Exception as e:
            logger.exception(f"[tts={job_id}] ❌ error: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass
            try:
                raise self.retry(exc=e)
            except MaxRetriesExceededError:
                logger.error(f"[tts={job_id}] max retries exceeded, marking failed")
                try:
                    job = DubbingJob.query.get(job_id)
                    if job:
                        job.status = 'failed'
                        job.error_message = str(e)[:500]
                        job.completed_at = datetime.utcnow()
                        db.session.commit()
                except Exception:
                    db.session.rollback()
                return


# ==========================================
# 🎙️ process_stt — تحويل الصوت لنص
# ==========================================
@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
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

    if not MODAL_STT_URL:
        logger.error("MODAL_STT_URL not configured")
        return

    session = _make_session(retries=3, backoff_factor=1)

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            logger.error(f"[stt={job_id}] not found")
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

            url = f"{base_url.rstrip('/')}{endpoint}"
            logger.info(f"[stt={job_id}] → Modal {url}")

            data = _post_json(session, url, modal_payload, timeout=900)

            if not data.get('success'):
                raise Exception(data.get('error', 'Unknown'))

            job.status = 'completed'
            job.output_url = data.get('json_url') or data.get('text_url') or data.get('audio_url')
            job.engine = data.get('engine', mode)
            job.completed_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"[stt={job_id}] ✅ done")

        except Exception as e:
            logger.exception(f"[stt={job_id}] ❌ error: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass
            try:
                raise self.retry(exc=e)
            except MaxRetriesExceededError:
                logger.error(f"[stt={job_id}] max retries exceeded, marking failed")
                try:
                    job = DubbingJob.query.get(job_id)
                    if job:
                        job.status = 'failed'
                        job.error_message = str(e)[:500]
                        job.completed_at = datetime.utcnow()
                        db.session.commit()
                except Exception:
                    db.session.rollback()
                return


# Backward compat alias
process_tts = process_smart_tts
