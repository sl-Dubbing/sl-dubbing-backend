# server.py — النسخة النهائية V27.0
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

MAX_FILE_BYTES = 150 * 1024 * 1024  # 150MB

AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

_secret = os.environ.get('SECRET_KEY')
if not _secret:
    _is_debug = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')
    if _is_debug:
        _secret = 'dev-only-secret'
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
    else: CLOUDINARY_AVAILABLE = False
except Exception: CLOUDINARY_AVAILABLE = False

_executor = ThreadPoolExecutor(max_workers=int(os.environ.get('MAX_WORKERS', '5')))
MODAL_URL = os.environ.get("MODAL_URL", "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/")
if not MODAL_URL.endswith('/'): MODAL_URL += '/'

# ── المصادقة ───────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS': return f(*args, **kwargs)
        token = request.cookies.get('sl_auth_token')
        if not token: return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user = User.query.get(data.get('user_id'))
            if not user: raise ValueError("User not found")
            request.user = user
        except Exception: return jsonify({'error': 'Session expired'}), 401
        return f(*args, **kwargs)
    return decorated

def _make_cookie(user, is_new=False):
    token = jwt.encode({
        'user_id': user.id, 'sub': user.email,
        'iat': datetime.utcnow(),
        'exp': datetime.utcnow() + timedelta(hours=24)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    resp = make_response(jsonify({'success': True, 'user': user.to_dict(), 'is_new': is_new}))
    resp.set_cookie('sl_auth_token', token, httponly=True, secure=True, samesite='None', max_age=86400)
    return resp

# ── دوال العمليات الخلفية ───────────────────────────────────
def _refund_and_fail(job):
    try:
        u = User.query.get(job.user_id)
        if u and job.credits_used: u.credits += job.credits_used
        job.status = 'failed'
        db.session.commit()
    except Exception: db.session.rollback()

def _resolve_audio_url(result_data, prefix, job_id):
    if result_data.get("audio_url"): return result_data["audio_url"]
    audio_bytes = base64.b64decode(result_data.get("audio_base64", ""))
    local_path = AUDIO_DIR / f"{prefix}_{job_id}.mp3"
    with open(local_path, "wb") as f: f.write(audio_bytes)
    if CLOUDINARY_AVAILABLE:
        resp = cloudinary.uploader.upload(
            str(local_path), resource_type='auto',
            folder=f"sl-dubbing/{prefix}", public_id=f"{prefix}_{job_id}", overwrite=True
        )
        return resp.get('secure_url') or resp.get('url')
    host = os.environ.get("PUBLIC_HOST")
    fname = f"{prefix}_{job_id}.mp3"
    return f"https://{host}/api/file/{fname}" if host else f"/api/file/{fname}"

def _run_dub(job_id, file_path, lang, voice_id, voice_url, sample_b64, filename):
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        start = time.time()
        try:
            with open(file_path, "rb") as f: file_b64 = base64.b64encode(f.read()).decode()
            r = http_requests.post(MODAL_URL, json={
                "file_b64": file_b64, "filename": filename, "lang": lang,
                "voice_id": voice_id, "voice_url": voice_url, "sample_b64": sample_b64,
            }, timeout=1800)
            if r.status_code != 200: raise Exception(f"Factory HTTP {r.status_code}")
            data = r.json()
            if not data.get("success"): raise Exception(data.get("error", "Factory error"))
            job.output_url = _resolve_audio_url(data, "dub", job_id)
            job.status = 'completed'
            job.processing_time = time.time() - start
            db.session.commit()
        except Exception as e:
            logger.error(f"DUB {job_id} failed: {e}")
            _refund_and_fail(job)
        finally:
            try:
                if os.path.exists(file_path): os.remove(file_path)
            except Exception: pass

def _run_tts(job_id, text, lang, voice_id, voice_url, sample_b64):
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        start = time.time()
        try:
            r = http_requests.post(MODAL_URL + "tts", json={
                "text": text, "lang": lang, "voice_id": voice_id,
                "voice_url": voice_url, "sample_b64": sample_b64,
            }, timeout=1800)
            if r.status_code != 200: raise Exception(f"Factory HTTP {r.status_code}")
            data = r.json()
            if not data.get("success"): raise Exception(data.get("error", "TTS error"))
            job.output_url = _resolve_audio_url(data, "tts", job_id)
            job.status = 'completed'
            job.processing_time = time.time() - start
            job.extra_data = data.get("final_text", "")
            db.session.commit()
        except Exception as e:
            logger.error(f"TTS {job_id} failed: {e}")
            _refund_and_fail(job)

# ── Auth ───────────────────────────────────────────────────
@app.route('/api/auth/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    d = request.get_json(force=True, silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    password = d.get('password') or ''
    if not email or not password: return jsonify({'success': False, 'error': 'Missing data'}), 400
    if User.query.filter_by(email=email).first(): return jsonify({'success': False, 'error': 'Email exists'}), 400
    user = User(email=email, name=email.split('@')[0], auth_method='email', credits=50000)
    user.set_password(password)
    db.session.add(user); db.session.commit()
    return _make_cookie(user, is_new=True)

@app.route('/api/auth/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    d = request.get_json(force=True, silent=True) or {}
    user = User.query.filter_by(email=(d.get('email') or '').strip().lower()).first()
    if not user or not user.check_password(d.get('password')): return jsonify({'success': False, 'error': 'Invalid login'}), 401
    return _make_cookie(user)

@app.route('/api/auth/logout', methods=['POST', 'OPTIONS'])
def logout():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('sl_auth_token', '', expires=0, httponly=True, secure=True, samesite='None')
    return resp

# ── Voices ─────────────────────────────────────────────────
@app.route('/api/voices', methods=['GET'])
def list_voices():
    voices = []
    if CLOUDINARY_AVAILABLE:
        try:
            result = cloudinary.api.resources(type="upload", prefix="sl_voices/", resource_type="video")
            voices = [{"name": res['public_id'].split('/')[-1], "url": res['secure_url']} for res in result.get('resources', [])]
        except Exception: pass
    if not voices: voices = [{"name": "muhammad_ar", "url": "https://res.cloudinary.com/dxbmvzsiz/video/upload/v1712611200/sl_voices/muhammad_ar.wav"}]
    return jsonify({"success": True, "voices": voices})

# ── Dubbing & TTS ──────────────────────────────────────────
@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("10 per minute")
def dub():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    media_file = request.files.get('media_file')
    if not media_file: return jsonify({'success': False, 'error': 'File missing'}), 400
    user = request.user
    if user.credits < 100: return jsonify({'success': False, 'error': 'No credits'}), 402

    lang = request.form.get('lang', 'ar')
    voice_id = request.form.get('voice_id', '')
    voice_url = request.form.get('voice_url', '')
    sample_b64 = request.form.get('sample_b64', '')

    job_id = str(uuid.uuid4())
    filename = secure_filename(media_file.filename)
    in_path = AUDIO_DIR / f"in_{job_id}_{filename}"
    media_file.save(in_path)

    voice_mode = voice_id or ('xtts' if (voice_url or sample_b64) else 'source')
    job = DubbingJob(id=job_id, user_id=user.id, language=lang, voice_mode=voice_mode, method='dub', credits_used=100, status='processing')
    user.credits -= 100
    db.session.add(job); db.session.commit()
    _executor.submit(_run_dub, job_id, str(in_path), lang, voice_id, voice_url, sample_b64, filename)
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'}), 200

@app.route('/api/tts', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("20 per minute")
def tts():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    d = request.get_json(force=True, silent=True) or {}
    user = request.user
    text = (d.get('text') or '').strip()
    if not text: return jsonify({'success': False, 'error': 'Text missing'}), 400
    if user.credits < 50: return jsonify({'success': False, 'error': 'No credits'}), 402

    lang = d.get('lang', 'en')
    voice_id = d.get('voice_id', 'source')
    voice_url = d.get('voice_url', '')
    sample_b64 = d.get('sample_b64', '')

    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, language=lang, voice_mode=voice_id, method='tts', credits_used=50, status='processing')
    user.credits -= 50
    db.session.add(job); db.session.commit()
    _executor.submit(_run_tts, job_id, text, lang, voice_id, voice_url, sample_b64)
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'}), 200

# ── SSE Progress ───────────────────────────────────────────
@app.route('/api/progress/<job_id>', methods=['GET', 'OPTIONS'])
@require_auth
def get_progress(job_id):
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    chk = DubbingJob.query.get(job_id)
    if not chk or chk.user_id != request.user.id: return jsonify({'error': 'Access Denied'}), 403

    def stream():
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job: break
                progress = 50 if job.status == 'processing' else (100 if job.status == 'completed' else 0)
                payload = {
                    "status": "done" if job.status == 'completed' else job.status,
                    "progress": progress,
                    "audio_url": job.output_url,
                    "final_text": getattr(job, 'extra_data', '') or ''
                }
                yield f"data: {json.dumps(payload)}\n\n"
                if job.status in ('completed', 'failed', 'error'): break
            time.sleep(2)
    return Response(stream(), mimetype='text/event-stream')

@app.route('/api/user', methods=['GET'])
@require_auth
def get_user(): return jsonify({'success': True, 'user': request.user.to_dict()}), 200

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
