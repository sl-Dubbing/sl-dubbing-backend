"""
sl-Dubbing Celery Tasks - V2 (SECURE)
- Uses environment variables (NO hardcoded secrets)
- Proper error handling
- Logging for debugging
"""

import os
import time
import logging
import traceback
from pathlib import Path
from celery import Celery
import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# ENVIRONMENT VARIABLES (SECURE)
# ============================================================================

# Cloudinary Configuration
CLOUDINARY_NAME = os.getenv('CLOUDINARY_NAME')
CLOUDINARY_API_KEY = os.getenv('CLOUDINARY_API_KEY')
CLOUDINARY_API_SECRET = os.getenv('CLOUDINARY_API_SECRET')

# Redis Configuration
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')

# ============================================================================
# VALIDATION
# ============================================================================

def validate_environment():
    """Validate that all required environment variables are set"""
    required_vars = {
        'CLOUDINARY_NAME': CLOUDINARY_NAME,
        'CLOUDINARY_API_KEY': CLOUDINARY_API_KEY,
        'CLOUDINARY_API_SECRET': CLOUDINARY_API_SECRET,
        'REDIS_URL': REDIS_URL,
    }
    
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        error_msg = f"❌ Missing environment variables: {', '.join(missing)}"
        logger.error(error_msg)
        raise ValueError(error_msg)
    
    logger.info("✅ All environment variables validated")

# Validate on startup
try:
    validate_environment()
except ValueError as e:
    logger.error(str(e))
    raise

# ============================================================================
# CLOUDINARY SETUP
# ============================================================================

cloudinary.config(
    cloud_name=CLOUDINARY_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

logger.info(f"✅ Cloudinary configured: {CLOUDINARY_NAME}")

# ============================================================================
# CELERY SETUP
# ============================================================================

app = Celery(
    'sl_dubbing_tasks',
    broker=REDIS_URL,
    backend=REDIS_URL
)

# Configure Celery
app.conf.update(
    # Redis configuration
    broker_use_ssl={'ssl_cert_reqs': 'none'},
    redis_backend_use_ssl={'ssl_cert_reqs': 'none'},
    
    # Worker configuration
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    task_time_limit=600,
    task_soft_time_limit=480,
    
    # Task configuration
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    
    # Result backend configuration
    result_expires=3600,
    result_backend_transport_options={
        'master_name': 'mymaster',
        'ssl_cert_reqs': 'none'
    }
)

logger.info(f"✅ Celery configured with Redis")

# ============================================================================
# TTS MODEL LOADING
# ============================================================================

try:
    import torch
    from TTS.api import TTS
    
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info(f"🎙️ Loading XTTS v2 on {device}...")
    
    tts = TTS(
        "tts_models/multilingual/multi-dataset/xtts_v2",
        progress_bar=False,
        gpu=(device == "cuda")
    )
    
    logger.info(f"✅ XTTS v2 loaded successfully on {device}")
    
except Exception as e:
    logger.error(f"❌ Failed to load TTS model: {e}")
    logger.error(traceback.format_exc())
    tts = None

# ============================================================================
# SPEAKER MANAGEMENT
# ============================================================================

SPEAKERS_DIR = Path(__file__).parent / "speakers"
SPEAKERS_DIR.mkdir(exist_ok=True)

def get_speaker_reference(speaker_id='default'):
    """Get speaker reference WAV file"""
    if speaker_id == 'default' or not speaker_id:
        speaker_path = SPEAKERS_DIR / "default.wav"
    else:
        speaker_path = SPEAKERS_DIR / f"{speaker_id}.wav"
    
    if not speaker_path.exists():
        logger.warning(f"⚠️ Speaker file not found: {speaker_path}")
        return str(SPEAKERS_DIR / "default.wav")
    
    return str(speaker_path)

# ============================================================================
# CELERY TASKS
# ============================================================================

@app.task(
    name='tasks.process_tts',
    bind=True,
    max_retries=2,
    default_retry_delay=60
)
def process_tts(self, data):
    """
    Process Text-to-Speech task
    
    Args:
        data (dict): Dictionary containing:
            - text: Text to synthesize
            - lang: Language code (ar, en, etc)
            - speaker_id: Speaker identifier
            - segments: List of subtitle segments
    
    Returns:
        dict: Result with status and audio_url or error
    """
    
    job_id = self.request.id
    logger.info(f"[{job_id}] 🎤 Starting TTS task")
    
    try:
        # Validate input data
        if not isinstance(data, dict):
            raise ValueError("Invalid input: data must be a dictionary")
        
        # Extract parameters
        lang = data.get('lang', 'ar')
        speaker_id = data.get('speaker_id', 'default')
        text = data.get('text', '').strip()
        segments = data.get('segments', [])
        
        # Validate language
        valid_langs = ['ar', 'en', 'fr', 'de', 'es', 'tr', 'zh', 'ja']
        if lang not in valid_langs:
            raise ValueError(f"Unsupported language: {lang}. Supported: {valid_langs}")
        
        # Validate text
        if not text and not segments:
            raise ValueError("Either 'text' or 'segments' must be provided")
        
        if segments:
            # Join segments into single text
            full_text = " . ".join([s.get('text', '').strip() for s in segments if s.get('text')])
        else:
            full_text = text
        
        if not full_text or len(full_text) < 5:
            raise ValueError("Text is too short or empty")
        
        if len(full_text) > 50000:
            logger.warning(f"[{job_id}] Text exceeds 50k chars, truncating...")
            full_text = full_text[:50000]
        
        logger.info(f"[{job_id}] 📝 Processing {len(full_text)} characters in {lang}")
        logger.info(f"[{job_id}] 🎙️ Speaker: {speaker_id}")
        
        # Validate TTS model
        if tts is None:
            raise RuntimeError("TTS model not loaded. Server may still be initializing.")
        
        # Get speaker reference
        speaker_wav = get_speaker_reference(speaker_id)
        if not Path(speaker_wav).exists():
            logger.warning(f"[{job_id}] Speaker WAV not found, using default")
            speaker_wav = get_speaker_reference('default')
        
        # Generate output path
        output_path = f"/tmp/tts_{job_id}.wav"
        
        logger.info(f"[{job_id}] 🔊 Generating audio...")
        
        # Generate speech
        tts.tts_to_file(
            text=full_text,
            speaker_wav=speaker_wav,
            language=lang,
            file_path=output_path,
            split_sentences=True,
            verbose=False
        )
        
        # Verify output file
        if not Path(output_path).exists():
            raise RuntimeError(f"TTS generation failed: output file not created")
        
        file_size = Path(output_path).stat().st_size
        if file_size < 1000:
            raise RuntimeError(f"TTS output file too small: {file_size} bytes")
        
        logger.info(f"[{job_id}] ✅ Audio generated: {file_size} bytes")
        
        # Upload to Cloudinary
        logger.info(f"[{job_id}] ☁️ Uploading to Cloudinary...")
        
        upload_response = cloudinary.uploader.upload(
            output_path,
            resource_type="video",
            folder="sl-dubbing/audio",
            public_id=f"tts_{job_id}",
            overwrite=True
        )
        
        audio_url = upload_response.get('secure_url')
        if not audio_url:
            raise RuntimeError("Cloudinary upload failed: no URL returned")
        
        logger.info(f"[{job_id}] ✅ Uploaded successfully")
        
        # Cleanup
        try:
            Path(output_path).unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"[{job_id}] Failed to cleanup temp file: {e}")
        
        result = {
            "status": "done",
            "job_id": job_id,
            "audio_url": audio_url,
            "message": "✅ Audio generation completed successfully",
            "file_size": file_size
        }
        
        logger.info(f"[{job_id}] 🎉 Task completed successfully")
        return result
        
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        logger.error(f"[{job_id}] ❌ {error_msg}")
        logger.error(traceback.format_exc())
        
        return {
            "status": "error",
            "job_id": job_id,
            "error": error_msg
        }

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.task(name='tasks.health_check')
def health_check():
    """Simple health check task"""
    return {
        "status": "ok",
        "tts_loaded": tts is not None,
        "timestamp": time.time()
    }

if __name__ == '__main__':
    app.start()
