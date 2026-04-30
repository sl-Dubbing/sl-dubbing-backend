# app.py — V4.1 (Merged Gateway & Server - Async Fixed & Dubbing Restored)
import os
import time
import base64
import asyncio
import logging
import uuid
import datetime as _dt
from functools import wraps

import jwt
import boto3
from botocore.client import Config
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from dotenv import load_dotenv

# حماية (Rate Limiting)
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    LIMITER_AVAILABLE = True
except Exception:
    LIMITER_AVAILABLE = False

import edge_tts

# استيراد النماذج والمهام من مشروعك
from models import db, User, DubbingJob, CreditTransaction
from tasks import process_smart_tts, process_dub

load_dotenv()

# ---------------------------
# الإعدادات الأساسية
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gateway")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]
CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS)

if LIMITER_AVAILABLE:
    limiter = Limiter(app, key_func=get_remote_address, default_limits=["200 per day", "50 per minute"])
    logger.info("Flask-Limiter enabled")
else:
    limiter = None

# إعداد قاعدة البيانات
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

SUPABASE_JWT_SECRET = os.environ.get('SUPABASE_JWT_SECRET')
MAX_TTS_LENGTH = int(os.environ.get('MAX_TTS_LENGTH', 5000))

# إعداد Cloudflare R2 Client (تمت إعادته لدعم رفع الفيديوهات)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)
R2_PUBLIC_BASE = os.environ.get('R2_PUBLIC_BASE')

# ---------------------------
# دوال المساعدة والمصادقة
# ---------------------------
def json_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not request.is_json:
            return jsonify({"error": "يجب أن يكون الطلب بصيغة JSON"}), 400
        return f(*args, **kwargs)
    return wrapper

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header and auth_header.lower().startswith('bearer '):
            token = auth_header.split()[1]
        
        if not token:
            token = request.cookies.get('session')

        if not token:
            return jsonify({'success': False, 'error': 'التوكن مفقود'}), 401

        try:
            data = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated")
            email = data.get('email')
            
            current_user = User.query.filter_by(email=email).first()
            if not current_user:
                meta = data.get('user_metadata', {})
                current_user = User(
                    email=email,
                    name=meta.get('full_name', meta.get('name', email.split('@')[0])),
                    avatar=meta.get('avatar_url'),
                    credits=int(os.environ.get('WELCOME_CREDITS', 1000)),
                    auth_method='supabase'
                )
                db.session.add(current_user)
                db.session.commit()
                
        except Exception as e:
            logger.warning(f"Invalid Token: {e}")
            return jsonify({'success': False, 'error': 'جلسة غير صالحة'}), 401

        return f(current_user, *args, **kwargs)
    return decorated

def deduct_credits_atomic(user_id: int, amount: int) -> bool:
    try:
        user = User.query.get(user_id)
        if not user or (user.credits or 0) < amount:
            return False
        user.credits -= amount
        db.session.add(CreditTransaction(user_id=user.id, amount=amount, transaction_type='debit'))
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        logger.error(f"Credit Deduction Error: {e}")
        return False

# ---------------------------
# المسارات (Routes)
# ---------------------------
@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "Gateway Online"}), 200

@app.route('/api/user', methods=['GET'])
@token_required
def get_user(current_user):
    user_dict = current_user.to_dict()
    if getattr(current_user, 'avatar_key', None) and R2_PUBLIC_BASE:
        user_dict['avatar'] = f"{R2_PUBLIC_BASE.rstrip('/')}/{current_user.avatar_key}"
    return jsonify({'success': True, 'user': user_dict}), 200

# ---------------------------
# 1. مسار الدبلجة (تمت إعادته)
# ---------------------------
@app.route('/api/dubbing', methods=['POST'])
@token_required
def start_dubbing_route(current_user):
    cost = int(os.environ.get('DUB_COST', 100))
    if not deduct_credits_atomic(current_user.id, cost):
        return jsonify({"error": "رصيد غير كافٍ"}), 402

    file = request.files.get('media_file')
    if not file: 
        # إرجاع الرصيد في حال عدم وجود ملف
        deduct_credits_atomic(current_user.id, -cost)
        return jsonify({"error": "لم يتم رفع ملف"}), 400

    # رفع الفيديو إلى R2
    file_key = f"uploads/{uuid.uuid4()}_{secure_filename(file.filename)}"
    s3_client.upload_fileobj(file, R2_BUCKET_NAME, file_key)

    new_job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='pending',
        language=request.form.get('lang', 'ar'),
        method='dubbing'
    )
    db.session.add(new_job)
    db.session.commit()

    # الإرسال لـ Worker
    process_dub.delay({
        'job_id': new_job.id,
        'file_key': file_key,
        'lang': new_job.language,
        'voice_id': request.form.get('voice_id', 'source'),
        'sample_b64': request.form.get('sample_b64', '')
    })

    return jsonify({"success": True, "job_id": new_job.id})

# ---------------------------
# 2. مسار التوليد السريع (Edge-TTS)
# ---------------------------
@app.route('/api/tts/quick', methods=['POST'])
@token_required
@json_required
def quick_tts(current_user):
    data = request.json or {}
    text = (data.get('text') or '').strip()
    voice = data.get('edge_voice') or "ar-SA-HamedNeural"
    rate = data.get('rate', '+0%')
    pitch = data.get('pitch', '+0Hz')

    if not text:
        return jsonify({"error": "النص مفقود"}), 400
    if len(text) > MAX_TTS_LENGTH:
        return jsonify({"error": "النص طويل جداً"}), 400

    cost = 1 
    if not deduct_credits_atomic(current_user.id, cost):
        return jsonify({"error": "رصيدك غير كافٍ"}), 402

    async def get_audio_chunks():
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return chunks

    def generate():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            audio_data_list = loop.run_until_complete(get_audio_chunks())
            loop.close()

            for chunk in audio_data_list:
                yield chunk
        except Exception as e:
            logger.error(f"Edge-TTS Error: {e}")

    response = Response(stream_with_context(generate()), mimetype="audio/mpeg")
    response.headers['X-Remaining-Credits'] = str(current_user.credits)
    response.headers['Access-Control-Expose-Headers'] = 'X-Remaining-Credits'
    return response

# ---------------------------
# 3. مسار التوليد الذكي (Modal)
# ---------------------------
@app.route('/api/tts/smart', methods=['POST'])
@token_required
@json_required
def start_smart_tts(current_user):
    data = request.json or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({"error": "النص غير موجود"}), 400

    cost = 10 
    if not deduct_credits_atomic(current_user.id, cost):
        return jsonify({"error": "رصيدك غير كافٍ"}), 402

    try:
        new_job = DubbingJob(
            user_id=current_user.id,
            status='pending',
            language=data.get('lang', 'ar'),
            method='smart_tts'
        )
        db.session.add(new_job)
        db.session.commit()

        payload = {
            'job_id': str(new_job.id),
            'text': text,
            'lang': data.get('lang', 'ar'),
            'voice_id': data.get('voice_id', ''),
            'sample_b64': data.get('sample_b64', ''),
            'rate': data.get('rate', '+0%'),
            'pitch': data.get('pitch', '+0Hz')
        }

        process_smart_tts.delay(payload)
        return jsonify({"success": True, "job_id": str(new_job.id)}), 202

    except Exception as e:
        logger.error(f"Dispatch Error: {e}")
        return jsonify({"error": "فشل توجيه المهمة"}), 500

@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def check_job(current_user, job_id):
    try:
        job = DubbingJob.query.get(job_id)
        if not job or job.user_id != current_user.id:
            return jsonify({"status": "failed", "error": "غير مصرح لك"}), 403
            
        return jsonify({
            "status": job.status,
            "audio_url": job.output_url,
            "error": getattr(job, 'error_message', None)
        }), 200
    except Exception as e:
        return jsonify({"status": "failed", "error": "خطأ في السيرفر"}), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
