# tasks.py — V2.0 (Celery + Modal integration + integrated text correction)
import os
import time
import logging
import requests
from celery import Celery
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ==========================================
# ⚙️ إعداد Celery
# ==========================================
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
celery_app = Celery('sl_dubbing_tasks', broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    task_time_limit=1800,
    task_soft_time_limit=1700,
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
)

# ==========================================
# 🗄️ إعداد Flask لاستخدام قاعدة البيانات داخل الـ Worker
# ==========================================
from models import db, User, DubbingJob, CreditTransaction
from flask import Flask

flask_app = Flask('sl_dubbing_worker')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(flask_app)

# ==========================================
# 🌐 روابط خدمات Modal
# ==========================================
MODAL_DUBBING_URL = os.environ.get(
    "MODAL_DUBBING_URL",
    "https://your_workspace--sl-dubbing-factory-fastapi-app.modal.run",
)
MODAL_TTS_URL = os.environ.get(
    "MODAL_TTS_URL",
    "https://your_workspace--sl-tts-factory-fastapi-app.modal.run",
)


# ==========================================
# 🔤 مصحح النصوص المدمج (كان text_corrector.py سابقاً)
# ==========================================
def _call_openai_with_retries(payload, headers, max_attempts=3):
    """استدعاء OpenAI مع backoff exponential"""
    backoff = 1
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            )
            if resp.status_code == 429:
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt == max_attempts:
                raise
            time.sleep(backoff)
            backoff *= 2
    if last_exc:
        raise last_exc


def smart_correct_text(raw_text: str, api_key: str = None) -> str:
    """تصحيح الأخطاء الإملائية والنحوية في النص العربي."""
    if not raw_text or not raw_text.strip():
        return raw_text

    openai_key = api_key or os.getenv("OPENAI_API_KEY")
    if not openai_key:
        logger.info("No OPENAI_API_KEY — skipping correction.")
        return raw_text

    system_prompt = (
        "أنت مدقق لغوي ومترجم محترف في استوديو دبلجة. سأعطيك نصاً مترجماً. "
        "مهمتك: 1) صحح الأخطاء الإملائية والنحوية فقط. "
        "2) استبدل الكلمات الأجنبية المكتوبة بحروف عربية بأصلها الإنجليزي. "
        "3) لا تغيّر المعنى ولا تضف شيئاً. "
        "4) أعد النص المصحَّح فقط بدون أي شرح."
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {openai_key}",
    }
    payload = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_text},
        ],
        "temperature": 0.1,
    }

    try:
        resp = _call_openai_with_retries(payload, headers)
        response_data = resp.json()
        choices = response_data.get('choices') or []
        if choices:
            message = choices[0].get('message') or {}
            corrected = (message.get('content') or '').strip()
            return corrected if corrected else raw_text
        logger.warning(f"OpenAI returned no choices: {response_data}")
        return raw_text
    except Exception as e:
        logger.error(f"Text correction failed: {e}")
        return raw_text


# ==========================================
# 🛠️ دوال مساعدة
# ==========================================
def _refund_and_fail(job, error_msg):
    """إعادة الرصيد للمستخدم وتحديث حالة المهمة للفشل"""
    try:
        logger.error(f"Job {job.id} failed: {error_msg}")
        u = User.query.get(job.user_id)
        if u and job.credits_used:
            u.credits = (u.credits or 0) + job.credits_used
            db.session.add(CreditTransaction(
                user_id=u.id,
                transaction_type='refund',
                amount=job.credits_used,
                reason=f'Processing failed: {str(error_msg)[:150]}',
            ))
        job.status = 'failed'
        db.session.commit()
    except Exception as e:
        logger.error(f"Refund failed: {e}")
        db.session.rollback()


def _safe_remove(path: str):
    """حذف ملف مع تجاهل الأخطاء"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"Could not remove {path}: {e}")


# ==========================================
# 🎙️ المسار الأول: الدبلجة (ملف فيديو/صوت)
# ==========================================
@celery_app.task(name='tasks.process_dub', bind=True, max_retries=1)
def process_dub(self, payload):
    job_id = payload.get('job_id')
    file_path = payload.get('file_path')
    logger.info(f"[{job_id}] Dubbing Worker started")

    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            _safe_remove(file_path)
            return {"status": "error", "error": "Job not found"}

        start_ts = time.time()

        try:
            if not file_path or not os.path.exists(file_path):
                raise Exception("Media file not found on disk")

            url = f"{MODAL_DUBBING_URL.rstrip('/')}/upload"

            # ⚡ إرسال الطلب كـ multipart form-data
            with open(file_path, 'rb') as f:
                files = {'media_file': f}
                data = {
                    'lang': payload.get('lang', 'en'),
                    'voice_id': payload.get('voice_id', 'source'),
                    'sample_b64': payload.get('sample_b64', ''),
                }
                response = requests.post(url, files=files, data=data, timeout=1800)

            if response.status_code != 200:
                raise Exception(
                    f"Modal returned HTTP {response.status_code}: {response.text[:300]}"
                )

            result_data = response.json()
            if not result_data.get("success"):
                raise Exception(result_data.get('error', 'Unknown Dubbing Error'))

            # ✅ تصحيح النص المترجم اختيارياً
            translated_text = result_data.get("translated_text", "") or ""
            if translated_text and os.getenv("ENABLE_TEXT_CORRECTION", "0") == "1":
                translated_text = smart_correct_text(translated_text)

            job.output_url = result_data.get("audio_url")
            job.extra_data = translated_text
            job.processing_time = round(time.time() - start_ts, 2)
            job.status = 'completed'
            db.session.commit()

            logger.info(f"[{job_id}] ✅ Completed in {job.processing_time}s")
            return {
                "status": "done",
                "job_id": job_id,
                "audio_url": job.output_url,
            }

        except Exception as e:
            _refund_and_fail(job, str(e))
            return {"status": "error", "job_id": job_id, "error": str(e)}
        finally:
            # 🧹 تنظيف الملف المؤقت دائماً
            _safe_remove(file_path)


# ==========================================
# 🌍 المسار الثاني: تحويل النص إلى صوت (TTS)
# ==========================================
@celery_app.task(name='tasks.process_smart_tts', bind=True, max_retries=1)
def process_smart_tts(self, payload):
    job_id = payload.get('job_id')
    logger.info(f"[{job_id}] TTS Worker started")

    with flask_app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            return {"status": "error", "error": "Job not found"}

        start_ts = time.time()

        try:
            tts_url = f"{MODAL_TTS_URL.rstrip('/')}/tts"

            body = {
                'text': payload.get('text', ''),
                'lang': payload.get('lang', 'en'),
                'voice_id': payload.get('voice_id', ''),
            }

            response = requests.post(tts_url, json=body, timeout=1800)
            if response.status_code != 200:
                raise Exception(
                    f"Modal returned HTTP {response.status_code}: {response.text[:300]}"
                )

            result_data = response.json()
            if not result_data.get("success"):
                raise Exception(result_data.get('error', 'Unknown TTS Error'))

            final_text = result_data.get("final_text", "") or ""
            if final_text and os.getenv("ENABLE_TEXT_CORRECTION", "0") == "1":
                final_text = smart_correct_text(final_text)

            job.output_url = result_data.get("audio_url")
            job.extra_data = final_text
            job.processing_time = round(time.time() - start_ts, 2)
            job.status = 'completed'
            db.session.commit()

            logger.info(f"[{job_id}] ✅ TTS completed in {job.processing_time}s")
            return {
                "status": "done",
                "job_id": job_id,
                "audio_url": job.output_url,
            }

        except Exception as e:
            _refund_and_fail(job, str(e))
            return {"status": "error", "job_id": job_id, "error": str(e)}
