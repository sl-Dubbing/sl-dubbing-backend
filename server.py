# server.py — النسخة النهائية V27.0
# يجمع: إصلاح DB من V26 + جميع endpoints + أمان + ThreadPoolExecutor

import os, uuid, logging, time, json, base64
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
import requests as http_requests
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

load_dotenv()

# ── إعداد التطبيق ──────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = [
    'https://sl-dubbing.github.io',
    'http://localhost:5500',
    'http://127.0.0.1:5500',
]

MAX_FILE_BYTES = 150 * 1024 * 1024  # 150MB — متطابق مع factory.py

AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# SECRET_KEY — يتوقف الخادم إن غاب في الإنتاج
_secret = os.environ.get('SECRET_KEY')
if not _secret:
    _is_debug = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')
    if _is_debug:
        _secret = 'dev-only-secret'
        logger.warning("SECRET_KEY not set — using dev fallback. Never do this in production!")
    else:
        raise RuntimeError("SECRET_KEY environment variable is required in production!")

app.config['SECRET_KEY'] = _secret
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

_db_url = os.environ.get('DATABASE_URL', '')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url

CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)
limiter = Limiter(get_remote_address, app=app, default_limits=["1000 per day"], storage_uri="memory://")

from models import db, User, DubbingJob
db.init_app(app)

# Cloudinary — اختياري، خطة بديلة فقط
try:
    import cloudinary, cloudinary.uploader, cloudinary.api
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

# ThreadPoolExecutor — حد 5 عمال، قابل للتعديل من env
_executor = ThreadPoolExecutor(max_workers=int(os.environ.get('MAX_WORKERS', '5')))

MODAL_URL = os.environ.get(
    "MODAL_URL",
    "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/"
)
if not MODAL_URL.endswith('/'):
    MODAL_URL += '/'

# ── المصادقة ───────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
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
    return decorated

def _make_cookie(user, is_new=False):
    token = jwt.encode({
        'user_id': user.id, 'sub': user.email,
        'iat': datetime.utcnow(),
        'exp': datetime.utcnow() + timedelta(hours=24)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    resp = make_response(jsonify({'success': True, 'user': user.to_dict(), 'is_new': is_new}))
    resp.set_cookie('sl_auth_token', token,
                    httponly=True, secure=True, samesite='None', max_age=86400)
    return resp

# ── دوال العمليات الخلفية ───────────────────────────────────
def _refund_and_fail(job):
    """إعادة الرصيد وتعليم الـ job بالفشل."""
    try:
        u = User.query.get(job.user_id)
        if u and job.credits_used:
            u.credits += job.credits_used
        job.status = 'failed'
        db.session.commit()
    except Exception:
        db.session.rollback()

def _resolve_audio_url(result_data, prefix, job_id):
    """
    يأخذ audio_url من factory إن وُجد.
    إن لم يوجد يحفظ base64 ويرفعه إلى Cloudinary أو يعيد رابطاً محلياً.
    """
    if result_data.get("audio_url"):
        return result_data["audio_url"]

    audio_bytes = base64.b64decode(result_data.get("audio_base64", ""))
    local_path = AUDIO_DIR / f"{prefix}_{job_id}.mp3"
    with open(local_path, "wb") as f:
        f.write(audio_bytes)

    if CLOUDINARY_AVAILABLE:
        resp = cloudinary.uploader.upload(
            str(local_path), resource_type='auto',
            folder=f"sl-dubbing/{prefix}",
            public_id=f"{prefix}_{job_id}", overwrite=True
        )
        return resp.get('secure_url') or resp.get('url')

    host = os.environ.get("PUBLIC_HOST")
    fname = f"{prefix}_{job_id}.mp3"
    return f"https://{host}/api/file/{fname}" if host else f"/api/file/{fname}"

def _run_dub(job_id, file_path, lang, voice_id, voice_url, sample_b64, filename):
    """دبلجة في الخلفية."""
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        start = time.time()
        try:
            with open(file_path, "rb") as f:
                file_b64 = base64.b64encode(f.read()).decode()

            # إرسال voice_id و voice_url و sample_b64 معاً
            # factory يختار المتاح بالترتيب: sample_b64 → voice_id → voice_url
            r = http_requests.post(MODAL_URL, json={
                "file_b64":   file_b64,
                "filename":   filename,
                "lang":       lang,
                "voice_id":   voice_id,
                "voice_url":  voice_url,
                "sample_b64": sample_b64,
            }, timeout=1800)

            if r.status_code != 200:
                raise Exception(f"Factory HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if not data.get("success"):
                raise Exception(data.get("error", "Factory error"))

            job.output_url = _resolve_audio_url(data, "dub", job_id)
            job.status = 'completed'
            job.processing_time = time.time() - start
            db.session.commit()
            logger.info(f"DUB {job_id} completed in {job.processing_time:.1f}s")

        except Exception as e:
            logger.error(f"DUB {job_id} failed: {e}")
            _refund_and_fail(job)
        finally:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass

def _run_tts(job_id, text, lang, voice_id, voice_url, sample_b64):
    """TTS في الخلفية."""
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        start = time.time()
        try:
            r = http_requests.post(MODAL_URL + "tts", json={
                "text":       text,
                "lang":       lang,
                "voice_id":   voice_id,
                "voice_url":  voice_url,
                "sample_b64": sample_b64,
            }, timeout=1800)

            if r.status_code != 200:
                raise Exception(f"Factory HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if not data.get("success"):
                raise Exception(data.get("error", "TTS error"))

            job.output_url = _resolve_audio_url(data, "tts", job_id)
            job.status = 'completed'
            job.processing_time = time.time() - start
            # الترجمة في DB مباشرة — لا تضيع عند إعادة تشغيل Railway
            job.extra_data = data.get("final_text", "")
            db.session.commit()
            logger.info(f"TTS {job_id} completed in {job.processing_time:.1f}s")

        except Exception as e:
            logger.error(f"TTS {job_id} failed: {e}")
            _refund_and_fail(job)

# ── Auth Endpoints ──────────────────────────────────────────
@app.route('/api/auth/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    d = request.get_json(force=True, silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    password = d.get('password') or ''
    if not email or not password:
        return jsonify({'success': False, 'error': 'البريد وكلمة المرور مطلوبان'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'success': False, 'error': 'البريد مسجّل مسبقاً'}), 400
    user = User(email=email, name=email.split('@')[0],
                auth_method='email', credits=50000)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return _make_cookie(user, is_new=True)

@app.route('/api/auth/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    d = request.get_json(force=True, silent=True) or {}
    user = User.query.filter_by(
        email=(d.get('email') or '').strip().lower()
    ).first()
    if not user or not user.check_password(d.get('password')):
        return jsonify({'success': False, 'error': 'بيانات الدخول غير صحيحة'}), 401
    return _make_cookie(user)

@app.route('/api/auth/google', methods=['POST', 'OPTIONS'])
def google_login():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    client_id = os.environ.get('GOOGLE_CLIENT_ID')
    if not client_id:
        return jsonify({'success': False, 'error': 'Google login not configured'}), 503
    d = request.get_json(force=True, silent=True) or {}
    try:
        info = id_token.verify_oauth2_token(
            d.get('credential'), google_requests.Request(), client_id
        )
        email = info['email']
        user = User.query.filter_by(email=email).first()
        is_new = False
        if not user:
            user = User(email=email,
                        name=info.get('name', email.split('@')[0]),
                        auth_method='google', credits=50000)
            db.session.add(user)
            db.session.commit()
            is_new = True
        user.last_login = datetime.utcnow()
        db.session.commit()
        return _make_cookie(user, is_new=is_new)
    except Exception as e:
        logger.warning(f"Google login failed: {e}")
        return jsonify({'success': False, 'error': 'Token verification failed'}), 401

@app.route('/api/auth/logout', methods=['POST', 'OPTIONS'])
def logout():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('sl_auth_token', '', expires=0,
                    httponly=True, secure=True, samesite='None')
    return resp

# ── Voices ─────────────────────────────────────────────────
@app.route('/api/voices', methods=['GET'])
def list_voices():
    """[V26] قائمة الأصوات من Cloudinary أو fallback ثابت."""
    voices = []
    if CLOUDINARY_AVAILABLE:
        try:
            result = cloudinary.api.resources(
                type="upload", prefix="sl_voices/", resource_type="video"
            )
            for res in result.get('resources', []):
                voices.append({
                    "name": res['public_id'].split('/')[-1],
                    "url":  res['secure_url']
                })
        except Exception as e:
            logger.warning(f"Cloudinary voices error: {e}")
    if not voices:
        voices = [{"name": "muhammad_ar",
                   "url": "https://res.cloudinary.com/dxbmvzsiz/video/upload/v1712611200/sl_voices/muhammad_ar.wav"}]
    return jsonify({"success": True, "voices": voices})

# ── Dubbing ────────────────────────────────────────────────
@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("10 per minute")
def dub():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200

    media_file = request.files.get('media_file')
    if not media_file:
        return jsonify({'success': False, 'error': 'الملف مطلوب'}), 400

    # تحقق من الحجم في server قبل الإرسال إلى Modal
    media_file.seek(0, 2)
    size = media_file.tell()
    media_file.seek(0)
    if size > MAX_FILE_BYTES:
        return jsonify({'success': False, 'error': 'حجم الملف يتجاوز 150MB'}), 413

    user = request.user
    if user.credits < 100:
        return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402

    lang       = request.form.get('lang', 'ar')
    voice_id   = request.form.get('voice_id', '')       # اسم الصوت من القائمة
    voice_url  = request.form.get('voice_url', '')      # رابط Cloudinary مباشر
    sample_b64 = request.form.get('sample_b64', '')     # عينة مرفوعة

    job_id   = str(uuid.uuid4())
    filename = secure_filename(media_file.filename)
    in_path  = AUDIO_DIR / f"in_{job_id}_{filename}"
    media_file.save(in_path)

    # [V26-FIX] حفظ language و voice_mode و method صح في DB
    voice_mode = voice_id or ('xtts' if (voice_url or sample_b64) else 'source')
    job = DubbingJob(
        id=job_id, user_id=user.id,
        language=lang,
        voice_mode=voice_mode,
        method='dub',
        credits_used=100,
        status='processing'
    )
    user.credits -= 100
    db.session.add(job)
    db.session.commit()

    _executor.submit(_run_dub, job_id, str(in_path),
                     lang, voice_id, voice_url, sample_b64, filename)

    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'}), 200

# ── TTS ────────────────────────────────────────────────────
@app.route('/api/tts', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("20 per minute")
def tts():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200

    d = request.get_json(force=True, silent=True) or {}
    user = request.user

    text = (d.get('text') or '').strip()
    if not text:
        return jsonify({'success': False, 'error': 'النص مطلوب'}), 400
    if user.credits < 50:
        return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402

    lang       = d.get('lang', 'en')
    voice_id   = d.get('voice_id', 'source')
    voice_url  = d.get('voice_url', '')
    sample_b64 = d.get('sample_b64', '')

    job_id = str(uuid.uuid4())
    # [V26-FIX] حفظ language و voice_mode و method صح
    job = DubbingJob(
        id=job_id, user_id=user.id,
        language=lang,
        voice_mode=voice_id,
        method='tts',
        credits_used=50,
        status='processing'
    )
    user.credits -= 50
    db.session.add(job)
    db.session.commit()

    _executor.submit(_run_tts, job_id, text,
                     lang, voice_id, voice_url, sample_b64)

    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'}), 200

# ── SSE Progress ───────────────────────────────────────────
@app.route('/api/progress/<job_id>', methods=['GET', 'OPTIONS'])
@require_auth
def get_progress(job_id):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200

    chk = DubbingJob.query.get(job_id)
    if not chk:
        return jsonify({'error': 'Job not found'}), 404
    if chk.user_id != request.user.id:
        return jsonify({'error': 'Access Denied'}), 403

    def stream():
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job:
                    yield f"data: {json.dumps({'status':'error','error':'Job not found'})}\n\n"
                    break

                progress = 50 if job.status == 'processing' else (
                    100 if job.status == 'completed' else 0)

                payload = {
                    "status":     "done" if job.status == 'completed' else job.status,
                    "progress":   progress,
                    "message":    "AI is working..." if job.status == 'processing' else job.status,
                    "audio_url":  job.output_url,
                    # final_text من DB — يبقى حتى بعد restart
                    "final_text": getattr(job, 'extra_data', '') or '',
                }
                yield f"data: {json.dumps(payload)}\n\n"

                if job.status in ('completed', 'failed', 'error'):
                    break
            time.sleep(2)

    return Response(stream(), mimetype='text/event-stream')

# ── Job / User / File / Health ─────────────────────────────
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
def get_user():
    return jsonify({'success': True, 'user': request.user.to_dict()}), 200

@app.route('/api/file/<filename>')
def get_file(filename):
    p = AUDIO_DIR / filename
    return send_file(str(p)) if p.exists() else (jsonify({'error': '404'}), 404)

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'ts': datetime.utcnow().isoformat()}), 200

# ── Bootstrap ──────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
