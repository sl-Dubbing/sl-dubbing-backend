# server.py — V2.0 (Celery-based, Modal-integrated)
import os
import uuid
import json
import logging
import time
import base64
import tempfile
from functools import wraps

import jwt
import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from dotenv import load_dotenv

from models import db, User, DubbingJob, CreditTransaction

# تحميل المتغيرات البيئية
load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# ⚙️ الإعدادات الأساسية
# ==========================================
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# CORS — تسمح لموقعك بالاتصال مع التوكنات
CORS(app, supports_credentials=True, origins=[
    "https://sl-dubbing.github.io",
    "https://sl-dubbing.github.io/",
])

# ✅ استيراد Celery tasks (بدلاً من ThreadPoolExecutor)
from tasks import process_dub, process_smart_tts

# سقوف الملفات
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', 100))
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024


# ==========================================
# 🔐 ديكوريتور حماية المسارات
# ==========================================
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header:
            parts = auth_header.split()
            if len(parts) == 2 and parts[0].lower() == 'bearer':
                token = parts[1]

        if not token:
            return jsonify({'error': 'Unauthorized'}), 401

        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_user = User.query.get(data['user_id'])
            if not current_user:
                raise Exception("User not found")
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except Exception:
            return jsonify({'error': 'Invalid token'}), 401

        return f(current_user, *args, **kwargs)
    return decorated


# ==========================================
# 🔐 مسارات المصادقة
# ==========================================
@app.route('/api/auth/google', methods=['POST'])
def google_auth():
    try:
        data = request.json or {}
        google_token = data.get('credential')
        if not google_token:
            return jsonify({'error': 'No credential provided'}), 400

        # التحقق من توكن جوجل
        google_res = requests.get(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={google_token}",
            timeout=10,
        )
        if google_res.status_code != 200:
            return jsonify({'error': 'فشل التحقق من حساب جوجل'}), 401

        g_data = google_res.json()
        email = g_data.get('email')
        if not email:
            return jsonify({'error': 'Email not found in Google response'}), 400

        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(
                email=email,
                name=g_data.get('name', email.split('@')[0]),
                avatar=g_data.get('picture', '👤'),
                auth_method='google',
                credits=1000,
            )
            db.session.add(user)
            db.session.commit()
        else:
            user.last_login = __import__('datetime').datetime.utcnow()
            db.session.commit()

        my_token = jwt.encode(
            {'user_id': user.id, 'exp': int(time.time()) + (86400 * 7)},
            app.config['SECRET_KEY'],
            algorithm="HS256",
        )

        return jsonify({
            'success': True,
            'token': my_token,
            'user': user.to_dict(),
        })
    except Exception as e:
        logger.error(f"Google Auth Error: {e}")
        return jsonify({'error': 'حدث خطأ في السيرفر أثناء تسجيل الدخول'}), 500


@app.route('/api/user', methods=['GET'])
@token_required
def get_user_data(current_user):
    return jsonify({'success': True, 'user': current_user.to_dict()})


# ==========================================
# 🎙️ مسار الدبلجة (ملف فيديو/صوت → دبلجة)
# ==========================================
@app.route('/api/dub', methods=['POST'])
@token_required
def upload_dub(current_user):
    temp_path = None
    try:
        cost = int(os.environ.get('DUB_COST', 100))
        if (current_user.credits or 0) < cost:
            return jsonify({"error": "رصيد غير كافٍ"}), 402

        if 'media_file' not in request.files:
            return jsonify({"error": "يرجى اختيار ملف"}), 400

        file = request.files['media_file']
        if not file or not file.filename:
            return jsonify({"error": "ملف غير صالح"}), 400

        # حفظ الملف الرئيسي مؤقتاً
        safe_name = f"{uuid.uuid4()}_{os.path.basename(file.filename)}"
        temp_path = os.path.join(tempfile.gettempdir(), safe_name)
        file.save(temp_path)

        voice_val = request.form.get('voice_id', 'source')
        # الأرقام الطويلة من كلاوديناري → تُصنّف
        safe_mode = "cloudinary_voice" if voice_val.startswith('http') else voice_val

        # بصمة صوتية مرفوعة؟ → حوّلها إلى base64 لتمريرها لـ Modal
        sample_b64 = ""
        if 'voice_sample' in request.files:
            v_file = request.files['voice_sample']
            if v_file and v_file.filename:
                sample_bytes = v_file.read()
                sample_b64 = base64.b64encode(sample_bytes).decode('utf-8')

        # إنشاء Job
        job = DubbingJob(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            status='processing',
            language=request.form.get('lang', 'ar'),
            voice_mode=safe_mode[:50],
            credits_used=cost,
            method='dubbing',
        )

        current_user.credits -= cost
        db.session.add(job)
        db.session.add(CreditTransaction(
            user_id=current_user.id,
            transaction_type='debit',
            amount=cost,
            reason=f'Dubbing job {job.id}',
        ))
        db.session.commit()

        # إرسال المهمة إلى Celery
        payload = {
            'job_id': job.id,
            'file_path': temp_path,
            'lang': job.language,
            'voice_id': voice_val,
            'sample_b64': sample_b64,
        }
        process_dub.delay(payload)

        return jsonify({"success": True, "job_id": job.id})

    except Exception as e:
        logger.error(f"Upload Error: {e}")
        # تنظيف إذا فشل الإدراج قبل إرسال Celery
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        return jsonify({"error": "حدث خطأ أثناء الرفع"}), 500


# ==========================================
# 🌍 مسار تحويل النص إلى صوت (TTS)
# ==========================================
@app.route('/api/tts', methods=['POST'])
@token_required
def upload_tts(current_user):
    try:
        data = request.json or {}
        text = (data.get('text') or '').strip()
        lang = data.get('lang', 'en')
        voice_id = data.get('voice_id', '')

        if not text:
            return jsonify({"error": "النص فارغ"}), 400

        # التكلفة بناءً على طول النص (كل 100 حرف = 10 كريدت)
        cost = max(10, (len(text) // 100) * 10)
        if (current_user.credits or 0) < cost:
            return jsonify({"error": "رصيد غير كافٍ"}), 402

        job = DubbingJob(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            status='processing',
            language=lang,
            voice_mode=(voice_id or 'default')[:50],
            credits_used=cost,
            text_length=len(text),
            method='tts',
        )

        current_user.credits -= cost
        db.session.add(job)
        db.session.add(CreditTransaction(
            user_id=current_user.id,
            transaction_type='debit',
            amount=cost,
            reason=f'TTS job {job.id}',
        ))
        db.session.commit()

        payload = {
            'job_id': job.id,
            'text': text,
            'lang': lang,
            'voice_id': voice_id,
        }
        process_smart_tts.delay(payload)

        return jsonify({"success": True, "job_id": job.id})

    except Exception as e:
        logger.error(f"TTS Error: {e}")
        return jsonify({"error": "حدث خطأ أثناء معالجة النص"}), 500


# ==========================================
# 📡 حالة المهمة
# ==========================================
@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def get_job(current_user, job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({'error': 'Job not found'}), 404

    return jsonify({
        'success': True,
        'job_id': job.id,
        'status': job.status,
        'audio_url': job.output_url,
        'method': job.method,
        'processing_time': job.processing_time,
        'credits_used': job.credits_used,
        'remaining_credits': current_user.credits,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None,
    })


# ==========================================
# 📡 SSE — بث التقدم للمتصفح
# ==========================================
@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    def generate():
        last_status = None
        deadline = time.time() + 1800  # 30 دقيقة كحد أقصى
        while time.time() < deadline:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job:
                    yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                    break
                payload = {
                    'status': job.status,
                    'audio_url': job.output_url,
                }
                if payload != last_status:
                    yield f"data: {json.dumps(payload)}\n\n"
                    last_status = payload
                if job.status in ('completed', 'failed'):
                    break
            time.sleep(2)

    return Response(generate(), mimetype='text/event-stream')


# ==========================================
# ❤️ Health
# ==========================================
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': int(time.time())})


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
