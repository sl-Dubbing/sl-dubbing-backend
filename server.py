# server.py — V3.1 Direct Upload Architecture + History Support
"""
🚀 المعمارية المحدثة:
  Browser → presigned URL → R2 (مباشر)
  Browser → /api/history (جلب السجل الخاص بالمستخدم)
  Browser → /api/dub (file_key) → Celery → Modal
"""
import os
import uuid
import logging
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import jwt
import boto3
from botocore.client import Config
import requests as _requests

# Models
from models import db, User, DubbingJob, CreditTransaction

# Tasks
from tasks import process_dub, process_tts, celery_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sl-dubbing-server")

# ==========================================
# 🔧 Setup
# ==========================================
app = Flask(__name__)
# تم تفعيل CORS لجميع المسارات لضمان عمل واجهتك البرمجية من أي مكان
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL'].replace("postgres://", "postgresql://")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me')
db.init_app(app)

# ==========================================
# 🗄️ R2 Client
# ==========================================
R2_BUCKET = os.environ.get('R2_BUCKET_NAME', 'sl-dubbing-media')
R2_ENDPOINT = os.environ.get('R2_ENDPOINT_URL')
R2_ACCESS_KEY = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')

s3 = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    config=Config(signature_version='s3v4'),
    region_name='auto'
)

# ==========================================
# 🔐 Auth: Supabase Verification
# ==========================================
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://ckjkkxrlgisjdolwddfg.supabase.co')

def verify_supabase_token(token):
    try:
        r = _requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": os.environ.get('SUPABASE_KEY', '')
            },
            timeout=5
        )
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        logger.exception("Supabase verify failed")
        return None

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[7:]

        if not token:
            return jsonify({'error': 'Token missing'}), 401

        supa_user = verify_supabase_token(token)
        if not supa_user:
            return jsonify({'error': 'Invalid token'}), 401

        user = User.query.filter_by(email=supa_user.get('email')).first()
        if not user:
            user = User(
                email=supa_user.get('email'),
                name=supa_user.get('user_metadata', {}).get('full_name') or supa_user.get('email').split('@')[0],
                supabase_id=supa_user.get('id'),
                credits=10
            )
            db.session.add(user)
            db.session.commit()

        return f(user, *args, **kwargs)
    return decorated

# ==========================================
# 🕒 NEW: History Endpoint
# ==========================================
@app.route('/api/history', methods=['GET'])
@token_required
def get_history(user):
    """
    📜 جلب سجل عمليات الدبلجة والـ TTS الخاصة بالمستخدم
    """
    try:
        # جلب آخر 50 عملية مرتبة من الأحدث للأقدم
        jobs = DubbingJob.query.filter_by(user_id=user.id)\
                               .order_by(DubbingJob.created_at.desc())\
                               .limit(50).all()
        
        history_data = []
        for job in jobs:
            # استخراج اسم الملف من الـ key المخزن
            filename = job.input_key.split('/')[-1] if job.input_key else ("نص إلى صوت" if job.kind == 'tts' else "ملف غير معروف")
            
            history_data.append({
                "id": job.id,
                "lang": job.lang,
                "kind": job.kind or 'dub',
                "status": job.status,
                "audio_url": job.audio_url,
                "error": job.error,
                "filename": filename,
                "created_at": job.created_at.isoformat() if job.created_at else None
            })

        return jsonify({
            "success": True,
            "history": history_data
        })
    except Exception as e:
        logger.exception("Failed to fetch history")
        return jsonify({"success": False, "error": str(e)}), 500

# ==========================================
# 📊 Other Endpoints
# ==========================================

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'version': 'v3.1-history-enabled'})

@app.route('/api/user/credits', methods=['GET'])
@token_required
def get_credits(user):
    return jsonify({
        'success': True,
        'user': {'id': user.id, 'credits': user.credits},
        'credits': user.credits
    })

@app.route('/api/upload-url', methods=['POST'])
@token_required
def get_upload_url(user):
    data = request.get_json() or {}
    filename = data.get('filename', 'file')
    content_type = data.get('content_type', 'application/octet-stream')
    size = int(data.get('size', 0))

    MAX_SIZE = 5 * 1024 * 1024 * 1024
    if size > MAX_SIZE:
        return jsonify({'error': f'File too large'}), 413

    if user.credits < 1:
        return jsonify({'error': 'Insufficient credits'}), 402

    ext = filename.rsplit('.', 1)[-1] if '.' in filename else 'mp4'
    file_key = f"uploads/u{user.id}/{uuid.uuid4().hex}.{ext}"

    try:
        upload_url = s3.generate_presigned_url(
            'put_object',
            Params={'Bucket': R2_BUCKET, 'Key': file_key, 'ContentType': content_type},
            ExpiresIn=3600,
            HttpMethod='PUT'
        )
        return jsonify({
            'success': True,
            'upload_url': upload_url,
            'file_key': file_key
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/dub', methods=['POST'])
@token_required
def start_dubbing(user):
    data = request.get_json() or {}
    file_key = data.get('file_key')
    lang = data.get('lang', 'en')
    voice_id = data.get('voice_id', 'source')
    sample_b64 = data.get('sample_b64', '')
    engine = data.get('engine', '')

    if not file_key:
        return jsonify({'error': 'file_key required'}), 400

    if user.credits < 1:
        return jsonify({'error': 'Insufficient credits'}), 402

    media_url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': R2_BUCKET, 'Key': file_key},
        ExpiresIn=7200
    )

    job = DubbingJob(
        user_id=user.id, lang=lang, voice_id=voice_id, engine=engine,
        status='queued', kind='dub', input_key=file_key, created_at=datetime.utcnow()
    )
    db.session.add(job)
    db.session.commit()

    process_dub.delay(
        job_id=job.id, media_url=media_url, lang=lang,
        voice_id=voice_id, sample_b64=sample_b64, engine=engine
    )

    return jsonify({'success': True, 'job_id': job.id, 'status': 'queued'})

@app.route('/api/job/<int:job_id>', methods=['GET'])
@token_required
def job_status(user, job_id):
    job = DubbingJob.query.filter_by(id=job_id, user_id=user.id).first()
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'id': job.id, 'status': job.status, 'audio_url': job.audio_url, 'error': job.error
    })

# ==========================================
# 🚀 Main
# ==========================================
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
