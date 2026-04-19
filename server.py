# server.py
import os
import uuid
import logging
import time
import json
from pathlib import Path
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask, request, jsonify, make_response, send_file, Response
from flask_cors import CORS
from dotenv import load_dotenv
import jwt
from functools import wraps
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
import requests
import base64
import shutil

# --- Google Auth Imports ---
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

load_dotenv()

DEBUG = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = ['*']
AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config['DEBUG'] = DEBUG
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or "sl-super-secret-key-123"

DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)
limiter = Limiter(get_remote_address, app=app, default_limits=["1000 per day"], storage_uri="memory://")

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

# 🟢 قاموس لحفظ النص المترجم في الذاكرة (بدون الحاجة لـ Redis)
tts_extra_data = {}

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
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
    return decorated_function

def generate_auth_response(user, is_new=False):
    token = jwt.encode({
        'user_id': user.id, 'sub': user.email,
        'iat': datetime.utcnow(), 'exp': datetime.utcnow() + timedelta(hours=24)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    resp = make_response(jsonify({'success': True, 'user': user.to_dict(), 'is_new': is_new}))
    resp.set_cookie('sl_auth_token', token, httponly=True, secure=True, samesite='None', max_age=24*60*60)
    return resp

# === دوال العمليات في الخلفية ===

def process_full_workflow(payload):
    with app.app_context():
        job_id = payload.get('job_id')
        user_id = payload.get('user_id')
        start_ts = time.time()
        job = None
        try:
            job = DubbingJob.query.get(job_id)
            file_path = payload.get('file_path')

            with open(file_path, "rb") as f:
                file_b64 = base64.b64encode(f.read()).decode('utf-8')

            MODAL_URL = os.environ.get("MODAL_URL") or "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/"
            response = requests.post(MODAL_URL, json={
                "file_b64": file_b64,
                "filename": payload.get('filename'),
                "lang": payload.get('lang', 'ar'),
                "voice_mode": payload.get('voice_mode', 'xtts'),
                "voice_url": payload.get('voice_url', ''),
                "openai_key": os.environ.get("OPENAI_API_KEY", "")
            }, timeout=600)

            if response.status_code != 200: raise Exception(f"Modal returned status {response.status_code}")
            result_data = response.json()
            if not result_data.get("success"): raise Exception(f"Factory Error: {result_data.get('error')}")

            audio_base64 = result_data.get("audio_base64")
            audio_bytes = base64.b64decode(audio_base64)
            mp_path = AUDIO_DIR / f"dub_{job_id}.mp3"
            with open(mp_path, "wb") as f: f.write(audio_bytes)

            if CLOUDINARY_AVAILABLE:
                resp = cloudinary.uploader.upload(str(mp_path), resource_type='auto', folder="sl-dubbing/audio", public_id=f"dub_{job_id}", overwrite=True)
                audio_url = resp.get('secure_url') or resp.get('url')
            else:
                PUBLIC_HOST = os.environ.get("PUBLIC_HOST")
                audio_url = f"https://{PUBLIC_HOST}/api/file/dub_{job_id}.mp3" if PUBLIC_HOST else f"/api/file/dub_{job_id}.mp3"

            job.output_url = audio_url
            job.status = 'completed'
            job.processing_time = time.time() - start_ts
            db.session.add(job); db.session.commit()

            try:
                if os.path.exists(file_path): os.remove(file_path)
                if os.path.exists(mp_path): os.remove(mp_path)
            except Exception: pass
        except Exception as exc:
            try:
                if job:
                    job.status = 'failed'; db.session.add(job)
                    u = User.query.get(job.user_id)
                    if u and job.credits_used:
                        u.credits += job.credits_used
                    db.session.commit()
            except Exception: db.session.rollback()

def process_tts_workflow(job_id, user_id, payload):
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        start_ts = time.time()
        try:
            MODAL_URL = os.environ.get("MODAL_URL") or "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/"
            if not MODAL_URL.endswith('/'): MODAL_URL += '/'
            tts_url = MODAL_URL + "tts"

            response = requests.post(tts_url, json={
                "text": payload.get('text'),
                "lang": payload.get('lang', 'en'),
                "voice_id": payload.get('voice_id', 'source'),
                "sample_b64": payload.get('sample_b64', '')
            }, timeout=300)

            if response.status_code != 200: raise Exception(f"Modal returned status {response.status_code}")
            result_data = response.json()
            if not result_data.get("success"): raise Exception(f"TTS Error: {result_data.get('error')}")

            audio_base64 = result_data.get("audio_base64")
            audio_bytes = base64.b64decode(audio_base64)
            mp_path = AUDIO_DIR / f"tts_{job_id}.mp3"
            with open(mp_path, "wb") as f: f.write(audio_bytes)

            if CLOUDINARY_AVAILABLE:
                resp = cloudinary.uploader.upload(str(mp_path), resource_type='auto', folder="sl-dubbing/tts", public_id=f"tts_{job_id}", overwrite=True)
                audio_url = resp.get('secure_url') or resp.get('url')
            else:
                PUBLIC_HOST = os.environ.get("PUBLIC_HOST")
                audio_url = f"https://{PUBLIC_HOST}/api/file/tts_{job_id}.mp3" if PUBLIC_HOST else f"/api/file/tts_{job_id}.mp3"

            job.output_url = audio_url
            job.status = 'completed'
            job.processing_time = time.time() - start_ts
            
            # حفظ النص المترجم في الذاكرة
            tts_extra_data[job_id] = result_data.get("final_text", "")
            
            db.session.add(job); db.session.commit()

            try:
                if os.path.exists(mp_path): os.remove(mp_path)
            except Exception: pass
        except Exception as exc:
            try:
                if job:
                    job.status = 'failed'; db.session.add(job)
                    u = User.query.get(job.user_id)
                    if u and job.credits_used:
                        u.credits += job.credits_used
                    db.session.commit()
            except Exception: db.session.rollback()

# === مسارات الـ API ===

@app.route('/api/auth/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or not password: return jsonify({'success': False, 'error': 'مطلوب'}), 400
    if User.query.filter_by(email=email).first(): return jsonify({'success': False, 'error': 'مسجل مسبقاً'}), 400
    user = User(email=email, name=email.split('@')[0], auth_method='email', credits=50000)
    user.set_password(password)
    db.session.add(user); db.session.commit()
    return generate_auth_response(user, True)

@app.route('/api/auth/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True) or {}
    user = User.query.filter_by(email=(data.get('email') or '').strip().lower()).first()
    if not user or not user.check_password(data.get('password')): return jsonify({'success': False, 'error': 'خطأ'}), 401
    return generate_auth_response(user)

@app.route('/api/auth/google', methods=['POST', 'OPTIONS'])
def google_login():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True)
    token = data.get('credential')
    try:
        CLIENT_ID = "497619073475-6vjelufub8gci231ettdhmk5pv0cdde3.apps.googleusercontent.com"
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), CLIENT_ID)
        email = idinfo['email']
        name = idinfo.get('name', email.split('@')[0])
        user = User.query.filter_by(email=email).first()
        is_new = False
        if not user:
            user = User(email=email, name=name, auth_method='google', credits=50000)
            db.session.add(user); db.session.commit(); is_new = True
        user.last_login = datetime.utcnow(); db.session.commit()
        return generate_auth_response(user, is_new=is_new)
    except Exception: return jsonify({'success': False, 'error': 'Token error'}), 401

@app.route('/api/auth/logout', methods=['POST', 'OPTIONS'])
def logout():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('sl_auth_token', '', expires=0, httponly=True, secure=True, samesite='None')
    return resp

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@require_auth
def dub():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    media_file = request.files.get('media_file')
    if not media_file: return jsonify({'success': False, 'error': 'Missing file'}), 400

    user = request.user
    if user.credits < 100: return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402

    job_id = str(uuid.uuid4())
    filename = secure_filename(media_file.filename)
    input_path = AUDIO_DIR / f"in_{job_id}_{filename}"
    media_file.save(input_path)

    job = DubbingJob(id=job_id, user_id=user.id, language=request.form.get('lang', 'ar'), voice_mode=request.form.get('voice_mode', 'xtts'), credits_used=100, status='processing', method='dub')
    user.credits -= 100
    db.session.add(job); db.session.commit()

    payload = {
        'job_id': job_id, 'user_id': user.id, 'lang': job.language,
        'voice_mode': job.voice_mode, 'voice_url': request.form.get('voice_url', ''),
        'file_path': str(input_path), 'filename': filename
    }
    Thread(target=process_full_workflow, args=(payload,), daemon=True).start()
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'}), 200

@app.route('/api/tts', methods=['POST', 'OPTIONS'])
@require_auth
def tts():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True) or {}
    
    user = request.user
    if user.credits < 50: return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402

    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, language=data.get('lang', 'en'), voice_mode=data.get('voice_id', 'source'), credits_used=50, status='processing', method='tts')
    user.credits -= 50
    db.session.add(job); db.session.commit()

    payload = {
        'job_id': job_id, 'user_id': user.id, 'text': data.get('text'),
        'lang': data.get('lang', 'en'), 'voice_id': data.get('voice_id', 'source'),
        'sample_b64': data.get('sample_b64', '')
    }
    Thread(target=process_tts_workflow, args=(job_id, user.id, payload), daemon=True).start()
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'}), 200

@app.route('/api/progress/<job_id>', methods=['GET'])
def get_progress(job_id):
    def generate():
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job:
                    yield f"data: {json.dumps({'status': 'error', 'error': 'Job not found'})}\n\n"
                    break
                
                progress_val = 50 if job.status == 'processing' else (100 if job.status == 'completed' else 0)
                msg = "AI is working..." if job.status == 'processing' else job.status

                final_text = ""
                if job.status == 'completed':
                    final_text = tts_extra_data.get(job_id, "")

                payload = {
                    "status": "done" if job.status == 'completed' else job.status,
                    "progress": progress_val,
                    "message": msg,
                    "audio_url": job.output_url,
                    "final_text": final_text
                }
                yield f"data: {json.dumps(payload)}\n\n"
                
                if job.status in ['completed', 'failed', 'error']:
                    if job_id in tts_extra_data: del tts_extra_data[job_id] # تفريغ الذاكرة
                    break
            time.sleep(1.5)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/job/<job_id>', methods=['GET'])
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job: return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'success': True, 'job_id': job.id, 'status': job.status, 'audio_url': job.output_url,
        'method': job.method, 'processing_time': job.processing_time, 'credits_used': job.credits_used,
        'remaining_credits': request.user.credits
    }), 200

@app.route('/api/user', methods=['GET'])
@require_auth
def get_current_user():
    return jsonify({'success': True, 'user': request.user.to_dict()}), 200

@app.route('/api/file/<filename>')
def get_file(filename):
    p = AUDIO_DIR / filename
    return send_file(str(p)) if p.exists() else (jsonify({'error': '404'}), 404)

with app.app_context(): db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
