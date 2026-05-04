# tasks.py — V5.2 (on-demand safe mode)
import os
import logging
import tempfile
import subprocess
import uuid
import time
from datetime import datetime
from celery import Celery
import requests
from requests.exceptions import RequestException

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
MODAL_LIPSYNC_URL = os.environ.get('MODAL_LIPSYNC_URL')  # Smart Video Dubber
# Prosody Transfer endpoint (ElevenLabs‑Pro style)
MODAL_PROSODY_URL = os.environ.get('MODAL_PROSODY_URL')  # Prosody Transfer
# Optional API key for Prosody service (if required)
MODAL_PROSODY_KEY = os.environ.get('MODAL_PROSODY_KEY')

# -------------------------
# On-demand guard (default: disabled)
# Set ENABLE_ON_DEMAND=1 in environment to allow processing
# -------------------------
ENABLE_ON_DEMAND = os.environ.get('ENABLE_ON_DEMAND', '0') == '1'


def _ffmpeg(args, **kwargs):
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error"] + args,
        capture_output=True, text=True,
        timeout=kwargs.get("timeout", 120),
        check=kwargs.get("check", False),
    )


def _merge_video_audio_locally(media_url, dubbed_audio_url):
    """
    🎬 دمج فيديو + صوت مدبلج محلياً (بدون Modal)
    سريع جداً (~10 ثوان لفيديو دقيقة) ولا يحتاج GPU.
    """
    import boto3
    from botocore.client import Config
    import shutil

    # تحقّق من ffmpeg
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            logger.error("❌ ffmpeg test failed")
            return None
        logger.info(f"✅ ffmpeg available")
    except FileNotFoundError:
        logger.error("❌ ffmpeg NOT INSTALLED in worker. Add aptPkgs=['ffmpeg'] to nixpacks.toml")
        return None
    except Exception as e:
        logger.error(f"❌ ffmpeg check failed: {e}")
        return None

    temp_dir = tempfile.mkdtemp()
    try:
        # 1. تحميل الفيديو الأصلي
        video_path = os.path.join(temp_dir, "video.mp4")
        with requests.get(media_url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)

        # 2. تحميل الصوت المدبلج
        audio_path = os.path.join(temp_dir, "dubbed.wav")
        with requests.get(dubbed_audio_url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(audio_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)

        # 3. دمج
        output_path = os.path.join(temp_dir, "output.mp4")
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            output_path
        ], capture_output=True, timeout=300)

        if result.returncode != 0:
            logger.error(f"ffmpeg failed: {result.stderr.decode()[:500]}")
            return None

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
            return None

        # 4. ارفع لـ R2
        s3 = boto3.client(
            's3',
            endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
            aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
            config=Config(signature_version='s3v4'),
            region_name='auto'
        )
        bucket = os.environ.get('R2_BUCKET_NAME')

        out_key = f"results/dub_video_{uuid.uuid4().hex}.mp4"
        s3.upload_file(output_path, bucket, out_key)

        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket, 'Key': out_key},
            ExpiresIn=604800
        )
        return url

    except Exception as e:
        logger.exception(f"Local merge failed: {e}")
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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
# 🎬 process_dub — للدبلجة (on-demand guard)
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
    with_lipsync = bool(payload.get('with_lipsync', False))
    video_output = bool(payload.get('video_output', True))

    with app.app_context():
        job = DubbingJob.query.get(job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return

        # -------------------------
        # On-demand guard: if not enabled, skip processing immediately
        # -------------------------
        if not ENABLE_ON_DEMAND:
            logger.info(f"[job={job_id}] On-demand mode disabled — skipping processing")
            try:
                job.status = 'pending'
                db.session.commit()
            except Exception:
                db.session.rollback()
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

            audio_url = data.get('audio_url')
            # ===========================================
            # 🎭 PROSODY TRANSFER (مستوى ElevenLabs Pro)
            # ===========================================
            prosody_audio_url = audio_url  # الافتراضي: لا تغيير

            # نفّذ نقل الـ prosody إذا كان endpoint معرفاً، ووسائط متاحة، وطلب المستخدم يسمح بذلك
            # ملاحظة: apply_prosody افتراضياً False الآن
            if MODAL_PROSODY_URL and media_url and payload.get('apply_prosody', False):
                headers = {}
                if MODAL_PROSODY_KEY:
                    headers['Authorization'] = f"Bearer {MODAL_PROSODY_KEY}"

                prosody_endpoint = f"{MODAL_PROSODY_URL.rstrip('/')}/transfer"
                attempts = int(payload.get('prosody_attempts', 1))  # default 1 attempt
                backoff = float(payload.get('prosody_backoff', 1.0))
                prosody_resp = None
                t_start = time.time()
                for attempt in range(1, attempts + 1):
                    try:
                        logger.info(f"[job={job_id}] 🎭 → Prosody Transfer attempt {attempt}/{attempts}")
                        call_t0 = time.time()
                        prosody_resp = requests.post(
                            prosody_endpoint,
                            json={
                                'source_audio_url': media_url,        # الأصلي
                                'target_audio_url': audio_url,        # المدبلج
                                'level': payload.get('prosody_level', 'pro'),  # basic|pro|max
                                'method': 'world',
                                'intensity': float(payload.get('prosody_intensity', 0.7)),
                            },
                            headers=headers,
                            timeout=600
                        )
                        call_dt = time.time() - call_t0
                        logger.info(f"[job={job_id}] 🎭 Prosody HTTP status={prosody_resp.status_code} (attempt {attempt}) took {call_dt:.1f}s")
                        if prosody_resp.status_code == 200:
                            pdata = prosody_resp.json()
                            if pdata.get('success'):
                                prosody_audio_url = pdata.get('audio_url', audio_url)
                                logger.info(
                                    f"[job={job_id}] ✅ Prosody applied: "
                                    f"emotion={pdata.get('emotion', {}).get('dominant', 'N/A')}"
                                )
                                break
                            else:
                                logger.warning(f"[job={job_id}] Prosody returned success=false: {pdata.get('error')}")
                        else:
                            logger.warning(f"[job={job_id}] Prosody HTTP {prosody_resp.status_code}: {prosody_resp.text[:300]}")
                    except RequestException as e:
                        logger.warning(f"[job={job_id}] Prosody request exception (attempt {attempt}): {e}")
                    except Exception as e:
                        logger.exception(f"[job={job_id}] Prosody unexpected error (attempt {attempt}): {e}")

                    # backoff before next attempt
                    if attempt < attempts:
                        sleep_time = backoff * (2 ** (attempt - 1))
                        logger.info(f"[job={job_id}] Prosody retry sleeping {sleep_time:.1f}s before next attempt")
                        time.sleep(sleep_time)

                total_dt = time.time() - t_start
                logger.info(f"[job={job_id}] 🎭 Prosody attempts finished in {total_dt:.1f}s")

                if prosody_resp is None:
                    logger.warning(f"[job={job_id}] Prosody never responded, using original audio_url")

            # استبدل audio_url بالنتيجة (سواء تم التعديل أم لا)
            audio_url = prosody_audio_url
            final_url = audio_url
            output_type = "audio"

            # ===========================================
            # 🎬 VIDEO OUTPUT - منطق ذكي
            # ===========================================
            # كشف نوع الملف من file_key أو من media_url
            source_str = (file_key or media_url or '').lower()
            video_exts = ['.mp4', '.mov', '.mkv', '.webm', '.avi', '.mpg', '.mpeg', '.m4v']
            is_video = any(ext in source_str for ext in video_exts)

            logger.info(f"[job={job_id}] 🔍 file={source_str[:80]}, is_video={is_video}, "
                       f"video_output={video_output}, with_lipsync={with_lipsync}")

            if is_video and video_output and media_url:
                # Path 1: مع lip sync → استخدم Modal (LatentSync)
                if with_lipsync and MODAL_LIPSYNC_URL:
                    try:
                        logger.info(f"[job={job_id}] 🎬 → LatentSync (Modal)")
                        smart_url = f"{MODAL_LIPSYNC_URL.rstrip('/')}/dub-video"
                        smart_resp = requests.post(smart_url, json={
                            'media_url': media_url,
                            'dubbed_audio_url': audio_url,
                            'preserve_background': True,
                            'auto_lipsync': True,
                            'force_lipsync': True,
                        }, timeout=1500)

                        if smart_resp.status_code == 200:
                            smart_data = smart_resp.json()
                            if smart_data.get('success'):
                                final_url = smart_data.get('output_url', audio_url)
                                output_type = smart_data.get('output_type', 'audio')
                                logger.info(f"[job={job_id}] ✅ LipSync done")
                            else:
                                logger.warning(f"[job={job_id}] LipSync data: {smart_data}")
                        else:
                            logger.warning(f"[job={job_id}] LipSync HTTP {smart_resp.status_code}")
                    except Exception as e:
                        logger.warning(f"[job={job_id}] LipSync failed: {e}")

                # Path 2: بدون lip sync → دمج محلي بـ ffmpeg (سريع جداً!)
                else:
                    logger.info(f"[job={job_id}] 🎬 → Local ffmpeg merge")
                    merged_url = _merge_video_audio_locally(media_url, audio_url)
                    if merged_url:
                        final_url = merged_url
                        output_type = "video"
                        logger.info(f"[job={job_id}] ✅ Video merged: {merged_url[:80]}")
                    else:
                        logger.warning(f"[job={job_id}] ⚠️ Merge returned None - check ffmpeg")
            elif not is_video:
                logger.info(f"[job={job_id}] 📻 Audio file detected, skipping video merge")
            elif not video_output:
                logger.info(f"[job={job_id}] 🔇 video_output=False, audio only")

            # ✅ نجح
            job.status = 'completed'
            job.output_url = final_url
            job.engine = data.get('engine_used', engine or 'auto')
            job.completed_at = datetime.utcnow()
            db.session.commit()

            logger.info(f"[job={job_id}] ✅ done ({output_type})")

        except Exception as e:
            logger.exception(f"[job={job_id}] ❌ failed: {e}")
            try:
                job.status = 'failed'
                job.error_message = str(e)[:500]
                job.completed_at = datetime.utcnow()
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
                job.completed_at = datetime.utcnow()
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
                job.completed_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()
            try:
                self.retry(exc=e, countdown=10)
            except Exception:
                pass


# Backward compat alias
process_tts = process_smart_tts
