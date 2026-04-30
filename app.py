# app.py — V4.0 (Merged Gateway & Server - Async Fixed)
import os
import time
import base64
import asyncio
import logging
import datetime as _dt
from functools import wraps

import jwt
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
            # التحقق المحلي (سريع جداً ولا يحتاج اتصال خارجي)
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
    return jsonify({'success': True, 'user': current_user.to_dict()}), 200

# مسار توليد الصوت السريع عبر Edge-TTS (مُصحح)
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
        return jsonify({"error": f"النص طويل جداً"}), 400

    cost = 1 # تكلفة رمزية للخدمة السريعة
    if not deduct_credits_atomic(current_user.id, cost):
        return jsonify({"error": "رصيدك غير كافٍ"}), 402

    # دالة غير متزامنة لجلب الصوت من Edge-TTS
    async def get_audio_chunks():
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return chunks

    def generate():
        try:
            # تشغيل الـ Async داخل بيئة Flask المتزامنة بشكل آمن
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            audio_data_list = loop.run_until_complete(get_audio_chunks())
            loop.close()

            # إرسال البيانات كـ Stream
            for chunk in audio_data_list:
                yield chunk
        except Exception as e:
            logger.error(f"Edge-TTS Error: {e}")

    response = Response(stream_with_context(generate()), mimetype="audio/mpeg")
    response.headers['X-Remaining-Credits'] = str(current_user.credits)
    response.headers['Access-Control-Expose-Headers'] = 'X-Remaining-Credits'
    return response

# مسار طلب التوليد الذكي للـ Worker
@app.route('/api/tts/smart', methods=['POST'])
@token_required
@json_required
def start_smart_tts(current_user):
    data = request.json or {}
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({"error": "النص غير موجود"}), 400

    cost = 10 # تكلفة الجودة العالية
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

        # الإرسال لـ Celery Worker
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
