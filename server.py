# server.py — النسخة النهائية الموثوقة لربط Cloudinary
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

load_dotenv()

# إعدادات التسجيل لمراقبة الأخطاء في Railway Logs
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = ['https://sl-dubbing.github.io', 'http://localhost:5500', 'http://127.0.0.1:5500']
AUDIO_DIR = Path('/tmp/sl_audio')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-secret-key-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)

from models import db, User, DubbingJob
db.init_app(app)

# 🟢 إعداد Cloudinary (تأكدي من إضافة المتغيرات في Railway)
import cloudinary
import cloudinary.api
try:
    if os.getenv('CLOUDINARY_NAME'):
        cloudinary.config(
            cloud_name=os.getenv('CLOUDINARY_NAME'),
            api_key=os.getenv('CLOUDINARY_API_KEY'),
            api_secret=os.getenv('CLOUDINARY_API_SECRET'),
            secure=True
        )
        CLOUDINARY_READY = True
        logger.info("Cloudinary is configured and ready.")
    else:
        CLOUDINARY_READY = False
        logger.warning("Cloudinary environment variables are missing!")
except Exception as e:
    CLOUDINARY_READY = False
    logger.error(f"Cloudinary Config Error: {e}")

_executor = ThreadPoolExecutor(max_workers=5)

# ============================================================
# 🟢 نقطة النهاية لجلب الأصوات (Auto-Sync)
# ============================================================
@app.route('/api/voices', methods=['GET'])
def list_voices():
    voices = []
    
    # محاولة الجلب من Cloudinary
    if CLOUDINARY_READY:
        try:
            # ملاحظة: الملفات الصوتية في Admin API تُطلب بـ resource_type="video"
            result = cloudinary.api.resources(
                type="upload",
                prefix="sl_voices/",
                resource_type="video"
            )
            for res in result.get('resources', []):
                # استخراج اسم الملف بدون المسار
                clean_name = res['public_id'].split('/')[-1]
                voices.append({
                    "name": clean_name,
                    "url": res['secure_url']
                })
        except Exception as e:
            logger.error(f"Failed to fetch from Cloudinary API: {e}")

    # 🟢 إذا فشل كلاوديناري أو كان فارغاً، نعرض أصواتاً افتراضية لكي لا تتعطل الواجهة
    if not voices:
        logger.info("No voices found in Cloudinary, providing defaults.")
        voices = [
            {"name": "muhammad_ar (Default)", "url": "https://res.cloudinary.com/dxbmvzsiz/video/upload/v1712611200/sl_voices/muhammad_ar.wav"},
            {"name": "adam_ar (Backup)", "url": ""}
        ]

    return jsonify({"success": True, "voices": voices})

# ============================================================
# مسارات العمليات والمصادقة
# ============================================================
def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.cookies.get('sl_auth_token')
        if not token: return jsonify({'error': 'Unauthorized'}), 401
        try:
            import jwt
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
            res_data = response.json()
            if res_data.get("success"):
                job.output_url = res_data.get("audio_url")
                job.status = 'completed'
            else:
                job.status = 'failed'
        except Exception:
            job.status = 'failed'
        db.session.commit()

@app.route('/api/dub', methods=['POST'])
@require_auth
def dub():
    media_file = request.files.get('media_file')
    user = request.user
    if user.credits < 100: return jsonify({'error': 'No credits'}), 402
    
    job_id = str(uuid.uuid4())
    input_path = AUDIO_DIR / f"in_{job_id}_{secure_filename(media_file.filename)}"
    media_file.save(input_path)

    job = DubbingJob(id=job_id, user_id=user.id, status='processing', credits_used=100)
    user.credits -= 100
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
    return jsonify({'status': job.status, 'audio_url': job.output_url})

@app.route('/api/user')
@require_auth
def get_user(): return jsonify({'success': True, 'user': request.user.to_dict()})

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
