# app.py — V2.3 (Final Fix: User Info Endpoint + CORS Robustness)
import os
import asyncio
import logging
import uuid
from functools import wraps
import jwt
import boto3
from botocore.client import Config
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from dotenv import load_dotenv
from models import db, User, DubbingJob, CreditTransaction
from tasks import process_smart_tts, process_dub, process_stt

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

# تحصين نظام CORS للسماح لجميع المسارات والطلبات بالتواصل مع الواجهة
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

# إعدادات قاعدة البيانات والـ S3
DATABASE_URL = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

s3_client = boto3.client('s3', endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'), region_name='auto')

R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'mp3', 'wav', 'ogg', 'm4a'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- نظام المصادقة المطور ---
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return f(None, *args, **kwargs)

        auth = request.headers.get('Authorization', '')
        token = auth.split()[1] if 'Bearer ' in auth else request.cookies.get('session')
        
        if not token: 
            return jsonify({'error': 'Unauthorized'}), 401
            
        try:
            # استخدام جميع الخوارزميات الممكنة لفك التشفير من Supabase
            secret = os.environ.get('SUPABASE_JWT_SECRET')
            data = jwt.decode(
                token, 
                secret, 
                algorithms=['HS256', 'HS384', 'HS512', 'RS256'], 
                options={'verify_aud': False}
            )
            
            user_id = data.get('sub') 
            email = data.get('email', '')
            
            # البحث عن المستخدم في قاعدة البيانات المحلية المرتبطة بـ Supabase
            user = User.query.filter_by(id=user_id).first()
            
            if not user:
                # إذا كان أول دخول له، ننشئ سجله ونعطيه الرصيد الافتراضي
                user = User(id=user_id, email=email, credits=200000) 
                db.session.add(user)
                db.session.commit()
                
            return f(user, *args, **kwargs)
            
        except Exception as e:
            print(f"Auth Error: {str(e)}")
            return jsonify({'error': 'Invalid Session'}), 401
            
    return decorated

# --- Endpoints ---

# 🌟 المسار السحري المفقود: جلب بيانات المستخدم ورصيده الحقيقي
@app.route('/api/user', methods=['GET', 'OPTIONS'])
@token_required
def get_user_info(current_user):
    if request.method == 'OPTIONS': return jsonify({'ok': True})
    return jsonify({
        'id': current_user.id,
        'email': current_user.email,
        'credits': current_user.credits # هذا ما سيقرأ الـ 199,500 نقطة ويعرضها
    })

@app.route('/api/upload-url', methods=['POST', 'OPTIONS'])
@token_required
def get_upload_url(current_user):
    if request.method == 'OPTIONS': return jsonify({'ok': True})
    data = request.json or {}
    filename = data.get('filename', 'file.mp4')
    if not allowed_file(filename): return jsonify({'error': 'Invalid format'}), 400
    ext = filename.rsplit('.', 1)[-1].lower()
    file_key = f"uploads/u{current_user.id}/{uuid.uuid4().hex}.{ext}"
    url = s3_client.generate_presigned_url('put_object', Params={'Bucket': R2_BUCKET_NAME, 'Key': file_key, 'ContentType': data.get('content_type')}, ExpiresIn=3600)
    return jsonify({'success': True, 'upload_url': url, 'file_key': file_key})

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@token_required
def start_dub(current_user):
    if request.method == 'OPTIONS': return jsonify({'ok': True})
    data = request.json or {}
    file_key = data.get('file_key')
    with_lipsync = data.get('with_lipsync', False) 
    return_video = data.get('return_video', True) 
    sample_b64 = data.get('sample_b64', '')
    
    cost = 150 if with_lipsync else 100
    if current_user.credits < cost:
        return jsonify({'error': 'Insufficient credits'}), 402

    current_user.credits -= cost
    job_id = str(uuid.uuid4())
    new_job = DubbingJob(id=job_id, user_id=current_user.id, status='pending', 
                         language=data.get('lang', 'ar'), method='dubbing', 
                         voice_id=data.get('voice_id', 'source'), file_key=file_key, credits_used=cost)
    db.session.add(new_job)
    db.session.commit()

    process_dub.delay({
        'job_id': job_id, 'file_key': file_key, 'lang': data.get('lang', 'ar'),
        'voice_id': data.get('voice_id', 'source'), 'sample_b64': sample_b64,
        'with_lipsync': with_lipsync, 'video_output': return_video
    })
    return jsonify({'success': True, 'job_id': job_id}), 202

@app.route('/api/job/<job_id>', methods=['GET', 'OPTIONS'])
@token_required
def check_job(current_user, job_id):
    if request.method == 'OPTIONS': return jsonify({'ok': True})
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != current_user.id: return jsonify({'error': 'Not found'}), 404
    return jsonify({'status': job.status, 'output_url': job.output_url, 'error': job.error_message})

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=5000)
