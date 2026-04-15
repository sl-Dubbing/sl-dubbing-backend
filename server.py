# server.py
import os, uuid, time, logging, re
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from dotenv import load_dotenv
import jwt
from functools import wraps
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Load env
load_dotenv()

# Logging
DEBUG = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Config constants
ALLOWED_ORIGINS = ['https://sl-dubbing.github.io', 'http://localhost:5500', 'http://127.0.0.1:5500']
ALLOWED_LANGS = ['ar', 'en', 'es', 'fr', 'de', 'it', 'pt', 'tr', 'ru', 'zh', 'ja', 'ko', 'yue', 'hi', 'ur']
ALLOWED_VOICE_MODES = ['gtts', 'xtts', 'cosy', 'source']
MAX_TEXT_LENGTH = 10000

# Flask app
app = Flask(__name__)
app.config['DEBUG'] = DEBUG
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
if not app.config['SECRET_KEY']:
    raise ValueError("SECRET_KEY must be set")

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL must be set")
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)
limiter = Limiter(get_remote_address, app=app, default_limits=["1000 per day", "100 per hour"], storage_uri="memory://")

# Import shared models and init db
from models import db, User, DubbingJob, CreditTransaction
db.init_app(app)

# Import Celery task entrypoint (tasks.py defines celery_app and process_tts)
# We import lazily inside function to avoid circular import at module import time
def get_celery():
    # tasks.py must be in same package; import here to avoid circular import issues
    import tasks
    return tasks.celery_app, tasks.process_tts

# Helpers
def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS': return f(*args, **kwargs)
        token = request.cookies.get('sl_auth_token')
        if not token: return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user_id = data.get('user_id')
            if not user_id:
                raise ValueError("Missing user_id")
            user = User.query.get(user_id)
            if not user:
                raise ValueError("User not found")
            request.user = user
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except (jwt.InvalidTokenError, ValueError, KeyError):
            ip = request.remote_addr or request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or 'unknown'
            logger.warning(f"Invalid token attempt from IP: {ip}")
            return jsonify({'error': 'Session expired or invalid.'}), 401
        except Exception as e:
            if app.config.get('DEBUG'):
                logger.exception("Unexpected auth error")
            else:
                logger.error(f"Unexpected auth error: {type(e).__name__}")
            return jsonify({'error': 'Session expired or invalid.'}), 401
        return f(*args, **kwargs)
    return decorated_function

def is_valid_srt(srt_text):
    if not srt_text: return False
    if srt_text.count('-->') < 1: return False
    timestamp_pattern = r'\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}'
    return re.search(timestamp_pattern, srt_text) is not None

# Routes
@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("5 per minute")
def dub():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get('text') or '').strip()
    srt = (data.get('srt') or '').strip()
    lang = data.get('lang', 'ar')
    voice_mode = data.get('voice_mode', 'gtts')
    voice_id = data.get('voice_id', '')
    voice_url = data.get('voice_url', '')

    if lang not in ALLOWED_LANGS:
        return jsonify({'success': False, 'error': 'Invalid language selected'}), 400
    if voice_mode not in ALLOWED_VOICE_MODES:
        return jsonify({'success': False, 'error': 'Invalid voice mode'}), 400
    if voice_mode in ['xtts', 'cosy'] and (not voice_id or not voice_url):
        return jsonify({'success': False, 'error': 'Voice URL and Voice ID required for cloning modes'}), 400
    if voice_url and not voice_url.startswith('https://'):
        return jsonify({'success': False, 'error': 'Invalid voice URL. HTTPS required.'}), 400
    if not text and srt and not is_valid_srt(srt):
        return jsonify({'success': False, 'error': 'Invalid SRT format detected'}), 400

    text_length = len(text) if text else len(srt)
    if text_length < 5:
        return jsonify({'success': False, 'error': 'Text too short'}), 400
    if text_length > MAX_TEXT_LENGTH:
        return jsonify({'success': False, 'error': f'Text exceeds maximum allowed length ({MAX_TEXT_LENGTH})'}), 400

    user = request.user
    # Reserve credits and create job
    if user.credits < text_length:
        return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402

    job_id = str(uuid.uuid4())
    try:
        user.credits -= text_length
        db.session.add(CreditTransaction(user_id=user.id, transaction_type='usage', amount=-text_length, reason='Dubbing'))
        job = DubbingJob(id=job_id, user_id=user.id, language=lang, voice_mode=voice_mode, text_length=text_length, credits_used=text_length, status='processing')
        db.session.add(job)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"DB error reserving credits/job: {type(e).__name__}")
        return jsonify({'success': False, 'error': 'Internal error reserving job'}), 500

    # Enqueue Celery task
    try:
        celery_app, process_tts = get_celery()
        payload = {
            'job_id': job_id,
            'user_id': user.id,
            'text': text,
            'srt': srt,
            'lang': lang,
            'voice_mode': voice_mode,
            'voice_id': voice_id,
            'voice_url': voice_url
        }
        task = process_tts.delay(payload)
        logger.info(f"Enqueued TTS task: job_id={job_id} task_id={task.id}")
    except Exception as e:
        # rollback reservation if enqueue fails
        try:
            job = DubbingJob.query.get(job_id)
            if job:
                job.status = 'failed'
            user.credits += text_length
            db.session.add(CreditTransaction(user_id=user.id, transaction_type='refund', amount=text_length, reason='Enqueue failed'))
            db.session.commit()
        except Exception:
            db.session.rollback()
        logger.error(f"Failed to enqueue task: {type(e).__name__}")
        return jsonify({'success': False, 'error': 'Failed to start background processing'}), 500

    return jsonify({'success': True, 'job_id': job_id, 'task_id': task.id, 'status': 'processing', 'remaining_credits': user.credits}), 200

@app.route('/api/job/<job_id>', methods=['GET'])
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != request.user.id:
        return jsonify({'success': False, 'error': 'Job not found'}), 404
    return jsonify({
        'success': True,
        'job_id': job.id,
        'status': job.status,
        'audio_url': job.output_url,
        'method': job.method,
        'processing_time': job.processing_time,
        'credits_used': job.credits_used,
        'created_at': job.created_at.isoformat()
    }), 200

# Health
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()}), 200

# Create DB tables if run directly
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), threaded=True)
