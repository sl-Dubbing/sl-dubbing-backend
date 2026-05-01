# server.py — V2.8 (Ultimate Auth Fix with Official Supabase Client)
import os
import uuid
import json
import logging
import time
import datetime as _dt
from functools import wraps
from io import BytesIO

import requests
import boto3
from botocore.client import Config
from flask import Flask, request, jsonify, Response, make_response
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from sqlalchemy.orm import Session

# استدعاء مكتبة Supabase الرسمية
from supabase import create_client, Client

from models import db, User, DubbingJob, CreditTransaction
from tasks import process_dub, process_smart_tts

# التحقق من وجود Pillow لمعالجة الصور
try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# الإعدادات الأساسية (Environment Variables)
# ==========================================
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

# إعدادات قاعدة البيانات
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# ==========================================
# إعداد Supabase Client
# ==========================================
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

supabase_client: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
# إعداد Cloudflare R2
# ==========================================
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)
R2_PUBLIC_BASE = os.environ.get('R2_PUBLIC_BASE')

# ==========================================
# إعداد CORS
# ==========================================
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', 'https://sl-dubbing.github.io')
CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS.split(','))

COOKIE_NAME = os.environ.get('COOKIE_NAME', 'session')

# ==========================================
# دوال المساعدة (Helpers)
# ==========================================

def _extract_voice_name(value: str) -> str:
    if not value: return "source"
    v = value.strip()
    if v in ("original", "source", ""): return "source"
    if v == "custom": return "custom"
    if v.startswith('http'):
        try:
            return v.rsplit('/', 1)[-1].rsplit('.', 1)[0] or "source"
        except: return "source"
    return v

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header and auth_header.lower().startswith('bearer '):
            token = auth_header.split()[1]
        if not token:
            token = request.cookies.get(COOKIE_NAME)

        if not token:
            return jsonify({'error': 'Unauthorized - Missing Token'}), 401
            
        try:
            if not supabase_client:
                logger.error("SUPABASE_URL or SUPABASE_KEY missing from environment variables!")
                return jsonify({'error': 'Server Configuration Error'}), 500

            # الحل الجذري: استخدام Supabase API للتحقق من التوكن (يتوافق مع جميع الخوارزميات الجديدة)
            user_res = supabase_client.auth.get_user(token)
            
            if not user_res or not user_res.user:
                return jsonify({'error': 'Invalid session'}), 401
                
            email = user_res.user.email
            if not email:
                return jsonify({'error': 'Invalid token payload'}), 401

            current_user = User.query.filter_by(email=email).first()
            
            # إنشاء حساب محلي للمستخدم الجديد
            if not current_user:
                meta = user_res.user.user_metadata or {}
                current_user = User(
                    email=email,
                    name=meta.get('full_name', meta.get('name', email.split('@')[0])),
                    avatar=meta.get('avatar_url'),
                    credits=500
                )
                db.session.add(current_user)
                db.session.commit()

        except Exception as e:
            logger.warning(f"Supabase Auth Error: {e}")
            return jsonify({'error': 'Invalid or expired token'}), 401
            
        return f(current_user, *args, **kwargs)
    return decorated

def deduct_credits_atomic(user_id, amount):
    try:
        user = User.query.get(user_id)
        if not user or (user.credits or 0) < amount:
            return False
        user.credits -= amount
        db.session.add(CreditTransaction(user_id=user_id, amount=amount, transaction_type='debit'))
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        logger.error(f"Credit Deduction Error: {e}")
        return False

# ==========================================
# مسارات الـ API (Routes)
# ==========================================

@app.route('/api/user', methods=['GET'])
@app.route('/api/user/credits', methods=['GET'])
@token_required
def get_user_data(current_user):
    user_dict = current_user.to_dict()
    if current_user.avatar_key and R2_PUBLIC_BASE:
        user_dict['avatar'] = f"{R2_PUBLIC_BASE.rstrip('/')}/{current_user.avatar_key}"
    return jsonify({'success': True, 'user': user_dict})

@app.route('/api/dubbing', methods=['POST'])
@token_required
def start_dubbing_route(current_user):
    cost = int(os.environ.get('DUB_COST', 100))
    if not deduct_credits_atomic(current_user.id, cost):
        return jsonify({"error": "رصيد غير كافٍ"}), 402

    file = request.files.get('media_file')
    if not file: return jsonify({"error": "لم يتم رفع ملف"}), 400

    file_key = f"uploads/{uuid.uuid4()}_{secure_filename(file.filename)}"
    s3_client.upload_fileobj(file, R2_BUCKET_NAME, file_key)

    job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='processing',
        language=request.form.get('lang', 'ar'),
        method='dubbing'
    )
    db.session.add(job)
    db.session.commit()

    process_dub.delay({
        'job_id': job.id,
        'file_key': file_key,
        'lang': job.language,
        'voice_id': _extract_voice_name(request.form.get('voice_id'))
    })

    return jsonify({"success": True, "job_id": job.id})

@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def get_job_status(current_user, job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'status': job.status,
        'audio_url': job.output_url,
        'credits_used': job.credits_used
    })

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'online', 'server': 'Railway'})

@app.route('/')
def index():
    return jsonify({"service": "sl-dubbing", "engine": "running"})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
