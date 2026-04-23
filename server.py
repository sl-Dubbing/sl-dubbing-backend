# server.py — النسخة V7.7 (Production ready)
import os
import uuid
import json
import base64
import logging
import time
import requests
import jwt
import tempfile
import shutil
from flask import Flask, request, jsonify, Response, make_response
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import wraps
from models import db, User, DubbingJob
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()
app = Flask(__name__)

# إعدادات الأمان والذاكرة
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

_db_url = os.environ.get('DATABASE_URL')
if not _db_url:
    raise RuntimeError("DATABASE_URL environment variable is required")
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 100 * 1024 * 1024))
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES

ALLOWED_ORIGINS = ['https://sl-dubbing.github.io', 'http://localhost:5500', 'http://127.0.0.1:5500']
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)

db.init_app(app)

executor = ThreadPoolExecutor(max_workers=int(os.environ.get("SERVER_MAX_WORKERS", "10")))

MODAL_URL = os.environ.get("MODAL_URL")
if not MODAL_URL:
    logging.warning("MODAL_URL not set; background tasks will fail until configured.")

ALLOWED_EXTENSIONS = {'.mp4', '.webm', '.wav', '.mp3', '.ogg', '.flac', '.m4a'}

_http_session = requests.Session()
_retry_strategy = Retry(
    total=int(os.environ.get("HTTP_RETRY_TOTAL", "2")),
    backoff_factor=float(os.environ.get("HTTP_RETRY_BACKOFF", "1")),
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST", "GET"]
)
_adapter = HTTPAdapter(max_retries=_retry_strategy)
_http_session.mount("https://", _adapter)
_http_session.mount("http://", _adapter)

if MODAL_URL:
    try:
        hc = _http_session.get(f"{MODAL_URL.rstrip('/')}/health", timeout=3)
        if hc.status_code != 200:
            logging.warning("MODAL_URL health check returned %s", hc.status_code)
    except Exception as e:
        logging.warning("MODAL_URL health check failed: %s", e)

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS': return f(*args, **kwargs)
        token = request.cookies.get('sl_auth_token')
        if not token: return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user = User.query.get(data.get('user_id'))
            if not user: raise Exception("User not found")
            request.user = user
        except jwt.ExpiredSignatureError: return jsonify({'error': 'Session expired'}), 401
        except Exception: return jsonify({'error': 'Session invalid'}), 401
        return f(*args, **kwargs)
    return decorated

def _make_cookie(user):
    token = jwt.encode({'user_id': user.id, 'exp': datetime.utcnow() + timedelta(hours=24)}, app.config['SECRET_KEY'], algorithm='HS256')
    if isinstance(token, bytes): token = token.decode('utf-8')
    resp = make_response(jsonify({'success': True, 'user': user.to_dict()}))
    resp.set_cookie('sl_auth_token', token, httponly=True, secure=True, samesite='None', max_age=86400)
    return resp

@app.route('/api/auth/register', methods=['POST'])
def register():
    d = request.get_json(silent=True) or {}
    email, password = d.get('email'), d.get('password')
    if not email or not password: return jsonify({'error': 'Missing credentials'}), 400
    if User.query.filter_by(email=email).first(): return jsonify({'error': 'Email exists'}), 400
    user = User(email=email, name=d.get('name', email.split('@')[0]), credits=500)
    user.set_password(password)
    db.session.add(user); db.session.commit()
    return _make_cookie(user)

@app.route('/api/auth/login', methods=['POST'])
def login():
    d = request.get_json(silent=True) or {}
    user = User.query.filter_by(email=d.get('email')).first()
    if not user or not user.check_password(d.get('password')): return jsonify({'error': 'Invalid credentials'}), 401
    return _make_cookie(user)

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('sl_auth_token', '', expires=0, httponly=True, secure=True, samesite='None')
    return resp

@app.route('/api/user')
@require_auth
def get_user():
    return jsonify({'success': True, 'user': request.user.to_dict()})

def run_background_task(job_id, endpoint, payload, cost, user_id):
    with app.app_context():
        job, user = DubbingJob.query.get(job_id), User.query.get(user_id)
        if not job or not user: return
        file_path = None
        try:
            if not MODAL_URL: raise RuntimeError("MODAL_URL not configured")
            target = f"{MODAL_URL.rstrip('/')}/{endpoint.lstrip('/')}" if endpoint else f"{MODAL_URL.rstrip('/')}/upload"
            file_path = payload.pop("_file_path", None)
            
            if file_path and os.path.exists(file_path):
                with open(file_path, "rb") as fh:
                    files = {'media_file': (os.path.basename(file_path), fh)}
                    data = {k: v for k, v in payload.items() if v is not None}
                    res = _http_session.post(target, data=data, files=files, timeout=int(os.environ.get("FACTORY_TIMEOUT", "120")))
            else:
                res = _http_session.post(target, json=payload, timeout=int(os.environ.get("FACTORY_TIMEOUT", "120")))

            if not res.ok:
                try: err = res.json()
                except Exception: err = res.text[:400]
                raise RuntimeError(f"Factory returned HTTP {res.status_code}: {err}")

            data = res.json()
            if data.get("success"):
                job.output_url = data.get("audio_url")
                job.extra_data = data.get("translated_text") or data.get("final_text")
                job.status = 'completed'
            else: raise RuntimeError(data.get("error", "Unknown factory error"))

        except Exception as e:
            logging.exception("Task Failed for job %s: %s", job_id, e)
            job.status = 'failed'
            try: user.credits = (user.credits or 0) + cost
            except: pass
        finally:
            try: db.session.commit()
            except: 
                try: db.session.rollback()
                except: pass
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass
                parent = os.path.dirname(file_path)
                try:
                    if os.path.isdir(parent): shutil.rmtree(parent, ignore_errors=True)
                except: pass

def _allowed_file(filename: str) -> bool:
    if not filename: return False
    return os.path.splitext(filename.lower())[1] in ALLOWED_EXTENSIONS

def _create_job_and_submit(file_path: str, lang: str, voice_id: str, sample_b64: str, user, cost: int):
    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, status='processing', language=lang, voice_mode=voice_id or 'xtts')
    user.credits = (user.credits or 0) - cost
    db.session.add(job); db.session.commit()
    payload = {"_file_path": file_path, "lang": lang, "voice_id": voice_id or "", "sample_b64": sample_b64 or ""}
    executor.submit(run_background_task, job_id, "upload", payload, cost, user.id)
    return job_id

def _start_dub_job_from_fileobj(file_obj, filename: str, user, form, cost: int = 100):
    if not _allowed_file(filename): return None, ("Unsupported file type", 415)
    temp_dir = tempfile.mkdtemp()
    input_path = os.path.join(temp_dir, "input.bin")
    try:
        try: file_obj.seek(0)
        except: pass
        with open(input_path, "wb") as out:
            while True:
                chunk = file_obj.read(8192)
                if not chunk: break
                out.write(chunk)

        if os.path.getsize(input_path) > MAX_UPLOAD_BYTES:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return None, (f"File is too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)}MB.", 413)

        return _create_job_and_submit(input_path, form.get('lang', 'en'), form.get('voice_id', ''), form.get('sample_b64', ''), user, cost), None
    except Exception as e:
        logging.exception("Failed to start job from fileobj: %s", e)
        try: db.session.rollback()
        except: pass
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, ("Failed to start job", 500)

@app.route('/api/dub', methods=['POST'])
@app.route('/upload', methods=['POST'])
@require_auth
def upload_dub():
    user, cost = request.user, 100
    if (user.credits or 0) < cost: return jsonify({"error": "Insufficient credits"}), 402
    if 'media_file' not in request.files or request.files['media_file'].filename == '': return jsonify({"error": "Valid media file is required"}), 400
    
    content_length = request.content_length
    if content_length and content_length > MAX_UPLOAD_BYTES: return jsonify({"error": f"File is too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)}MB."}), 413

    file = request.files['media_file']
    job_id, err = _start_dub_job_from_fileobj(file.stream, file.filename, user, request.form, cost)
    if err: return jsonify({"error": err[0]}), err[1]
    return jsonify({"success": True, "job_id": job_id})

@app.route('/api/tts', methods=['POST'])
@require_auth
def tts():
    user, cost = request.user, 20
    if (user.credits or 0) < cost: return jsonify({"error": "Insufficient credits"}), 402
    d = request.get_json(silent=True) or {}
    if not d.get('text'): return jsonify({"error": "Text is required"}), 400

    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, status='processing', language=d.get('lang', 'en'), voice_mode='tts')
    user.credits = (user.credits or 0) - cost
    db.session.add(job); db.session.commit()

    executor.submit(run_background_task, job_id, "tts", d, cost, user.id)
    return jsonify({"success": True, "job_id": job_id})

@app.route('/api/progress/<job_id>')
@require_auth
def progress(job_id):
    user_id = request.user.id
    def stream():
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job or job.user_id != user_id:
                    yield f"data: {json.dumps({'status': 'error', 'error': 'Unauthorized'})}\n\n"
                    break
                payload = {'status': job.status, 'progress': 100 if job.status == 'completed' else 50, 'audio_url': job.output_url, 'final_text': job.extra_data}
                yield f"data: {json.dumps(payload)}\n\n"
                if job.status in ['completed', 'failed', 'error']: break
            time.sleep(2)
    return Response(stream(), headers={"Cache-Control": "no-cache", "Content-Type": "text/event-stream", "Connection": "keep-alive"})

@app.route('/api/job/<job_id>')
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != request.user.id: return jsonify({'error': 'Unauthorized'}), 403
    return jsonify({'status': job.status, 'audio_url': job.output_url})

@app.errorhandler(413)
def request_entity_too_large(error): return jsonify({"error": f"File is too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)}MB."}), 413
@app.route('/api/voices', methods=['GET'])
def get_voices():
    """مسار بسيط لإرجاع قائمة الأصوات للواجهة الأمامية وتجنب خطأ 404"""
    # يمكنك لاحقاً إضافة الأصوات الحقيقية هنا، هذه قائمة مبدئية لكي يعمل الموقع
    voices = [
        {"id": "muhammad_ar", "name": "Muhammad (Arabic)", "gender": "male", "lang": "ar"},
        {"id": "default", "name": "Default Voice", "gender": "neutral", "lang": "en"}
    ]
    return jsonify({"success": True, "voices": voices})
if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
