# tasks.py
import os, time, logging, traceback, tempfile
from pathlib import Path
from celery import Celery
from dotenv import load_dotenv
import cloudinary, cloudinary.uploader

# Load env
load_dotenv()

DEBUG = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Celery / Redis
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
celery_app = Celery('sl_dubbing_tasks', broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    task_time_limit=600,
    task_soft_time_limit=480,
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    result_expires=3600,
)

# Cloudinary
CLOUDINARY_NAME = os.getenv('CLOUDINARY_NAME')
CLOUDINARY_API_KEY = os.getenv('CLOUDINARY_API_KEY')
CLOUDINARY_API_SECRET = os.getenv('CLOUDINARY_API_SECRET')
if not (CLOUDINARY_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET):
    logger.warning("Cloudinary credentials not fully set; uploads will fail if attempted.")
else:
    cloudinary.config(cloud_name=CLOUDINARY_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET, secure=True)

# Import models and DB; create a minimal Flask app context to bind SQLAlchemy
from flask import Flask
from models import db, User, DubbingJob, CreditTransaction

# Create a small Flask app to initialize SQLAlchemy with same DATABASE_URL
flask_app = Flask('sl_dubbing_worker')
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL must be set for worker")
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
flask_app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
flask_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(flask_app)

# TTS model loading (workers handle model)
tts = None
try:
    import torch
    from TTS.api import TTS
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(f"Loading XTTS model on {device}...")
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False, gpu=(device == "cuda"))
    logger.info("XTTS loaded")
except Exception as e:
    logger.error(f"Failed to load TTS model: {type(e).__name__}")
    if DEBUG:
        logger.exception("TTS load exception")
    tts = None

# Helpers
def safe_temp_wav(prefix="tts_", suffix=".wav"):
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return path

def cloudinary_upload_with_retries(local_path, public_id, folder="sl-dubbing/audio", max_attempts=3):
    attempt = 0
    last_exc = None
    while attempt < max_attempts:
        try:
            resp = cloudinary.uploader.upload(local_path, resource_type='auto', folder=folder, public_id=public_id, overwrite=True, use_filename=False)
            return resp
        except Exception as e:
            last_exc = e
            attempt += 1
            wait = 2 ** attempt
            logger.warning(f"Cloudinary upload attempt {attempt} failed: {type(e).__name__}; retrying in {wait}s")
            time.sleep(wait)
    raise last_exc

# The Celery task
@celery_app.task(name='tasks.process_tts', bind=True, max_retries=2, default_retry_delay=60)
def process_tts(self, payload):
    job_id = payload.get('job_id')
    user_id = payload.get('user_id')
    logger.info(f"[{job_id}] Worker started for user {user_id}")

    tmp_output = None
    start_ts = time.time()

    # Use Flask app context for DB operations
    with flask_app.app_context():
        try:
            # Basic validation
            if not job_id or not user_id:
                raise ValueError("Missing job_id or user_id in payload")

            job = DubbingJob.query.get(job_id)
            user = User.query.get(user_id)
            if not job:
                raise ValueError("Job not found")
            if not user:
                raise ValueError("User not found")

            # Prepare text
            text = (payload.get('text') or '').strip()
            srt = (payload.get('srt') or '').strip()
            lang = payload.get('lang', 'ar')
            voice_mode = payload.get('voice_mode', 'gtts')
            voice_id = payload.get('voice_id', '')
            voice_url = payload.get('voice_url', '')

            if not text and srt:
                # join srt lines
                text = srt

            if not text or len(text) < 5:
                raise ValueError("Text too short")

            # Generate temp output
            tmp_output = safe_temp_wav(prefix=f"tts_{job_id}_", suffix=".wav")
            logger.info(f"[{job_id}] Generating audio to {tmp_output}")

            # Choose synthesis method
            method = "gtts"
            output_path = tmp_output

            if voice_mode in ['xtts', 'cosy'] and voice_url and voice_id and tts:
                # Use XTTS zero-shot with speaker sample if possible
                # For simplicity, we attempt to download voice sample to temp and pass to tts
                # NOTE: In production, validate and sanitize voice_url and limit size
                import urllib.request
                sample_tmp = None
                try:
                    sample_tmp = safe_temp_wav(prefix=f"sample_{voice_id}_", suffix=".wav")
                    with urllib.request.urlopen(voice_url, timeout=30) as resp, open(sample_tmp, 'wb') as out_f:
                        out_f.write(resp.read())
                    # call tts with speaker_wav
                    tts.tts_to_file(text=text, speaker_wav=sample_tmp, language=lang, file_path=output_path, split_sentences=True, verbose=False)
                    method = "xtts" if voice_mode == 'xtts' else "cosy"
                except Exception as e:
                    logger.warning(f"[{job_id}] Voice cloning failed, falling back to gTTS: {type(e).__name__}")
                    # fallback to gTTS below
                finally:
                    if sample_tmp and Path(sample_tmp).exists():
                        try:
                            Path(sample_tmp).unlink(missing_ok=True)
                        except Exception:
                            pass

            if method == "gtts":
                # Use gTTS as fallback
                try:
                    from gtts import gTTS
                    gTTS(text=text, lang=lang[:2]).save(output_path)
                except Exception as e:
                    logger.error(f"[{job_id}] gTTS failed: {type(e).__name__}")
                    if DEBUG:
                        logger.exception("gTTS exception")
                    raise

            # Verify output
            if not Path(output_path).exists():
                raise RuntimeError("TTS output file not created")
            file_size = Path(output_path).stat().st_size
            if file_size < 1000:
                raise RuntimeError(f"TTS output file too small: {file_size} bytes")

            # Upload to Cloudinary if configured, else keep local file URL (not recommended for production)
            audio_url = None
            if CLOUDINARY_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
                try:
                    upload_resp = cloudinary_upload_with_retries(output_path, public_id=f"tts_{job_id}")
                    audio_url = upload_resp.get('secure_url') or upload_resp.get('url')
                except Exception as e:
                    logger.error(f"[{job_id}] Cloudinary upload failed: {type(e).__name__}")
                    if DEBUG:
                        logger.exception("Cloudinary upload exception")
                    raise
            else:
                # Fallback: store in /tmp/sl_audio and serve via server's /api/file if desired
                dest_dir = Path('/tmp/sl_audio')
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_path = dest_dir / f"dub_{job_id}.wav"
                Path(output_path).rename(dest_path)
                audio_url = f"file://{dest_path}"

            # Update DB: mark job completed
            processing_time = time.time() - start_ts
            job.output_url = audio_url
            job.status = 'completed'
            job.processing_time = processing_time
            job.method = method
            db.session.add(job)
            db.session.commit()

            # Cleanup local temp
            try:
                if tmp_output and Path(tmp_output).exists():
                    Path(tmp_output).unlink(missing_ok=True)
            except Exception:
                logger.warning(f"[{job_id}] Failed to cleanup tmp output")

            logger.info(f"[{job_id}] Completed successfully: {audio_url}")
            return {
                "status": "done",
                "job_id": job_id,
                "audio_url": audio_url,
                "file_size": file_size
            }

        except Exception as exc:
            # On failure: mark job failed and refund credits
            err_msg = f"{type(exc).__name__}: {str(exc)}"
            logger.error(f"[{job_id}] Task failed: {err_msg}")
            if DEBUG:
                logger.exception("Task exception")

            try:
                job = DubbingJob.query.get(job_id) if job_id else None
                if job:
                    job.status = 'failed'
                    db.session.add(job)
                # refund credits if reserved
                if job and job.credits_used:
                    u = User.query.get(job.user_id)
                    if u:
                        u.credits += job.credits_used
                        db.session.add(CreditTransaction(user_id=u.id, transaction_type='refund', amount=job.credits_used, reason='Dubbing failed'))
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.error(f"[{job_id}] Failed to update DB during error handling")

            # cleanup temp
            try:
                if tmp_output and Path(tmp_output).exists():
                    Path(tmp_output).unlink(missing_ok=True)
            except Exception:
                pass

            # Return structured error (Celery will store result)
            return {"status": "error", "job_id": job_id, "error": err_msg}

    # end with app_context
