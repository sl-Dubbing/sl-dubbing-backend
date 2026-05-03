# server.py — V3.2 File Manager Edition (Final Fix)
import os
import uuid
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import boto3
from botocore.client import Config
import requests as _requests

# تأكد من مطابقة هذه الواردات لملفاتك المحلية
from models import db, User, DubbingJob 
from tasks import process_dub, process_tts, celery_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sl-dubbing-server")

app = Flask(__name__)
# تفعيل CORS بشكل كامل للسماح لـ GitHub Pages بالوصول للسيرفر
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL'].replace("postgres://", "postgresql://")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me')
db.init_app(app)

# إعداد R2 Client
s3 = boto3.client('s3', 
                  endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
                  aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
                  aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
                  config=Config(signature_version='s3v4'), 
                  region_name='auto')
R2_BUCKET = os.environ.get('R2_BUCKET_NAME', 'sl-dubbing-media')

# --- 🔐 دالة التحقق من التوكين ---
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else None
        if not token: return jsonify({'error': 'Token missing'}), 401
        
        # التحقق من Supabase
        r = _requests.get(f"{os.environ.get('SUPABASE_URL')}/auth/v1/user", 
                          headers={"Authorization": f"Bearer {token}", 
                                   "apikey": os.environ.get('SUPABASE_KEY', '')})
        
        if r.status_code != 200: return jsonify({'error': 'Invalid token'}), 401
        
        supa_user = r.json()
        user = User.query.filter_by(email=supa_user.get('email')).first()
        if not user:
            user = User(email=supa_user.get('email'), 
                        name=supa_user.get('email').split('@')[0], 
                        credits=10)
            db.session.add(user)
            db.session.commit()
        return f(user, *args, **kwargs)
    return decorated

# --- 🛰️ مسار فحص الحالة (يصلح رسالة "النظام غير متصل") ---
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok', 
        'version': 'v3.2-file-manager',
        'timestamp': datetime.utcnow().isoformat()
    })

# --- 📂 واجهة مدير الملفات ---

@app.route('/api/history', methods=['GET'])
@token_required
def get_history(user):
    # سياسة الـ 15 يوماً
    limit_date = datetime.utcnow() - timedelta(days=15)
    jobs = DubbingJob.query.filter(DubbingJob.user_id == user.id, DubbingJob.created_at >= limit_date)\
                           .order_by(DubbingJob.created_at.desc()).all()
    
    return jsonify({
        "success": True,
        "history": [{
            "id": j.id,
            "name": j.custom_name or (j.input_key.split('/')[-1] if j.input_key else "معالجة بلا اسم"),
            "folder": j.folder_name or "الرئيسية",
            "lang": j.lang,
            "status": j.status,
            "audio_url": j.audio_url,
            "created_at": j.created_at.isoformat(),
            "expires_in": 15 - (datetime.utcnow() - j.created_at).days
        } for j in jobs]
    })

@app.route('/api/file/rename', methods=['POST'])
@token_required
def rename_file(user):
    data = request.json
    job = DubbingJob.query.filter_by(id=data.get('id'), user_id=user.id).first()
    if job:
        job.custom_name = data.get('new_name')
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"error": "File not found"}), 404

@app.route('/api/file/move', methods=['POST'])
@token_required
def move_file(user):
    data = request.json
    job = DubbingJob.query.filter_by(id=data.get('id'), user_id=user.id).first()
    if job:
        job.folder_name = data.get('folder_name')
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"error": "File not found"}), 404

# --- 🚀 مسارات العمليات ---

@app.route('/api/upload-url', methods=['POST'])
@token_required
def get_upload_url(user):
    data = request.get_json() or {}
    file_key = f"uploads/u{user.id}/{uuid.uuid4().hex}.mp4"
    url = s3.generate_presigned_url('put_object', 
                                    Params={'Bucket': R2_BUCKET, 'Key': file_key, 'ContentType': data.get('content_type', 'video/mp4')}, 
                                    ExpiresIn=3600, HttpMethod='PUT')
    return jsonify({'success': True, 'upload_url': url, 'file_key': file_key})

@app.route('/api/dub', methods=['POST'])
@token_required
def start_dubbing(user):
    data = request.get_json() or {}
    file_key = data.get('file_key')
    if not file_key: return jsonify({'error': 'file_key missing'}), 400
    
    media_url = s3.generate_presigned_url('get_object', Params={'Bucket': R2_BUCKET, 'Key': file_key}, ExpiresIn=7200)
    
    job = DubbingJob(user_id=user.id, kind='dub', lang=data.get('lang', 'en'), 
                     status='queued', input_key=file_key, created_at=datetime.utcnow())
    db.session.add(job)
    db.session.commit()
    
    process_dub.delay(job_id=job.id, media_url=media_url, lang=job.lang)
    return jsonify({'success': True, 'job_id': job.id})

@app.route('/api/job/<int:job_id>', methods=['GET'])
@token_required
def job_status(user, job_id):
    job = DubbingJob.query.filter_by(id=job_id, user_id=user.id).first()
    if not job: return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'id': job.id, 
        'status': job.status, 
        'audio_url': job.audio_url, 
        'error': job.error
    })

@app.route('/api/user/credits', methods=['GET'])
@token_required
def get_credits(user):
    return jsonify({'success': True, 'credits': user.credits})

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
