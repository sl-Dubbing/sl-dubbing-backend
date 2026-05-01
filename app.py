# app.py — V1.1 (Atomic Credits, Safer Uploads, Edge-TTS Fixes)
import os
import time
import base64
import asyncio
import logging
import uuid
import datetime as _dt
from functools import wraps

import jwt
import boto3
from botocore.client import Config, BotoCoreError, ClientError
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from dotenv import load_dotenv

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    LIMITER_AVAILABLE = True
except Exception:
    LIMITER_AVAILABLE = False

import edge_tts

from models import db, User, DubbingJob, CreditTransaction
from tasks import process_smart_tts, process_dub

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gateway")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]
CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS)

if LIMITER_AVAILABLE:
    limiter = Limiter(app, key_func=get_remote_address, default_limits=["200 per day", "50 per minute"])
    logger.info("Flask-Limiter enabled")
else:
    limiter = None

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

SUPABASE_JWT_SECRET = os.environ.get('SUPABASE_JWT_SECRET')
MAX_TTS_LENGTH = int(os.environ.get('MAX_TTS_LENGTH', 5000))

R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)
R2_PUBLIC_BASE = os.environ.get('R2_PUBLIC_BASE')

# File restrictions
ALLOWED_EXTENSIONS = {'mp4', 'mp3', 'wav'}
MAX_FILE_SIZE_MB = int(os.environ.get('MAX_FILE_SIZE_MB', 200))

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def json_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not request.is_json:
            return jsonify({"error": "يجب أن يكون الطلب بصيغة JSON"}), 400
        return f(*args, **kwargs)
    return wrapper

def get_token_from_request():
    auth_header = request.headers.get('Authorization', '') or ''
    if auth_header.lower().startswith('bearer '):
        return auth_header.split()[1]
    return request.cookies.get('session')

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = get_token_from_request()
        if not token:
            return jsonify({'success': False, 'error': 'التوكن مفقود'}), 401

        if not SUPABASE_JWT_SECRET:
            logger.error("SUPABASE_JWT_SECRET not configured")
            return jsonify({'success': False, 'error': 'Server configuration error'}), 500

        try:
            data = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
            email = data.get('email')
            if not email:
                raise ValueError("No email in token")

            current_user = User.query.filter_by(email=email).first()
            if not current_user:
                meta = data.get('user_metadata', {})
                current_user = User(
                    email=email,
                    name=meta.get('full_name', meta.get('name', email.split('@')[0])),
                    avatar=meta.get('avatar_url'),
                    credits=int(os.environ.get('WELCOME_CREDITS', 1000)),
                    auth_method='supabase'
                )
                db.session.add(current_user)
                db.session.commit()
        except Exception as e:
            logger.warning(f"Invalid Token: {e}")
            return jsonify({'success': False, 'error': 'جلسة غير صالحة'}), 401

        return f(current_user, *args, **kwargs)
    return decorated

def deduct_credits_atomic(user_id: int, amount: int, job_id: str = None) -> bool:
    """
    Atomically deduct credits and create a CreditTransaction linked to job_id.
    Use SELECT FOR UPDATE to avoid race conditions.
    """
    try:
        # lock the user row
        user = db.session.query(User).with_for_update().get(user_id)
        if not user or (user.credits or 0) < amount:
            return False
        user.credits -= amount
        tx = CreditTransaction(user_id=user.id, amount=amount, transaction_type='debit', job_id=job_id)
        db.session.add(tx)
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        logger.exception("Credit Deduction Error user_id=%s job_id=%s: %s", user_id, job_id, e)
        return False

@app.route('/api/health', methods=['GET'])
def health_check():
    db_ok = True
    try:
        db.session.execute("SELECT 1")
    except Exception:
        db_ok = False
    return jsonify({"status": "ok", "message": "Gateway Online", "db": "ok" if db_ok else "error"}), 200

@app.route('/api/user', methods=['GET'])
@token_required
def get_user(current_user):
    user_dict = current_user.to_dict()
    if getattr(current_user, 'avatar_key', None) and R2_PUBLIC_BASE:
        user_dict['avatar'] = f"{R2_PUBLIC_BASE.rstrip('/')}/{current_user.avatar_key}"
    return jsonify({'success': True, 'user': user_dict}), 200

@app.route('/api/dubbing', methods=['POST'])
@token_required
def start_dubbing_route(current_user):
    cost = int(os.environ.get('DUB_COST', 100))

    file = request.files.get('media_file')
    if not file:
        return jsonify({"error": "لم يتم رفع ملف"}), 400

    filename = secure_filename(file.filename or "")
    if not allowed_file(filename):
        return jsonify({"error": "ملف غير مدعوم"}), 400

    content_length = request.content_length or 0
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    if content_length and content_length > max_bytes:
        return jsonify({"error": "حجم الملف أكبر من المسموح"}), 413

    # Create job first so we can link the credit transaction
    new_job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='pending',
        language=request.form.get('lang', 'ar'),
        method='dubbing',
        credits_used=cost
    )
    db.session.add(new_job)
    db.session.flush()  # ensure new_job.id is available

    # Deduct credits atomically linked to job
    if not deduct_credits_atomic(current_user.id, cost, job_id=new_job.id):
        db.session.rollback()
        return jsonify({"error": "رصيد غير كافٍ"}), 402

    # Upload file with error handling
    file_key = f"uploads/{uuid.uuid4()}_{filename}"
    try:
        file.stream.seek(0)
        s3_client.upload_fileobj(file.stream, R2_BUCKET_NAME, file_key)
    except (BotoCoreError, ClientError, Exception) as e:
        db.session.rollback()
        logger.exception("File upload failed user_id=%s job_id=%s: %s", current_user.id, new_job.id, e)
        # refund
        try:
            # create refund transaction
            u = User.query.get(current_user.id)
            if u:
                u.credits = (u.credits or 0) + cost
                db.session.add(CreditTransaction(user_id=u.id, amount=cost, transaction_type='refund', job_id=new_job.id))
                db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("Refund failed for user_id=%s job_id=%s", current_user.id, new_job.id)
        return jsonify({"error": "فشل رفع الملف"}), 500

    # finalize job record and enqueue
    try:
        new_job.file_key = file_key
        new_job.status = 'processing'
        db.session.add(new_job)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to finalize job record job_id=%s: %s", new_job.id, e)
        return jsonify({"error": "فشل إنشاء المهمة"}), 500

    try:
        process_dub.delay({
            'job_id': new_job.id,
            'file_key': file_key,
            'lang': new_job.language,
            'voice_id': request.form.get('voice_id', 'source'),
            'sample_b64': request.form.get('sample_b64', '')
        })
    except Exception as e:
        logger.exception("Failed to enqueue job_id=%s: %s", new_job.id, e)

    return jsonify({"success": True, "job_id": new_job.id}), 202

# Helper to run edge-tts safely
def _edge_tts_stream(text, voice, rate, pitch):
    async def _run():
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]
    # Use asyncio.run to avoid creating persistent event loops in WSGI workers
    return asyncio.run(_collect_chunks(_run()))

async def _collect_chunks(coro):
    chunks = []
    async for c in coro:
        chunks.append(c)
    return chunks

@app.route('/api/tts/quick', methods=['POST'])
@token_required
@json_required
def quick_tts(current_user):
    data = request.json or {}
    text = (data.get('text') or '').strip()
    voice = data.get('edge_voice') or "ar-SA-HamedNeural"
    rate = data.get('rate', '+0%')
    pitch = data.get('pitch', '+0Hz')

    if not text:
        return jsonify({"error": "النص مفقود"}), 400
    if len(text) > MAX_TTS_LENGTH:
        return jsonify({"error": "النص طويل جداً"}), 400

    cost = 1
    # create a pseudo-job for tracking optional (not required for quick TTS)
    if not deduct_credits_atomic(current_user.id, cost):
        return jsonify({"error": "رصيدك غير كافٍ"}), 402

    def generate():
        try:
            # run edge-tts and stream chunks
            chunks = _edge_tts_stream(text, voice, rate, pitch)
            for chunk in chunks:
                yield chunk
        except Exception as e:
            logger.exception("Edge-TTS Error user_id=%s: %s", current_user.id, e)

    response = Response(stream_with_context(generate()), mimetype="audio/mpeg")
    response.headers['X-Remaining-Credits'] = str(User.query.get(current_user.id).credits)
    response.headers['Access-Control-Expose-Headers'] = 'X-Remaining-Credits'
    return response

@app.route('/api/tts/smart', methods=['POST'])
@token_required
@json_required
def start_smart_tts(current_user):
    data = request.json or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({"error": "النص غير موجود"}), 400

    cost = 10
    # create job first
    new_job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='pending',
        language=data.get('lang', 'ar'),
        method='smart_tts',
        credits_used=cost
    )
    db.session.add(new_job)
    db.session.flush()

    if not deduct_credits_atomic(current_user.id, cost, job_id=new_job.id):
        db.session.rollback()
        return jsonify({"error": "رصيدك غير كافٍ"}), 402

    try:
        db.session.add(new_job)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to create smart_tts job user_id=%s: %s", current_user.id, e)
        return jsonify({"error": "فشل إنشاء المهمة"}), 500

    payload = {
        'job_id': new_job.id,
        'text': text,
        'lang': data.get('lang', 'ar'),
        'voice_id': data.get('voice_id', ''),
        'sample_b64': data.get('sample_b64', ''),
        'rate': data.get('rate', '+0%'),
        'pitch': data.get('pitch', '+0Hz')
    }

    try:
        process_smart_tts.delay(payload)
    except Exception as e:
        logger.exception("Failed to enqueue smart_tts job_id=%s: %s", new_job.id, e)

    return jsonify({"success": True, "job_id": new_job.id}), 202

@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def check_job(current_user, job_id):
    try:
        job = DubbingJob.query.get(job_id)
        if not job or job.user_id != current_user.id:
            return jsonify({"status": "failed", "error": "غير مصرح لك"}), 403

        return jsonify({
            "status": job.status,
            "audio_url": job.output_url,
            "error": getattr(job, 'error_message', None)
        }), 200
    except Exception as e:
        logger.exception("Job check error user_id=%s job_id=%s: %s", current_user.id, job_id, e)
        return jsonify({"status": "failed", "error": "خطأ في السيرفر"}), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
