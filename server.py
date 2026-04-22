# server.py — النسخة المستقرة جداً (V20.0)
import os, uuid, logging, time, json, threading, requests, base64
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, make_response, send_file, Response
from flask_cors import CORS
from dotenv import load_dotenv
import jwt
from functools import wraps
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = ['https://sl-dubbing.github.io', 'http://localhost:5500', 'http://127.0.0.1:5500']
AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-secret-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)

from models import db, User, DubbingJob
db.init_app(app)

import cloudinary
import cloudinary.api
try:
    cloudinary.config(
        cloud_name=os.getenv('CLOUDINARY_NAME'),
        api_key=os.getenv('CLOUDINARY_API_KEY'),
        api_secret=os.getenv('CLOUDINARY_API_SECRET'),
        secure=True
    )
    CLOUDINARY_READY = True
except:
    CLOUDINARY_READY = False

_executor = ThreadPoolExecutor(max_workers=5)

# 🟢 جلب الأصوات مع نظام حماية من الانهيار
@app.route('/api/voices', methods=['GET'])
def list_voices():
    voices = []
    if CLOUDINARY_READY:
        try:
            result = cloudinary.api.resources(type="upload", prefix="sl_voices/", resource_type="video")
            for res in result.get('resources', []):
                voices.append({"name": res['public_id'].split('/')[-1], "url": res['secure_url']})
        except Exception as e:
            logger.error(f"Cloudinary Error: {e}")
    
    # أصوات احتياطية تظهر فوراً إذا فشل الاتصال بكلاوديناري
    if not voices:
        voices = [{"name": "muhammad_ar", "url": "https://res.cloudinary.com/dxbmvzsiz/video/upload/v1712611200/sl_voices/muhammad_ar.wav"}]
    
    return jsonify({"success": True, "voices": voices})

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'OPTIONS': return f(*args, **kwargs)
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
            if res.get("success"):
                job.output_url = res.get("audio_url")
                job.status = 'completed'
                if res.get("final_text"): job.extra_data = res.get("final_text")
            else: job.status = 'failed'
        except: job.status = 'failed'
        db.session.commit()

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@require_auth
def dub():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    media_file = request.files.get('media_file')
    if not media_file: return jsonify({'error': 'No file'}), 400
    
    user = request.user
    job_id = str(uuid.uuid4())
    input_path = AUDIO_DIR / f"in_{job_id}_{secure_filename(media_file.filename)}"
    media_file.save(input_path)

    job = DubbingJob(id=job_id, user_id=user.id, status='processing', credits_used=0)
    db.session.add(job); db.session.commit()

    with open(input_path, "rb") as f: file_b64 = base64.b64encode(f.read()).decode('utf-8')

    modal_payload = {
        "file_b64": file_b64,
        "lang": request.form.get('lang', 'ar'),
        "voice_url": request.form.get('voice_url', ''),
        "voice_mode": "xtts" if request.form.get('voice_url') else "source"
    }
    
    _executor.submit(_run_workflow, job_id, "https://sl-dubbing--sl-dubbing-factory-fastapi-app.modal.run/", modal_payload)
    return jsonify({'success': True, 'job_id': job_id}), 200

@app.route('/api/job/<job_id>')
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job: return jsonify({'error': 'Not found'}), 404
    return jsonify({'status': job.status, 'audio_url': job.output_url})

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
    return jsonify({'error': 'Invalid'}), 401

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
