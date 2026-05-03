# app.py — V1.3 (Direct Upload + STT + Multi-Lang)
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
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
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
from tasks import process_smart_tts, process_dub, process_stt

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gateway")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]
CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS)

if LIMITER_AVAILABLE:
    limiter = Limiter(app, key_func=get_remote_address, default_limits=["500 per day", "100 per minute"])
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
    region_name='auto',
)
R2_PUBLIC_BASE = os.environ.get('R2_PUBLIC_BASE')

ALLOWED_EXTENSIONS = {'mp4', 'mp3', 'wav', 'm4a', 'mov', 'webm', 'mkv', 'aac', 'ogg', 'flac'}
MAX_FILE_SIZE_MB = int(os.environ.get('MAX_FILE_SIZE_MB', 5000))


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def json_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not request.is_json:
            return jsonify({"error": "JSON required"}), 400
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
            return jsonify({'success': False, 'error': 'Token missing'}), 401

        if not SUPABASE_JWT_SECRET:
            logger.error("SUPABASE_JWT_SECRET not configured")
            return jsonify({'success': False, 'error': 'Server config error'}), 500

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
                    auth_method='supabase',
                    supabase_id=data.get('sub')
                )
                db.session.add(current_user)
                db.session.commit()
        except Exception as e:
            logger.warning(f"Invalid Token: {e}")
            return jsonify({'success': False, 'error': 'Invalid session'}), 401

        return f(current_user, *args, **kwargs)
    return decorated


def deduct_credits_atomic(user_id, amount, job_id=None):
    try:
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
        logger.exception(f"Credit deduction error: {e}")
        return False


def refund_credits(user_id, amount, job_id=None):
    try:
        user = User.query.get(user_id)
        if user:
            user.credits = (user.credits or 0) + amount
            db.session.add(CreditTransaction(user_id=user.id, amount=amount, transaction_type='refund', job_id=job_id))
            db.session.commit()
    except Exception:
        db.session.rollback()


# ==========================================
# 📋 Endpoints
# ==========================================
@app.route('/api/health', methods=['GET'])
def health_check():
    db_ok = True
    try:
        db.session.execute("SELECT 1")
    except Exception:
        db_ok = False
    return jsonify({"status": "ok", "version": "v1.3-direct-upload", "db": "ok" if db_ok else "error"}), 200


@app.route('/api/user', methods=['GET'])
@token_required
def get_user(current_user):
    user_dict = current_user.to_dict()
    if getattr(current_user, 'avatar_key', None) and R2_PUBLIC_BASE:
        user_dict['avatar'] = f"{R2_PUBLIC_BASE.rstrip('/')}/{current_user.avatar_key}"
    return jsonify({'success': True, 'user': user_dict}), 200


@app.route('/api/user/credits', methods=['GET'])
@token_required
def get_credits(current_user):
    return jsonify({'success': True, 'user': {'credits': current_user.credits or 0}, 'credits': current_user.credits or 0})


# ==========================================
# 🚀 Direct Upload — presigned URL
# ==========================================
@app.route('/api/upload-url', methods=['POST'])
@token_required
@json_required
def get_upload_url(current_user):
    """يولّد presigned URL للرفع المباشر إلى R2"""
    data = request.json or {}
    filename = secure_filename(data.get('filename', 'file'))
    content_type = data.get('content_type', 'application/octet-stream')
    size = int(data.get('size', 0))

    if not allowed_file(filename):
        return jsonify({'error': 'Unsupported file type'}), 400

    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    if size > max_bytes:
        return jsonify({'error': f'File too large (max {MAX_FILE_SIZE_MB}MB)'}), 413

    if (current_user.credits or 0) < 1:
        return jsonify({'error': 'Insufficient credits'}), 402

    ext = filename.rsplit('.', 1)[-1] if '.' in filename else 'bin'
    file_key = f"uploads/u{current_user.id}/{uuid.uuid4().hex}.{ext}"

    try:
        upload_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': R2_BUCKET_NAME,
                'Key': file_key,
                'ContentType': content_type,
            },
            ExpiresIn=3600,
            HttpMethod='PUT'
        )
        return jsonify({
            'success': True,
            'upload_url': upload_url,
            'file_key': file_key,
            'expires_in': 3600,
            'method': 'PUT',
            'headers': {'Content-Type': content_type}
        })
    except Exception as e:
        logger.exception(f"upload-url failed: {e}")
        return jsonify({'error': str(e)}), 500


# ==========================================
# 🎬 /api/dub — Direct Upload (JSON)
# ==========================================
@app.route('/api/dub', methods=['POST'])
@token_required
@json_required
def start_dub(current_user):
    """يبدأ مهمة دبلجة بعد الرفع المباشر إلى R2"""
    data = request.json or {}
    file_key = data.get('file_key')
    lang = data.get('lang', 'ar')
    voice_id = data.get('voice_id', 'source')
    sample_b64 = data.get('sample_b64', '')
    engine = data.get('engine', '')

    if not file_key:
        return jsonify({'error': 'file_key required'}), 400

    # تحقّق وجود الملف
    try:
        s3_client.head_object(Bucket=R2_BUCKET_NAME, Key=file_key)
    except Exception:
        return jsonify({'error': 'File not found'}), 404

    cost = int(os.environ.get('DUB_COST', 100))
    if (current_user.credits or 0) < cost:
        return jsonify({'error': 'Insufficient credits'}), 402

    new_job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='pending',
        language=lang,
        method='dubbing',
        voice_id=voice_id,
        engine=engine,
        file_key=file_key,
        credits_used=cost,
    )
    db.session.add(new_job)
    db.session.flush()

    if not deduct_credits_atomic(current_user.id, cost, job_id=new_job.id):
        db.session.rollback()
        return jsonify({'error': 'Insufficient credits'}), 402

    db.session.commit()

    try:
        process_dub.delay({
            'job_id': new_job.id,
            'file_key': file_key,
            'lang': lang,
            'voice_id': voice_id,
            'sample_b64': sample_b64,
            'engine': engine,
        })
    except Exception as e:
        logger.exception(f"Failed to enqueue: {e}")
        return jsonify({'error': 'Queue failed'}), 500

    return jsonify({'success': True, 'job_id': new_job.id, 'status': 'queued'}), 202


# ==========================================
# 🎬 /api/dubbing — احتفظت به للتوافق مع legacy (multipart)
# ==========================================
@app.route('/api/dubbing', methods=['POST'])
@token_required
def start_dubbing_legacy(current_user):
    """Legacy: يقبل multipart، يرفع للـ R2 ثم يستدعي نفس process_dub"""
    cost = int(os.environ.get('DUB_COST', 100))

    file = request.files.get('media_file')
    if not file:
        return jsonify({'error': 'No file'}), 400

    filename = secure_filename(file.filename or '')
    if not allowed_file(filename):
        return jsonify({'error': 'Unsupported file'}), 400

    if (current_user.credits or 0) < cost:
        return jsonify({'error': 'Insufficient credits'}), 402

    new_job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='pending',
        language=request.form.get('lang', 'ar'),
        method='dubbing',
        voice_id=request.form.get('voice_id', 'source'),
        credits_used=cost,
    )
    db.session.add(new_job)
    db.session.flush()

    if not deduct_credits_atomic(current_user.id, cost, job_id=new_job.id):
        db.session.rollback()
        return jsonify({'error': 'Insufficient credits'}), 402

    file_key = f"uploads/u{current_user.id}/{uuid.uuid4().hex}_{filename}"
    try:
        file.stream.seek(0)
        s3_client.upload_fileobj(file.stream, R2_BUCKET_NAME, file_key)
    except Exception as e:
        logger.exception(f"Upload failed: {e}")
        refund_credits(current_user.id, cost, new_job.id)
        return jsonify({'error': 'Upload failed'}), 500

    new_job.file_key = file_key
    new_job.status = 'processing'
    db.session.commit()

    try:
        process_dub.delay({
            'job_id': new_job.id,
            'file_key': file_key,
            'lang': new_job.language,
            'voice_id': new_job.voice_id,
            'sample_b64': request.form.get('sample_b64', ''),
        })
    except Exception as e:
        logger.exception(f"Enqueue failed: {e}")

    return jsonify({'success': True, 'job_id': new_job.id}), 202


# ==========================================
# 🎙️ /api/stt — تحويل الصوت لنص
# ==========================================
@app.route('/api/stt', methods=['POST'])
@token_required
@json_required
def start_stt(current_user):
    data = request.json or {}
    file_key = data.get('file_key')
    language = data.get('language', 'auto')
    mode = data.get('mode', 'fast')
    diarize = bool(data.get('diarize', False))
    translate = bool(data.get('translate', False))

    if not file_key:
        return jsonify({'error': 'file_key required'}), 400

    try:
        s3_client.head_object(Bucket=R2_BUCKET_NAME, Key=file_key)
    except Exception:
        return jsonify({'error': 'File not found'}), 404

    cost = int(os.environ.get('STT_COST', 30))
    if (current_user.credits or 0) < cost:
        return jsonify({'error': 'Insufficient credits'}), 402

    new_job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='pending',
        language=language,
        method='stt',
        engine=mode,
        file_key=file_key,
        credits_used=cost,
    )
    db.session.add(new_job)
    db.session.flush()

    if not deduct_credits_atomic(current_user.id, cost, job_id=new_job.id):
        db.session.rollback()
        return jsonify({'error': 'Insufficient credits'}), 402

    db.session.commit()

    try:
        process_stt.delay({
            'job_id': new_job.id,
            'file_key': file_key,
            'language': language,
            'mode': mode,
            'diarize': diarize,
            'translate': translate,
        })
    except Exception as e:
        logger.exception(f"STT enqueue failed: {e}")
        return jsonify({'error': 'Queue failed'}), 500

    return jsonify({'success': True, 'job_id': new_job.id, 'status': 'queued', 'mode': mode}), 202


# ==========================================
# ⚡ Quick TTS streaming
# ==========================================
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
        return jsonify({'error': 'Text missing'}), 400
    if len(text) > MAX_TTS_LENGTH:
        return jsonify({'error': 'Text too long'}), 400

    cost = 1
    if not deduct_credits_atomic(current_user.id, cost):
        return jsonify({'error': 'Insufficient credits'}), 402

    def generate():
        try:
            async def stream():
                comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        yield chunk["data"]

            loop = asyncio.new_event_loop()
            agen = stream()
            while True:
                try:
                    yield loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    break
            loop.close()
        except Exception as e:
            logger.exception(f"Edge-TTS error: {e}")

    response = Response(stream_with_context(generate()), mimetype="audio/mpeg")
    response.headers['X-Remaining-Credits'] = str(User.query.get(current_user.id).credits)
    response.headers['Access-Control-Expose-Headers'] = 'X-Remaining-Credits'
    return response


# ==========================================
# 🎯 Smart TTS
# ==========================================
@app.route('/api/tts', methods=['POST'])
@app.route('/api/tts/smart', methods=['POST'])
@token_required
@json_required
def start_smart_tts(current_user):
    data = request.json or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'Text missing'}), 400

    cost = 10
    new_job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='pending',
        language=data.get('lang', 'ar'),
        method='smart_tts',
        voice_id=data.get('voice_id', ''),
        credits_used=cost,
    )
    db.session.add(new_job)
    db.session.flush()

    if not deduct_credits_atomic(current_user.id, cost, job_id=new_job.id):
        db.session.rollback()
        return jsonify({'error': 'Insufficient credits'}), 402

    db.session.commit()

    try:
        process_smart_tts.delay({
            'job_id': new_job.id,
            'text': text,
            'lang': data.get('lang', 'ar'),
            'voice_id': data.get('voice_id', ''),
            'sample_b64': data.get('sample_b64', ''),
            'rate': data.get('rate', '+0%'),
            'pitch': data.get('pitch', '+0Hz'),
        })
    except Exception as e:
        logger.exception(f"smart_tts enqueue failed: {e}")

    return jsonify({'success': True, 'job_id': new_job.id}), 202


# ==========================================
# 📊 Job status + history
# ==========================================
@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def check_job(current_user, job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({'status': 'failed', 'error': 'Not authorized'}), 403

    return jsonify({
        'id': job.id,
        'status': job.status,
        'audio_url': job.output_url,
        'error': job.error_message,
        'lang': job.language,
        'method': job.method,
        'engine': job.engine,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'completed_at': job.completed_at.isoformat() if job.completed_at else None,
    })


@app.route('/api/jobs', methods=['GET'])
@token_required
def list_jobs(current_user):
    """ملفاتي"""
    jobs = DubbingJob.query.filter_by(user_id=current_user.id) \
        .order_by(DubbingJob.created_at.desc()).limit(100).all()
    return jsonify({
        'success': True,
        'jobs': [{
            'id': j.id,
            'method': j.method,
            'lang': j.language,
            'engine': j.engine,
            'status': j.status,
            'audio_url': j.output_url,
            'custom_name': j.custom_name,
            'folder_name': j.folder_name,
            'created_at': j.created_at.isoformat() if j.created_at else None,
        } for j in jobs]
    })


@app.route('/api/logout', methods=['POST'])
def logout():
    return jsonify({'success': True})


# ==========================================
# 🛠️ Migration endpoint (مؤقت — احذفه بعد الاستخدام!)
# ==========================================
@app.route('/api/admin/migrate-db', methods=['POST'])
def migrate_db():
    """⚠️ مؤقت: يحذف ويعيد إنشاء الجداول"""
    secret = request.headers.get('X-Admin-Secret', '')
    if secret != os.environ.get('ADMIN_SECRET', 'change-me-to-secret-2026'):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        with app.app_context():
            db.session.execute("DROP TABLE IF EXISTS credit_transactions CASCADE")
            db.session.execute("DROP TABLE IF EXISTS dubbing_jobs CASCADE")
            db.session.execute("DROP TABLE IF EXISTS users CASCADE")
            db.session.commit()
            db.create_all()
        return jsonify({'success': True, 'message': 'DB migrated'})
    except Exception as e:
        logger.exception("Migration failed")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
