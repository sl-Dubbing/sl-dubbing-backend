# server.py
import os
import uuid
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask, request, jsonify, make_response, send_file
from flask_cors import CORS
from dotenv import load_dotenv
import jwt
from functools import wraps
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import requests
import base64

# --- Google Auth Imports ---
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

load_dotenv()

DEBUG = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = ['https://sl-dubbing.github.io', 'http://localhost:5500', 'http://127.0.0.1:5500']
ALLOWED_LANGS = ['ar', 'en', 'es', 'fr', 'de', 'it', 'pt', 'tr', 'ru', 'zh', 'ja', 'ko', 'yue', 'hi', 'ur']
ALLOWED_VOICE_MODES = ['gtts', 'xtts', 'cosy', 'source']
AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

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

from models import db, User, DubbingJob, CreditTransaction
db.init_app(app)

# Cloudinary optional import
try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_AVAILABLE = True
    CLOUDINARY_NAME = os.getenv('dxbmvzsiz')
    CLOUDINARY_API_KEY = os.getenv('0wmWqlKFRVmqbE8lBbYDYeUQ24E')
    CLOUDINARY_API_SECRET = os.getenv('295811796272148')
    if CLOUDINARY_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
        cloudinary.config(cloud_name=CLOUDINARY_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET, secure=True)
    else:
        CLOUDINARY_AVAILABLE = False
except Exception:
    CLOUDINARY_AVAILABLE = False

# ----------------- Auth helpers -----------------
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
            user_id = data.get('user_id')
            user = User.query.get(user_id)
            if not user:
                raise ValueError("User not found")
            request.user = user
        except Exception:
            return jsonify({'error': 'Session expired or invalid.'}), 401
        return f(*args, **kwargs)
    return decorated_function

def generate_auth_response(user, is_new=False):
    token = jwt.encode({
        'user_id': user.id,
        'sub': user.email,
        'iat': datetime.utcnow(),
        'exp': datetime.utcnow() + timedelta(hours=2)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    resp = make_response(jsonify({'success': True, 'user': user.to_dict(), 'is_new': is_new}))
    resp.set_cookie('sl_auth_token', token, httponly=True, secure=True, samesite='None', max_age=2*60*60)
    return resp

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
            time.sleep(2 ** attempt)
    raise last_exc

# ----------------- Background processing (The GPU Connection) -----------------
def process_full_workflow(payload):
    job_id = payload.get('job_id')
    user_id = payload.get('user_id')
    start_ts = time.time()

    try:
        job = DubbingJob.query.get(job_id)
        user = User.query.get(user_id)
        
        yt_url = payload.get('yt_url')
        if not yt_url:
            raise ValueError("في الوقت الحالي، النظام يدعم روابط يوتيوب فقط.")

        logger.info(f"[{job_id}] Sending request to Modal GPU Factory...")

        MODAL_URL = "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/"
        
        response = requests.post(MODAL_URL, json={
            "yt_url": yt_url,
            "lang": payload.get('lang', 'ar'),
            "voice_mode": payload.get('voice_mode', 'xtts'),
            "voice_url": payload.get('voice_url', ''),
            "openai_key": os.environ.get("OPENAI_API_KEY", "")
        }, timeout=600)
        
        result_data = response.json()
        
        if not result_data.get("success"):
            raise Exception(f"خطأ في المصنع: {result_data.get('error')}")

        logger.info(f"[{job_id}] Received processed audio from GPU!")
        
        audio_base64 = result_data.get("audio_base64")
        audio_bytes = base64.b64decode(audio_base64)
        
        mp_path = AUDIO_DIR / f"dub_{job_id}.mp3"
        with open(mp_path, "wb") as f:
            f.write(audio_bytes)

        if CLOUDINARY_AVAILABLE:
            upload_resp = cloudinary_upload_with_retries(str(mp_path), public_id=f"dub_{job_id}")
            audio_url = upload_resp.get('secure_url') or upload_resp.get('url')
        else:
            audio_url = f"file://{mp_path}"

        job.output_url = audio_url
        job.status = 'completed'
        job.processing_time = time.time() - start_ts
        job.method = payload.get('voice_mode', 'xtts')
        db.session.add(job)
        db.session.commit()
        logger.info(f"[{job_id}] Completed successfully!")

    except Exception as exc:
        logger.error(f"[{job_id}] Processing failed: {exc}")
        try:
            job = DubbingJob.query.get(job_id) if job_id else None
            if job:
                job.status = 'failed'
                db.session.add(job)
            if job and job.credits_used:
                u = User.query.get(job.user_id)
                if u:
                    u.credits += job.credits_used
                    db.session.add(CreditTransaction(user_id=u.id, transaction_type='refund', amount=job.credits_used, reason='Dubbing failed'))
            db.session.commit()
        except Exception:
            db.session.rollback()

# ----------------- Routes -----------------
@app.route('/api/auth/register', methods=['POST', 'OPTIONS'])
@limiter.limit("10 per minute")
def register():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True)
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or not password: return jsonify({'success': False, 'error': 'Email and password required'}), 400
    if User.query.filter_by(email=email).first(): return jsonify({'success': False, 'error': 'Email already registered'}), 400
    user = User(email=email, name=email.split('@')[0][:50], auth_method='email', credits=50000)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return generate_auth_response(user, True)

@app.route('/api/auth/login', methods=['POST', 'OPTIONS'])
@limiter.limit("10 per minute")
def login():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True)
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password): return jsonify({'success': False, 'error': 'Invalid credentials'}), 401
    user.last_login = datetime.utcnow()
    db.session.commit()
    return generate_auth_response(user)

# === هذه هي الدالة المفقودة التي أعدناها ===
@app.route('/api/auth/google', methods=['POST', 'OPTIONS'])
@limiter.limit("10 per minute")
def google_login():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True)
    if not data or 'credential' not in data: return jsonify({'success': False, 'error': 'Missing credential'}), 400
    token = data['credential']
    try:
        CLIENT_ID = "497619073475-6vjelufub8gci231ettdhmk5pv0cdde3.apps.googleusercontent.com"
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), CLIENT_ID)
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
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid Google token'}), 401
    except Exception as e:
        logger.error(f"Google login error: {e}")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500
# ============================================

@app.route('/api/auth/logout', methods=['POST', 'OPTIONS'])
def logout():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('sl_auth_token', '', expires=0, httponly=True, secure=True, samesite='None')
    return resp

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@require_auth
@limiter.limit("5 per minute")
def dub():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    lang = request.form.get('lang', 'ar')
    voice_mode = request.form.get('voice_mode', 'xtts')
    voice_id = request.form.get('voice_id', '')
    voice_url = request.form.get('voice_url', '')
    yt_url = request.form.get('yt_url', '').strip()
    if not yt_url: return jsonify({'success': False, 'error': 'يرجى تقديم رابط يوتيوب'}), 400
    user = request.user
    processing_cost = 100 
    if user.credits < processing_cost: return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402
    job_id = str(uuid.uuid4())
    try:
        user.credits -= processing_cost
        db.session.add(CreditTransaction(user_id=user.id, transaction_type='usage', amount=-processing_cost, reason='Processing Fee'))
        job = DubbingJob(id=job_id, user_id=user.id, language=lang, voice_mode=voice_mode, text_length=0, credits_used=processing_cost, status='processing')
        db.session.add(job)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'Internal DB error'}), 500
    payload = {
        'job_id': job_id,
        'user_id': user.id,
        'lang': lang,
        'voice_mode': voice_mode,
        'voice_id': voice_id,
        'voice_url': voice_url,
        'yt_url': yt_url
    }
    t = Thread(target=process_full_workflow, args=(payload,), daemon=True)
    t.start()
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing', 'remaining_credits': user.credits}), 200

@app.route('/api/job/<job_id>', methods=['GET'])
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != request.user.id: return jsonify({'success': False, 'error': 'Job not found'}), 404
    audio_url = job.output_url
    if audio_url and audio_url.startswith('file://'):
        local_path = audio_url[len('file://'):]
        p = Path(local_path)
        if p.exists(): audio_url = f"https://{request.host}/api/file/{p.name}"
        else: audio_url = None
    return jsonify({'success': True, 'job_id': job.id, 'status': job.status, 'audio_url': audio_url, 'processing_time': job.processing_time, 'remaining_credits': request.user.credits}), 200

@app.route('/api/user', methods=['GET'])
@require_auth
def get_current_user():
    return jsonify({'success': True, 'user': request.user.to_dict()}), 200

@app.route('/api/file/<filename>')
def get_file(filename):
    p = AUDIO_DIR / filename
    return send_file(str(p), mimetype='audio/mpeg', as_attachment=False) if p.exists() else (jsonify({'error': 'Not found'}), 404)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), threaded=True)
