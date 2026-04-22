# server.py — النسخة النهائية المتوافقة مع Cloudinary
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
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

_secret = os.environ.get('SECRET_KEY', 'sl-secret-key-2026')
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')

ALLOWED_ORIGINS = ['https://sl-dubbing.github.io', 'http://localhost:5500', 'http://127.0.0.1:5500']
AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['SECRET_KEY'] = _secret
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)
limiter = Limiter(get_remote_address, app=app, default_limits=["1000 per day"], storage_uri="memory://")

from models import db, User, DubbingJob
db.init_app(app)

# إعداد Cloudinary
import cloudinary
import cloudinary.api
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

_executor = ThreadPoolExecutor(max_workers=5)

# ============================================================
# 🟢 ميزة الجلب التلقائي من Cloudinary (مجلد sl_voices)
# ============================================================
@app.route('/api/voices', methods=['GET'])
def list_cloudinary_voices():
    if not CLOUDINARY_AVAILABLE:
        return jsonify({"success": False, "error": "Cloudinary configuration missing"}), 500
    try:
        # جلب الملفات من المجلد sl_voices (يتم تصنيف الصوت كـ video في API كلاوديناري)
        result = cloudinary.api.resources(
            type="upload",
            prefix="sl_voices/",
            resource_type="video"
        )
        
        voices = []
        for res in result.get('resources', []):
            # تنظيف الاسم (إزالة اسم المجلد والامتداد)
            raw_id = res['public_id']
            display_name = raw_id.replace('sl_voices/', '')
            
            voices.append({
                "name": display_name,
                "url": res['secure_url']
            })
        return jsonify({"success": True, "voices": voices})
    except Exception as e:
        logger.error(f"Cloudinary Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ============================================================
# أنظمة المصادقة والعمليات الخلفية
# ============================================================
def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('sl_auth_token')
        if not token: return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user = User.query.get(data.get('user_id'))
            if not user: raise ValueError()
            request.user = user
        except: return jsonify({'error': 'Session expired'}), 401
        return f(*args, **kwargs)
    return decorated_function

def _run_workflow(job_id, modal_url, payload):
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        try:
            response = requests.post(modal_url, json=payload, timeout=1800)
            res = response.json()
            if not res.get("success"): raise Exception(res.get('error'))
            job.output_url = res.get("audio_url")
            job.status = 'completed'
            if res.get("final_text"): job.extra_data = res.get("final_text")
        except Exception as e:
            job.status = 'failed'
            u = User.query.get(job.user_id)
            u.credits += job.credits_used
        db.session.commit()

@app.route('/api/dub', methods=['POST'])
@require_auth
def dub():
    media_file = request.files.get('media_file')
    user = request.user
    if user.credits < 100: return jsonify({'error': 'Insufficient credits'}), 402
    
    job_id = str(uuid.uuid4())
    filename = secure_filename(media_file.filename)
    input_path = AUDIO_DIR / f"in_{job_id}_{filename}"
    media_file.save(input_path)

    job = DubbingJob(id=job_id, user_id=user.id, status='processing', credits_used=100)
    user.credits -= 100
    db.session.add(job); db.session.commit()

    with open(input_path, "rb") as f: file_b64 = base64.b64encode(f.read()).decode('utf-8')

    modal_payload = {
        "file_b64": file_b64,
        "lang": request.form.get('lang', 'ar'),
        "voice_url": request.form.get('voice_url', ''), # الرابط المباشر من كلاوديناري
        "voice_mode": "xtts" if request.form.get('voice_url') else "source"
    }
    
    _executor.submit(_run_workflow, job_id, "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/", modal_payload)
    return jsonify({'success': True, 'job_id': job_id}), 200

@app.route('/api/tts', methods=['POST'])
@require_auth
def tts():
    data = request.get_json()
    user = request.user
    if user.credits < 50: return jsonify({'error': 'Insufficient credits'}), 402
    
    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, status='processing', credits_used=50)
    user.credits -= 50
    db.session.add(job); db.session.commit()

    modal_payload = {
        "text": data.get('text'),
        "lang": data.get('lang', 'en'),
        "voice_url": data.get('voice_url', ''),
        "voice_id": "custom" if data.get('voice_url') else "source"
    }

    _executor.submit(_run_workflow, job_id, "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/tts", modal_payload)
    return jsonify({'success': True, 'job_id': job_id}), 200

@app.route('/api/job/<job_id>')
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job: return jsonify({'error': 'Not found'}), 404
    return jsonify({'status': job.status, 'audio_url': job.output_url, 'final_text': getattr(job, 'extra_data', '')})

@app.route('/api/user')
@require_auth
def get_user(): return jsonify({'success': True, 'user': request.user.to_dict()})

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(email=data.get('email')).first()
    if user and user.check_password(data.get('password')):
        token = jwt.encode({'user_id': user.id, 'exp': datetime.utcnow() + timedelta(hours=24)}, app.config['SECRET_KEY'], algorithm='HS256')
        resp = make_response(jsonify({'success': True, 'user': user.to_dict()}))
        resp.set_cookie('sl_auth_token', token, httponly=True, secure=True, samesite='None', max_age=24*3600)
        return resp
    return jsonify({'error': 'Invalid credentials'}), 401

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
