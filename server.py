import os
import uuid
import logging
import time
import json
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, make_response, send_file, Response
from flask_cors import CORS
from dotenv import load_dotenv
import jwt
from functools import wraps
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.utils import secure_filename
import requests

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

load_dotenv()

DEBUG = os.environ.get('DEBUG', '0') in ('1', 'true', 'True')
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# 🟢 تم إضافة النجمة للسماح لجميع النطاقات بالاتصال بدون أخطاء CORS
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

# 🟢 تم تحديث إعدادات CORS للسماح بالاتصال
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)
limiter = Limiter(get_remote_address, app=app, default_limits=["1000 per day"], storage_uri="memory://")

from models import db, User, DubbingJob, CreditTransaction
db.init_app(app)

import redis
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
redis_client = redis.from_url(REDIS_URL)

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

@app.route('/api/auth/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True) or {}
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
    data = request.get_json(force=True, silent=True) or {}
    user = User.query.filter_by(email=(data.get('email') or '').strip().lower()).first()
    if not user or not user.check_password(data.get('password')): return jsonify({'success': False, 'error': 'بيانات الدخول غير صحيحة'}), 401
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
    except Exception: return jsonify({'success': False, 'error': 'Invalid Google token'}), 401

@app.route('/api/auth/logout', methods=['POST', 'OPTIONS'])
def logout():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('sl_auth_token', '', expires=0, httponly=True, secure=True, samesite='None')
    return resp

# مسار الدبلجة الأساسي
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
    from tasks import process_tts # استدعاء المهمة من ملف مهام Celery (كانت تسمى هكذا في كودك)
    process_tts.delay(payload)
    
    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing', 'remaining_credits': user.credits}), 200

# 🌍 مسار الـ TTS الذكي الجديد
@app.route('/api/tts', methods=['POST', 'OPTIONS'])
@require_auth
def tts():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    data = request.get_json(force=True, silent=True) or {}
    
    user = request.user
    if user.credits < 50: return jsonify({'success': False, 'error': 'رصيدك غير كافٍ للـ TTS'}), 402

    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, language=data.get('lang', 'en'), voice_mode=data.get('voice_id', 'source'), credits_used=50, status='processing', method='tts')
    user.credits -= 50
    db.session.add(job); db.session.commit()

    payload = {
        'job_id': job_id, 'user_id': user.id, 'text': data.get('text'),
        'lang': data.get('lang', 'en'), 'voice_id': data.get('voice_id', 'source'),
        'sample_b64': data.get('sample_b64', '')
    }
    from tasks import process_smart_tts # استدعاء المهمة الجديدة
    process_smart_tts.delay(payload)

    return jsonify({'success': True, 'job_id': job_id, 'status': 'processing'}), 200

# 📡 تتبع التقدم لصفحة الـ TTS
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
                msg = "AI is translating and speaking..." if job.status == 'processing' else job.status

                final_text = ""
                if job.status == 'completed':
                    try:
                        f_text_bytes = redis_client.get(f"tts_text_{job_id}")
                        if f_text_bytes: final_text = f_text_bytes.decode('utf-8')
                    except Exception: pass

                payload = {
                    "status": "done" if job.status == 'completed' else job.status,
                    "progress": progress_val,
                    "message": msg,
                    "audio_url": job.output_url,
                    "final_text": final_text
                }
                yield f"data: {json.dumps(payload)}\n\n"
                
                if job.status in ['completed', 'failed', 'error']:
                    break
            time.sleep(1.5)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/job/<job_id>', methods=['GET'])
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != request.user.id: return jsonify({'error': 'Not found'}), 404
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
