# gateway.py — API Gateway (معدّل: تحسينات أمان، استقرار، وحدود)
import os
import json
import logging
import time
import base64
import datetime as _dt
from functools import wraps

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session
from werkzeug.exceptions import BadRequest

# Optional rate limiting
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    LIMITER_AVAILABLE = True
except Exception:
    LIMITER_AVAILABLE = False

# Supabase client
from supabase import create_client, Client

# edge-tts
import edge_tts

# DB models and tasks (from your project)
from models import db, User, DubbingJob, CreditTransaction
from tasks import process_smart_tts

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gateway")

# ---------------------------
# Flask app and CORS
# ---------------------------
app = Flask(__name__)

# Read allowed origins from env (comma separated) or default to localhost for dev
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]

# Use CORS with explicit origins (avoid "*")
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)

# Rate limiter (optional)
if LIMITER_AVAILABLE:
    limiter = Limiter(app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])
    logger.info("Flask-Limiter enabled")
else:
    limiter = None
    logger.info("Flask-Limiter not available; skipping rate limiting")

# ---------------------------
# Database / Supabase setup
# ---------------------------
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Supabase client: {e}")
        supabase = None
else:
    logger.warning("SUPABASE_URL or SUPABASE_KEY not set; auth will fail")

# Default edge voices map
DEFAULT_EDGE_VOICES = {
    "ar": "ar-SA-HamedNeural", "en": "en-US-AriaNeural",
    "fr": "fr-FR-DeniseNeural", "es": "es-ES-AlvaroNeural",
}

# Limits and config
MAX_TTS_LENGTH = int(os.environ.get('MAX_TTS_LENGTH', 5000))
HIGH_QUALITY_COST = int(os.environ.get('HIGH_QUALITY_COST', 10))
QUICK_TTS_COST = int(os.environ.get('QUICK_TTS_COST', 1))

# ---------------------------
# Helpers
# ---------------------------
def json_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not request.is_json:
            raise BadRequest("Expected application/json")
        return f(*args, **kwargs)
    return wrapper

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not supabase:
            logger.error("Supabase client not configured")
            return jsonify({'success': False, 'message': 'Authentication service not configured'}), 500

        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header:
            parts = auth_header.split()
            if len(parts) == 2 and parts[0].lower() == 'bearer':
                token = parts[1]
            else:
                token = auth_header

        if not token:
            return jsonify({'success': False, 'message': 'التوكن مفقود!'}), 401

        try:
            # supabase-py interface may vary; handle common shapes
            user_resp = supabase.auth.get_user(token) if hasattr(supabase.auth, "get_user") else supabase.auth.api.get_user(token)
            supabase_user = None
            # user_resp may be a dict-like or object
            if isinstance(user_resp, dict):
                supabase_user = user_resp.get('data') or user_resp.get('user')
            else:
                # object with .user or .data
                supabase_user = getattr(user_resp, 'user', None) or getattr(user_resp, 'data', None)

            if not supabase_user:
                logger.warning("Supabase returned no user for token")
                return jsonify({'success': False, 'message': 'توكن غير صالح!'}), 401

            # supabase_user may be dict or object
            supabase_id = supabase_user.get('id') if isinstance(supabase_user, dict) else getattr(supabase_user, 'id', None)
            email = supabase_user.get('email') if isinstance(supabase_user, dict) else getattr(supabase_user, 'email', None)
            metadata = supabase_user.get('user_metadata', {}) if isinstance(supabase_user, dict) else getattr(supabase_user, 'user_metadata', {}) or {}

            if not supabase_id:
                logger.warning("Supabase user id missing")
                return jsonify({'success': False, 'message': 'توكن غير صالح!'}), 401

            # Find or create local user
            current_user = User.query.filter_by(supabase_id=supabase_id).first()
            if not current_user:
                logger.info(f"Creating local user for supabase_id={supabase_id}")
                current_user = User(
                    supabase_id=supabase_id,
                    email=email,
                    name=metadata.get('full_name', (email.split('@')[0] if email else 'user')),
                    avatar=metadata.get('avatar_url', '👤'),
                    credits=int(os.environ.get('WELCOME_CREDITS', 50000)),
                    auth_method='supabase'
                )
                db.session.add(current_user)
                db.session.flush()

                welcome_amount = int(os.environ.get('WELCOME_CREDITS', 50000))
                welcome_transaction = CreditTransaction(
                    user_id=current_user.id,
                    transaction_type='bonus',
                    amount=welcome_amount,
                    reason='نقاط ترحيبية'
                )
                db.session.add(welcome_transaction)
                db.session.commit()
            else:
                # update last_login if you have such field
                try:
                    current_user.last_login = _dt.datetime.utcnow()
                    db.session.commit()
                except Exception:
                    db.session.rollback()

        except Exception as e:
            logger.error(f"Auth Error: {e}")
            return jsonify({'success': False, 'message': 'جلسة غير صالحة'}), 401

        return f(current_user, *args, **kwargs)
    return decorated

def deduct_credits_atomic(user_id: int, amount: int) -> bool:
    """
    Attempt to deduct credits atomically using SELECT FOR UPDATE.
    Returns True if deduction succeeded, False if insufficient funds.
    """
    session: Session = db.session
    try:
        # Start a transaction block
        with session.begin():
            # Lock the user row
            user = session.query(User).with_for_update().filter(User.id == user_id).one_or_none()
            if not user:
                logger.error(f"User {user_id} not found for credit deduction")
                return False
            if (user.credits or 0) < amount:
                logger.info(f"User {user_id} has insufficient credits: {user.credits} < {amount}")
                return False
            user.credits -= amount
            # Add transaction record
            tx = CreditTransaction(
                user_id=user.id,
                transaction_type='debit',
                amount=amount,
                reason=f'Automatic deduction {int(time.time())}'
            )
            session.add(tx)
        return True
    except Exception as e:
        logger.error(f"Atomic deduction failed: {e}")
        session.rollback()
        return False

# ---------------------------
# Routes
# ---------------------------
@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "Gateway Online"}), 200

@app.route('/api/user', methods=['GET'])
@token_required
def get_user(current_user):
    return jsonify({'success': True, 'user': current_user.to_dict()}), 200

# High-quality TTS dispatch (async)
@app.route('/api/tts', methods=['POST'])
@token_required
@json_required
def start_tts(current_user):
    data = request.json or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({"success": False, "error": "النص غير موجود"}), 400
    if len(text) > MAX_TTS_LENGTH:
        return jsonify({"success": False, "error": f"النص طويل جداً (الحد {MAX_TTS_LENGTH} حرف)"}), 400

    cost = HIGH_QUALITY_COST
    # Atomic deduction
    ok = deduct_credits_atomic(current_user.id, cost)
    if not ok:
        return jsonify({"success": False, "error": "رصيدك غير كافٍ"}), 402

    try:
        new_job = DubbingJob(
            user_id=current_user.id,
            status='pending',
            language=data.get('lang', 'en'),
            voice_mode=(data.get('voice_id') or 'default')[:50],
            text_length=len(text)
        )
        db.session.add(new_job)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to create job record: {e}")
        # refund if needed (attempt best-effort)
        try:
            session = db.session
            with session.begin():
                user = session.query(User).with_for_update().filter(User.id == current_user.id).one_or_none()
                if user:
                    user.credits += cost
                    session.add(CreditTransaction(user_id=user.id, transaction_type='credit', amount=cost, reason=f'refund job creation failure {new_job.id}'))
        except Exception as ex:
            logger.error(f"Failed to refund after job creation failure: {ex}")
        return jsonify({"success": False, "error": "فشل إنشاء المهمة"}), 500

    payload = {
        'job_id': str(new_job.id),
        'text': text,
        'lang': data.get('lang', 'en'),
        'voice_id': data.get('voice_id', ''),
        'sample_b64': data.get('sample_b64', ''),
        'edge_voice': data.get('edge_voice', ''),
        'translate': data.get('translate', True),
        'rate': data.get('rate', '+0%'),
        'pitch': data.get('pitch', '+0Hz')
    }

    # Dispatch to background worker safely
    try:
        if hasattr(process_smart_tts, 'spawn'):
            process_smart_tts.spawn(payload)
        elif hasattr(process_smart_tts, 'delay'):
            process_smart_tts.delay(payload)
        else:
            # If process_smart_tts is a callable that handles dispatch itself
            process_smart_tts(payload)
        logger.info(f"Dispatched job {new_job.id}")
    except Exception as e:
        logger.error(f"Failed to dispatch task for job {new_job.id}: {e}")
        # mark job failed and refund
        try:
            new_job.status = 'failed'
            db.session.commit()
        except Exception:
            db.session.rollback()
        # refund
        try:
            session = db.session
            with session.begin():
                user = session.query(User).with_for_update().filter(User.id == current_user.id).one_or_none()
                if user:
                    user.credits += cost
                    session.add(CreditTransaction(user_id=user.id, transaction_type='credit', amount=cost, reason=f'refund dispatch failure {new_job.id}'))
        except Exception as ex:
            logger.error(f"Failed to refund after dispatch failure: {ex}")
        return jsonify({"success": False, "error": "فشل توجيه المهمة"}), 500

    return jsonify({
        "success": True,
        "message": "بدأت المعالجة في الخلفية (جودة عالية)",
        "job_id": str(new_job.id),
        "status": "processing"
    }), 202

# Quick streaming TTS (edge-tts)
@app.route('/api/tts/quick', methods=['POST'])
@token_required
@json_required
def quick_tts(current_user):
    data = request.json or {}
    text = (data.get('text') or '').strip()
    lang = data.get('lang', 'ar')
    voice = data.get('edge_voice') or DEFAULT_EDGE_VOICES.get(lang, "ar-SA-HamedNeural")

    if not text:
        return jsonify({"error": "النص مفقود"}), 400
    if len(text) > MAX_TTS_LENGTH:
        return jsonify({"error": f"النص طويل جداً (الحد {MAX_TTS_LENGTH} حرف)"}), 400

    cost = QUICK_TTS_COST
    ok = deduct_credits_atomic(current_user.id, cost)
    if not ok:
        return jsonify({"error": "رصيدك غير كافٍ"}), 402

    # Streaming generator
    def generate():
        try:
            communicate = edge_tts.Communicate(
                text,
                voice,
                rate=data.get('rate', '+0%'),
                pitch=data.get('pitch', '+0Hz')
            )
            for chunk in communicate.stream_sync():
                # chunk expected to be dict with type and data
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("type") == "audio":
                    raw = chunk.get("data")
                    if isinstance(raw, str):
                        # likely base64 string
                        try:
                            audio_bytes = base64.b64decode(raw)
                        except Exception:
                            audio_bytes = raw.encode('utf-8', errors='ignore')
                    elif isinstance(raw, (bytes, bytearray)):
                        audio_bytes = bytes(raw)
                    else:
                        # fallback
                        audio_bytes = str(raw).encode('utf-8', errors='ignore')
                    yield audio_bytes
        except Exception as e:
            logger.error(f"Streaming Error: {e}")
            # On error, yield nothing and close stream
            return

    response = Response(stream_with_context(generate()), mimetype="audio/mpeg")
    response.headers['X-Remaining-Credits'] = str(current_user.credits)
    response.headers['Access-Control-Expose-Headers'] = 'X-Remaining-Credits'
    return response

# Job status check
@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def check_job(current_user, job_id):
    try:
        job = DubbingJob.query.get(job_id)
        if not job or job.user_id != current_user.id:
            return jsonify({"status": "failed", "error": "غير مصرح لك"}), 403
        return jsonify(job.to_dict()), 200
    except Exception as e:
        logger.error(f"Check job error: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500

# ---------------------------
# Error handlers
# ---------------------------
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"success": False, "error": str(e)}), 400

@app.errorhandler(401)
def unauthorized(e):
    return jsonify({"success": False, "error": "Unauthorized"}), 401

@app.errorhandler(404)
def not_found(e):
    return jsonify({"success": False, "error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    logger.exception("Internal server error")
    return jsonify({"success": False, "error": "Internal server error"}), 500

# ---------------------------
# Run (use production WSGI server in production)
# ---------------------------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    # debug False for safety; use Gunicorn/uvicorn in production
    app.run(host='0.0.0.0', port=port, debug=False)
