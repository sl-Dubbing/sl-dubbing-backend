# server.py — النسخة المُصلحة والمتوافقة
import os
import uuid
import logging
import time
import json
import threading
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, make_response, send_file, Response
from flask_cors import CORS
from dotenv import load_dotenv
import jwt
from functools import wraps
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import base64

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

load_dotenv()

# ============================================================
# إعداد التطبيق
# ============================================================
DEBUG = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# [FIX-1] SECRET_KEY إلزامي في الإنتاج — لا fallback ضعيف
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    if DEBUG:
        _secret = "dev-only-secret-key-not-for-production"
        logger.warning("SECRET_KEY not set — using dev fallback. Never do this in production!")
    else:
        raise RuntimeError("SECRET_KEY environment variable is required in production!")

# [FIX-2] Google Client ID من البيئة فقط — لا يُكتب في الكود أبداً
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
if not GOOGLE_CLIENT_ID:
    logger.warning("GOOGLE_CLIENT_ID not set — Google login will be disabled.")

ALLOWED_ORIGINS = [
    'https://sl-dubbing.github.io',
    'http://localhost:5500',
    'http://127.0.0.1:5500',
    'http://localhost:3000',
]

# [FIX-3] حد حجم الملف متطابق مع factory.py (150MB)
MAX_FILE_BYTES = 150 * 1024 * 1024

AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['DEBUG'] = DEBUG
app.config['SECRET_KEY'] = _secret

DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)

# [FIX-4] Rate limiting مخصص للعمليات الثقيلة
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per day"],
    storage_uri="memory://"
)

from models import db, User, DubbingJob, CreditTransaction
db.init_app(app)

try:
    import cloudinary
    import cloudinary.uploader
    if os.getenv('CLOUDINARY_NAME'):
        cloudinary.config(
            cloud_name=os.getenv('CLOUDINARY_NAME'),
            api_key=os.getenv('CLOUDINARY_API_KEY'),
            api_secret=os.getenv('CLOUDINARY_API_SECRET'),
            secure=True
        )
        CLOUDINARY_AVAILABLE = True
    else:
        CLOUDINARY_AVAILABLE = False
except Exception:
    CLOUDINARY_AVAILABLE = False

# [FIX-5] ThreadPoolExecutor بدلاً من Thread مفتوح لكل طلب
_executor = ThreadPoolExecutor(max_workers=int(os.environ.get('MAX_WORKERS', '5')))

# ============================================================
# المصادقة
# ============================================================
def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS':
            return f(*args, **kwargs)
        token = request.cookies.get('sl_auth_token')
        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user = User.query.get(data.get('user_id'))
            if not user:
                raise ValueError("User not found")
            request.user = user
        except Exception:
            return jsonify({'error': 'Session expired'}), 401
        return f(*args, **kwargs)
    return decorated_function

def generate_auth_response(user, is_new=False):
    token = jwt.encode({
        'user_id': user.id,
        'sub': user.email,
        'iat': datetime.utcnow(),
        'exp': datetime.utcnow() + timedelta(hours=24)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    resp = make_response(jsonify({'success': True, 'user': user.to_dict(), 'is_new': is_new}))
    resp.set_cookie(
        'sl_auth_token', token,
        httponly=True, secure=True, samesite='None',
        max_age=24 * 60 * 60
    )
    return resp

# ============================================================
# دوال مساعدة مشتركة
# ============================================================
def _upload_audio_fallback(local_path, prefix, job_id):
    """رفع الملف إلى Cloudinary أو استخدام رابط محلي — خطة بديلة فقط."""
    if CLOUDINARY_AVAILABLE:
        resp = cloudinary.uploader.upload(
            str(local_path),
            resource_type='auto',
            folder=f"sl-dubbing/{prefix}",
            public_id=f"{prefix}_{job_id}",
            overwrite=True
        )
        return resp.get('secure_url') or resp.get('url')
    PUBLIC_HOST = os.environ.get("PUBLIC_HOST")
    filename = f"{prefix}_{job_id}.mp3"
    return f"https://{PUBLIC_HOST}/api/file/{filename}" if PUBLIC_HOST else f"/api/file/{filename}"

def _handle_job_failure(job, exc):
    """معالجة فشل الوظيفة وإعادة الرصيد للمستخدم مع تسجيل الخطأ."""
    logger.error(f"Job {job.id if job else 'unknown'} failed: {exc}", exc_info=True)
    try:
        if job:
            job.status = 'failed'
            db.session.add(job)
            u = User.query.get(job.user_id)
            if u and job.credits_used:
                u.credits += job.credits_used
            db.session.commit()
    except Exception as db_exc:
        logger.error(f"DB rollback error: {db_exc}")
        db.session.rollback()

# ============================================================
# [FIX-6] دالة workflow موحدة بدلاً من دالتين متكررتين
# ============================================================
def _run_workflow(job_id, modal_url, payload, is_tts=False):
    """دالة خلفية موحدة لـ dubbing و TTS."""
    with app.app_context():
        job = None
        start_ts = time.time()
        try:
            job = DubbingJob.query.get(job_id)
            if not job:
                logger.error(f"Job {job_id} not found in DB")
                return

            response = requests.post(modal_url, json=payload, timeout=1800)

            if response.status_code != 200:
                raise Exception(f"Modal returned status {response.status_code}: {response.text[:200]}")

            result_data = response.json()
            if not result_data.get("success"):
                raise Exception(f"Factory Error: {result_data.get('error')}")

            # الأولوية: رابط GCS الجاهز من Modal
            if "audio_url" in result_data:
                audio_url = result_data["audio_url"]
            else:
                # خطة بديلة: Base64 قديم
                audio_bytes = base64.b64decode(result_data.get("audio_base64", ""))
                prefix = "tts" if is_tts else "dub"
                local_path = AUDIO_DIR / f"{prefix}_{job_id}.mp3"
                with open(local_path, "wb") as f:
                    f.write(audio_bytes)
                audio_url = _upload_audio_fallback(local_path, prefix, job_id)

            job.output_url = audio_url
            job.status = 'completed'
            job.processing_time = time.time() - start_ts

            # [FIX-7] حفظ final_text في قاعدة البيانات بدلاً من الذاكرة فقط
            if is_tts and result_data.get("final_text"):
                job.extra_data = result_data.get("final_text", "")

            db.session.add(job)
            db.session.commit()
            logger.info(f"Job {job_id} completed in {job.processing_time:.1f}s → {audio_url}")

            # تنظيف ملف المدخل المحلي إن وجد
            input_file = payload.get('_local_file_path')
            if input_file and os.path.exists(input_file):
                try:
                    os.remove(input_file)
                except Exception:
                    pass

        except Exception as exc:
            _handle_job_failure(job, exc)

# ============================================================
# مسارات المصادقة
# ============================================================
@app.route('/api/auth/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or not password:
        return jsonify({'success': False, 'error': 'البريد وكلمة المرور مطلوبان'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'success': False, 'error': 'البريد مسجل مسبقاً'}), 400
    user = User(email=email, name=email.split('@')[0], auth_method='email', credits=50000)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return generate_auth_response(user, True)

@app.route('/api/auth/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True) or {}
    user = User.query.filter_by(email=(data.get('email') or '').strip().lower()).first()
    if not user or not user.check_password(data.get('password')):
        return jsonify({'success': False, 'error': 'بيانات الدخول غير صحيحة'}), 401
    return generate_auth_response(user)

@app.route('/api/auth/google', methods=['POST', 'OPTIONS'])
def google_login():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    if not GOOGLE_CLIENT_ID:
        return jsonify({'success': False, 'error': 'Google login is not configured'}), 503
    data = request.get_json(force=True, silent=True) or {}
    token = data.get('credential')
    try:
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        email = idinfo['email']
        name = idinfo.get('name', email.split('@')[0])
        user = User.query.filter_by(email=email).first()
        is_new = False
        if not user:
            user = User(email=email, name=name, auth_method='google', credits=50000)
            db.session.add(user)
            db.session.commit()
            is_new = True
        user.last_login = datetime.utcnow()
        db.session.commit()
        return generate_auth_response(user, is_new=is_new)
    except Exception as e:
        logger.warning(f"Google login failed: {e}")
        return jsonify({'success': False, 'error': 'Token verification failed'}), 401

@app.route('/api/auth/logout', methods=['POST', 'OPTIONS'])
def logout():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('sl_auth_token', '', expires=0, httponly=True, secure=True, samesite='None')
    return resp

# ============================================================
# مسارات الدبلجة والـ TTS
# ============================================================
@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("10 per minute")
def dub():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200

    media_file = request.files.get('media_file')
    if not media_file:
        return jsonify({'success': False, 'error': 'الملف مطلوب'}), 400

    # [FIX-8] التحقق من حجم الملف قبل الحفظ
    media_file.seek(0, 2)
    file_size = media_file.tell()
    media_file.seek(0)
    if file_size > MAX_FILE_BYTES:
        return jsonify({'success': False, 'error': 'حجم الملف يتجاوز 150MB'}), 413

    user = request.user
    if user.credits < 100:
        return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402

    job_id = str(uuid.uuid4())
    filename = secure_filename(media_file.filename)
    input_path = AUDIO_DIR / f"in_{job_id}_{filename}"
    media_file.save(input_path)

    voice_id = request.form.get('voice_mode', 'source')
    lang = request.form.get('lang', 'ar')

    job = DubbingJob(
        id=job_id, user_id=user.id,
        language=lang, voice_mode=voice_id,
        credits_used=100, status='processing', method='dub'
    )
    user.credits -= 100
    db.session.add(job)
    db.session.commit()

    MODAL_URL = os.environ.get("MODAL_URL", "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/")
    if not MODAL_URL.endswith('/'):
        MODAL_URL += '/'

    with open(input_path, "rb") as f:
        file_b64 = base64.b64encode(f.read()).decode('utf-8')

    modal_payload = {
        "file_b64": file_b64,
        "filename": filename,
        "lang": lang,
        "voice_mode": voice_id,
        "voice_id": voice_id,   # متوافق مع factory.py
        "_local_file_path": str(input_path),
    }

    _executor.submit(_run_workflow, job_id, MODAL_URL, modal_payload, False)
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'}), 200

@app.route('/api/tts', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("20 per minute")
def tts():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200

    data = request.get_json(force=True, silent=True) or {}
    user = request.user

    if user.credits < 50:
        return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402

    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'success': False, 'error': 'النص مطلوب'}), 400

    job_id = str(uuid.uuid4())
    lang = data.get('lang', 'en')
    voice_id = data.get('voice_id', 'source')

    job = DubbingJob(
        id=job_id, user_id=user.id,
        language=lang, voice_mode=voice_id,
        credits_used=50, status='processing', method='tts'
    )
    user.credits -= 50
    db.session.add(job)
    db.session.commit()

    MODAL_URL = os.environ.get("MODAL_URL", "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/")
    if not MODAL_URL.endswith('/'):
        MODAL_URL += '/'

    modal_payload = {
        "text": text,
        "lang": lang,
        "voice_id": voice_id,
        "sample_b64": data.get('sample_b64', ''),
    }

    _executor.submit(_run_workflow, job_id, MODAL_URL + "tts", modal_payload, True)
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'}), 200

# ============================================================
# [FIX-9] SSE مع مصادقة وتحقق من ملكية الـ job
# ============================================================
@app.route('/api/progress/<job_id>', methods=['GET', 'OPTIONS'])
@require_auth
def get_progress(job_id):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200

    # تحقق من ملكية الـ job
    job_check = DubbingJob.query.get(job_id)
    if not job_check:
        return jsonify({'error': 'Job not found'}), 404
    if job_check.user_id != request.user.id:
        return jsonify({'error': 'Access Denied'}), 403

    def generate():
        while True:
            with app.app_context():
                current_job = DubbingJob.query.get(job_id)
                if not current_job:
                    yield f"data: {json.dumps({'status': 'error', 'error': 'Job not found'})}\n\n"
                    break

                progress_val = 50 if current_job.status == 'processing' else (
                    100 if current_job.status == 'completed' else 0
                )
                msg = "AI is working..." if current_job.status == 'processing' else current_job.status

                payload = {
                    "status": "done" if current_job.status == 'completed' else current_job.status,
                    "progress": progress_val,
                    "message": msg,
                    "audio_url": current_job.output_url,
                    # [FIX-7] final_text من قاعدة البيانات مباشرة
                    "final_text": getattr(current_job, 'extra_data', '') or '',
                }
                yield f"data: {json.dumps(payload)}\n\n"

                if current_job.status in ['completed', 'failed', 'error']:
                    break
            time.sleep(2)

    return Response(generate(), mimetype='text/event-stream')

# ============================================================
# مسارات مساعدة
# ============================================================
@app.route('/api/job/<job_id>', methods=['GET'])
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != request.user.id:
        return jsonify({'error': 'Not found or Access Denied'}), 404
    return jsonify({
        'success': True,
        'job_id': job.id,
        'status': job.status,
        'audio_url': job.output_url,
        'method': job.method,
        'processing_time': job.processing_time,
        'credits_used': job.credits_used,
        'remaining_credits': request.user.credits,
    }), 200

@app.route('/api/user', methods=['GET'])
@require_auth
def get_current_user():
    return jsonify({'success': True, 'user': request.user.to_dict()}), 200

@app.route('/api/file/<filename>')
def get_file(filename):
    p = AUDIO_DIR / filename
    return send_file(str(p)) if p.exists() else (jsonify({'error': '404'}), 404)

# [FIX-10] Health check لـ Railway وأي load balancer
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()}), 200

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
