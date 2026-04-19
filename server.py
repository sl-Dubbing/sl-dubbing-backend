# server.py - النسخة الاحترافية النهائية والمستقرة
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
from werkzeug.utils import secure_filename

import requests
import base64

# --- Google Auth Imports ---
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

load_dotenv()

# إعداد السجلات بالإنجليزية لتجنب أخطاء الترميز ASCII في Railway Terminal
DEBUG = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = ['https://sl-dubbing.github.io', 'http://localhost:5500', 'http://127.0.0.1:5500']
AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config['DEBUG'] = DEBUG
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or "sl-super-secret-key-123"

# معالجة رابط قاعدة البيانات ليتوافق مع SQLAlchemy
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)
limiter = Limiter(get_remote_address, app=app, default_limits=["1000 per day"], storage_uri="memory://")

from models import db, User, DubbingJob, CreditTransaction
db.init_app(app)

# إعداد Cloudinary للرفع السحابي
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
            user = User.query.get(data.get('user_id'))
            if not user: raise ValueError("User not found")
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
    resp.set_cookie('sl_auth_token', token, httponly=True, secure=True, samesite='None', max_age=24*60*60)
    return resp

# ----------------- Background processing (The GPU Factory) -----------------
def process_full_workflow(payload):
    with app.app_context():
        job_id = payload.get('job_id')
        user_id = payload.get('user_id')
        start_ts = time.time()

        try:
            job = DubbingJob.query.get(job_id)
            user = User.query.get(user_id)
            file_path = payload.get('file_path')

            if not file_path or not os.path.exists(file_path):
                raise ValueError("Source file missing")

            # سجلات بالإنجليزية فقط لتجنب انهيار ASCII في التيرمينال
            logger.info(f"[{job_id}] Sending encoded file to Modal...")

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
            
            result_data = response.json()
            
            if not result_data.get("success"):
                # رسالة إنجليزية لضمان عدم حدوث Crash أثناء محاولة طباعة سجلات الخطأ
                error_msg = result_data.get('error', 'Unknown Factory Error')
                raise Exception(f"Factory Error: {error_msg}")

            audio_base64 = result_data.get("audio_base64")
            audio_bytes = base64.b64decode(audio_base64)
            
            mp_path = AUDIO_DIR / f"dub_{job_id}.mp3"
            with open(mp_path, "wb") as f:
                f.write(audio_bytes)

            if CLOUDINARY_AVAILABLE:
                resp = cloudinary.uploader.upload(str(mp_path), resource_type='auto', folder="sl-dubbing/audio", public_id=f"dub_{job_id}")
                audio_url = resp.get('secure_url') or resp.get('url')
            else:
                # الرابط المحلي كخيار بديل في حال تعطل Cloudinary
                audio_url = f"https://{request.host}/api/file/dub_{job_id}.mp3"

            job.output_url = audio_url
            job.status = 'completed'
            job.processing_time = time.time() - start_ts
            db.session.commit()
            
            # تنظيف الملفات المؤقتة لتوفير المساحة
            if os.path.exists(file_path): os.remove(file_path)
            logger.info(f"[{job_id}] Processing Success!")

        except Exception as exc:
            # تسجيل الخطأ تقنياً بالإنجليزية فقط
            logger.error(f"[{job_id}] Failed: {str(exc)}")
            try:
                job = DubbingJob.query.get(job_id)
                if job:
                    job.status = 'failed'
                    u = User.query.get(job.user_id)
                    if u and job.credits_used:
                        u.credits += job.credits_used
                        db.session.add(CreditTransaction(user_id=u.id, transaction_type='refund', amount=job.credits_used, reason='Failed'))
                    db.session.commit()
            except Exception:
                db.session.rollback()

# ----------------- Routes -----------------
@app.route('/api/auth/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True)
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    if not email or not password: return jsonify({'success': False, 'error': 'البريد وكلمة المرور مطلوبة'}), 400
    if User.query.filter_by(email=email).first(): return jsonify({'success': False, 'error': 'هذا البريد مسجل مسبقاً'}), 400
    user = User(email=email, name=email.split('@')[0], auth_method='email', credits=50000)
    user.set_password(password)
    db.session.add(user); db.session.commit()
    return generate_auth_response(user, True)

@app.route('/api/auth/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True)
    user = User.query.filter_by(email=(data.get('email') or '').strip().lower()).first()
    if not user or not user.check_password(data.get('password')): return jsonify({'success': False, 'error': 'بيانات الدخول غير صحيحة'}), 401
    return generate_auth_response(user)

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@require_auth
def dub():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    media_file = request.files.get('media_file')
    if not media_file: return jsonify({'success': False, 'error': 'يرجى رفع ملف أولاً'}), 400

    user = request.user
    if user.credits < 100: return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402
    
    job_id = str(uuid.uuid4())
    filename = secure_filename(media_file.filename)
    input_path = AUDIO_DIR / f"in_{job_id}_{filename}"
    media_file.save(input_path)

    job = DubbingJob(id=job_id, user_id=user.id, language=request.form.get('lang', 'ar'), voice_mode=request.form.get('voice_mode', 'xtts'), credits_used=100, status='processing')
    user.credits -= 100
    db.session.add(job); db.session.commit()
    
    payload = {
        'job_id': job_id, 'user_id': user.id, 'lang': job.language,
        'voice_mode': job.voice_mode, 'voice_url': request.form.get('voice_url', ''),
        'file_path': str(input_path), 'filename': filename
    }
    Thread(target=process_full_workflow, args=(payload,), daemon=True).start()
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing', 'remaining_credits': user.credits}), 200

@app.route('/api/job/<job_id>', methods=['GET'])
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != request.user.id: return jsonify({'error': 'Not found'}), 404
    return jsonify({'success': True, 'status': job.status, 'audio_url': job.output_url, 'remaining_credits': request.user.credits}), 200

@app.route('/api/user', methods=['GET'])
@require_auth
def get_current_user():
    return jsonify({'success': True, 'user': request.user.to_dict()}), 200

@app.route('/api/file/<filename>')
def get_file(filename):
    p = AUDIO_DIR / filename
    return send_file(str(p)) if p.exists() else (jsonify({'error': '404'}), 404)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
