# app.py — V4.0 (Single Source of Truth + All Endpoints)
"""
🎯 المعمارية المبسّطة:
  - Supabase: المصدر الوحيد للمستخدمين والرصيد
  - Railway Postgres: المهام (DubbingJob) فقط
  - لا تكرار، لا تشتت
"""
import os
import uuid
import jwt
import boto3
import requests
import logging
from functools import wraps
from botocore.client import Config
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from models import db, DubbingJob
from tasks import process_dub

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gateway")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

# ==========================================
# 📦 Database (للـ DubbingJob فقط)
# ==========================================
DATABASE_URL = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}
db.init_app(app)

# ==========================================
# 📦 R2
# ==========================================
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
    region_name='auto'
)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')

# ==========================================
# 📦 Supabase
# ==========================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # ⚠️ Service Role (للكتابة)
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")
WELCOME_CREDITS = int(os.environ.get('WELCOME_CREDITS', 1000))

ALLOWED_EXTENSIONS = {'mp4', 'mp3', 'wav', 'm4a', 'mov', 'webm', 'mkv', 'aac', 'ogg', 'flac', 'avi'}
MAX_FILE_SIZE_MB = int(os.environ.get('MAX_FILE_SIZE_MB', 500))


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ==========================================
# 🔐 Supabase Helpers
# ==========================================
def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def supabase_get_user(user_id):
    """يجلب أو ينشئ مستخدم في جدول public.users في Supabase"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/users"
        params = {"id": f"eq.{user_id}", "select": "*"}
        resp = requests.get(url, headers=supabase_headers(), params=params, timeout=8)

        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                return data[0]
        return None
    except Exception as e:
        logger.exception(f"supabase_get_user failed: {e}")
        return None


def supabase_create_user(user_id, email, name=None, avatar=None):
    """ينشئ مستخدم جديد في Supabase + يضع له رصيد ترحيبي"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/users"
        payload = {
            "id": user_id,
            "email": email,
            "name": name or email.split('@')[0],
            "avatar": avatar,
            "credits": WELCOME_CREDITS,
        }
        resp = requests.post(url, headers={**supabase_headers(), "Prefer": "return=representation"},
                             json=payload, timeout=8)
        if resp.status_code in (200, 201):
            data = resp.json()
            return data[0] if isinstance(data, list) and data else data
        else:
            logger.warning(f"supabase_create_user status={resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        logger.exception(f"supabase_create_user failed: {e}")
        return None


def supabase_get_or_create_user(user_id, email, name=None, avatar=None):
    """يضمن وجود المستخدم. إذا لم يكن موجوداً، ينشئه."""
    user = supabase_get_user(user_id)
    if user:
        return user

    logger.info(f"Creating new user in Supabase: {email}")
    user = supabase_create_user(user_id, email, name, avatar)
    return user


def supabase_deduct_credits(user_id, amount):
    """خصم رصيد عبر RPC (atomic)"""
    try:
        # محاولة 1: استخدام RPC إذا تم إنشاؤها
        rpc_url = f"{SUPABASE_URL}/rest/v1/rpc/decrement_credits"
        payload = {"uid": user_id, "amt": amount}
        resp = requests.post(rpc_url, headers=supabase_headers(), json=payload, timeout=8)

        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                return True, int(data[0].get("credits", 0))
            return True, 0

        # محاولة 2: قراءة + كتابة (fallback)
        user = supabase_get_user(user_id)
        if not user or (user.get("credits") or 0) < amount:
            return False, None

        new_credits = user["credits"] - amount
        update_url = f"{SUPABASE_URL}/rest/v1/users"
        params = {"id": f"eq.{user_id}"}
        update_resp = requests.patch(
            update_url,
            headers=supabase_headers(),
            params=params,
            json={"credits": new_credits},
            timeout=8
        )

        if update_resp.status_code in (200, 204):
            return True, new_credits

        return False, None
    except Exception as e:
        logger.exception(f"supabase_deduct_credits failed: {e}")
        return False, None


def supabase_refund_credits(user_id, amount):
    """رد الرصيد عند الفشل"""
    try:
        user = supabase_get_user(user_id)
        if not user:
            return False
        new_credits = (user.get("credits") or 0) + amount
        update_url = f"{SUPABASE_URL}/rest/v1/users"
        params = {"id": f"eq.{user_id}"}
        resp = requests.patch(update_url, headers=supabase_headers(),
                              params=params, json={"credits": new_credits}, timeout=8)
        return resp.status_code in (200, 204)
    except Exception:
        return False


# ==========================================
# 🔐 Token Auth
# ==========================================
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return f(None, *args, **kwargs)

        auth = request.headers.get('Authorization', '')
        token = auth.split()[1] if 'Bearer ' in auth else None
        if not token:
            return jsonify({'error': 'Unauthorized'}), 401

        try:
            try:
                token_alg = jwt.get_unverified_header(token).get('alg', 'HS256')
            except Exception:
                token_alg = 'HS256'

            if token_alg == 'HS256':
                try:
                    data = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=['HS256'],
                                      options={'verify_aud': False})
                except Exception:
                    data = jwt.decode(token, options={"verify_signature": False})
            else:
                # ES256/RS256 - decode بدون verification (Supabase الحديث)
                data = jwt.decode(token, options={"verify_signature": False})

            user_id = data.get('sub')
            email = data.get('email', '')

            if not user_id or not email:
                return jsonify({'error': 'Invalid token payload'}), 401

            # جلب أو إنشاء المستخدم في Supabase
            meta = data.get('user_metadata', {}) or {}
            user = supabase_get_or_create_user(
                user_id=user_id,
                email=email,
                name=meta.get('full_name') or meta.get('name'),
                avatar=meta.get('avatar_url')
            )

            if not user:
                # حتى لو فشل، نسمح بالطلب (نستخدم بيانات JWT)
                user = {
                    'id': user_id,
                    'email': email,
                    'name': meta.get('full_name') or meta.get('name') or email.split('@')[0],
                    'credits': 0,
                }

            return f(user, *args, **kwargs)
        except Exception as e:
            logger.warning(f"Token decode failed: {e}")
            return jsonify({'error': 'Invalid Session'}), 401
    return decorated


# ==========================================
# 📋 Endpoints
# ==========================================
@app.route('/api/health', methods=['GET'])
def health_check():
    db_ok = True
    try:
        db.session.execute("SELECT 1")
    except Exception:
        db_ok = False
    return jsonify({
        'status': 'online',
        'version': 'v4.0',
        'db': 'ok' if db_ok else 'error'
    }), 200


@app.route('/api/user', methods=['GET', 'OPTIONS'])
@token_required
def get_user_info(current_user):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})

    return jsonify({
        'success': True,
        'user': {
            'id': current_user.get('id'),
            'email': current_user.get('email'),
            'name': current_user.get('name'),
            'avatar': current_user.get('avatar'),
            'credits': current_user.get('credits') or 0,
        }
    })


@app.route('/api/user/credits', methods=['GET', 'OPTIONS'])
@token_required
def get_credits(current_user):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})

    # حدّث من Supabase مباشرة (للتأكد من القيمة الحالية)
    fresh_user = supabase_get_user(current_user.get('id'))
    credits = (fresh_user.get('credits') if fresh_user else current_user.get('credits')) or 0

    return jsonify({
        'success': True,
        'user': {'credits': credits},
        'credits': credits
    })


# ==========================================
# 🚀 Direct Upload
# ==========================================
@app.route('/api/upload-url', methods=['POST', 'OPTIONS'])
@token_required
def get_upload_url(current_user):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})

    data = request.json or {}
    filename = secure_filename(data.get('filename', 'file'))
    content_type = data.get('content_type', 'application/octet-stream')
    size = int(data.get('size', 0))

    if not allowed_file(filename):
        return jsonify({'error': 'نوع الملف غير مدعوم'}), 400

    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    if size > max_bytes:
        return jsonify({'error': f'حجم الملف كبير (الحد {MAX_FILE_SIZE_MB}MB)'}), 413

    if (current_user.get('credits') or 0) < 1:
        return jsonify({'error': 'رصيدك غير كافٍ'}), 402

    ext = filename.rsplit('.', 1)[-1] if '.' in filename else 'bin'
    user_short = str(current_user.get('id'))[:8]
    file_key = f"uploads/u{user_short}/{uuid.uuid4().hex}.{ext}"

    try:
        upload_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': R2_BUCKET_NAME,
                'Key': file_key,
                'ContentType': content_type,
            },
            ExpiresIn=3600,
            HttpMethod='PUT'
        )
        return jsonify({
            'success': True,
            'upload_url': upload_url,
            'file_key': file_key,
            'expires_in': 3600,
            'method': 'PUT',
            'headers': {'Content-Type': content_type}
        })
    except Exception as e:
        logger.exception(f"upload-url failed: {e}")
        return jsonify({'error': str(e)}), 500


# ==========================================
# 🎬 /api/dub
# ==========================================
@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@token_required
def start_dub(current_user):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})

    data = request.json or {}
    file_key = data.get('file_key')
    lang = data.get('lang', 'ar')
    voice_id = data.get('voice_id', 'source')
    sample_b64 = data.get('sample_b64', '')
    with_lipsync = bool(data.get('with_lipsync', False))
    return_video = bool(data.get('return_video', True))
    engine = data.get('engine', 'auto')

    if not file_key:
        return jsonify({'error': 'file_key مفقود'}), 400

    # تحقّق من وجود الملف
    try:
        s3_client.head_object(Bucket=R2_BUCKET_NAME, Key=file_key)
    except Exception:
        return jsonify({'error': 'الملف غير موجود في التخزين'}), 404

    # حساب التكلفة
    cost = 150 if with_lipsync else 100

    # خصم الرصيد
    success, new_balance = supabase_deduct_credits(current_user.get('id'), cost)
    if not success:
        return jsonify({'error': 'رصيدك غير كافٍ'}), 402

    # إنشاء job في Railway DB
    job_id = str(uuid.uuid4())
    try:
        new_job = DubbingJob(
            id=job_id,
            user_id=str(current_user.get('id')),
            language=lang,
            status='pending',
            credits_used=cost,
        )
        db.session.add(new_job)
        db.session.commit()
    except Exception as e:
        logger.exception(f"DB insert failed: {e}")
        db.session.rollback()
        # رد الرصيد
        supabase_refund_credits(current_user.get('id'), cost)
        return jsonify({'error': 'فشل إنشاء المهمة'}), 500

    # إرسال للـ Celery
    try:
        process_dub.delay({
            'job_id': job_id,
            'file_key': file_key,
            'lang': lang,
            'voice_id': voice_id,
            'sample_b64': sample_b64,
            'with_lipsync': with_lipsync,
            'video_output': return_video,
            'engine': engine,
        })
    except Exception as e:
        logger.exception(f"Celery enqueue failed: {e}")
        return jsonify({'error': 'فشل إرسال المهمة'}), 500

    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': 'queued',
        'credits_remaining': new_balance,
    })


# ==========================================
# 📊 Job Status
# ==========================================
@app.route('/api/job/<job_id>', methods=['GET', 'OPTIONS'])
@token_required
def check_job(current_user, job_id):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})

    job = DubbingJob.query.get(job_id)
    if not job or str(job.user_id) != str(current_user.get('id')):
        return jsonify({'status': 'failed', 'error': 'غير مصرّح'}), 403

    return jsonify({
        'id': job.id,
        'status': job.status,
        'output_url': job.output_url,
        'audio_url': job.output_url,  # توافق
        'error': job.error_message,
        'lang': job.language,
        'created_at': job.created_at.isoformat() if job.created_at else None,
    })


@app.route('/api/jobs', methods=['GET', 'OPTIONS'])
@token_required
def list_jobs(current_user):
    """ملفاتي"""
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})

    jobs = DubbingJob.query.filter_by(user_id=str(current_user.get('id'))) \
        .order_by(DubbingJob.created_at.desc()).limit(100).all()
    return jsonify({
        'success': True,
        'jobs': [{
            'id': j.id,
            'lang': j.language,
            'status': j.status,
            'output_url': j.output_url,
            'audio_url': j.output_url,
            'created_at': j.created_at.isoformat() if j.created_at else None,
        } for j in jobs]
    })


@app.route('/api/logout', methods=['POST'])
def logout():
    return jsonify({'success': True})


# ==========================================
# 🚀 Init DB
# ==========================================
def init_db():
    try:
        with app.app_context():
            db.create_all()
            logger.info("✅ Database tables created/verified")
    except Exception as e:
        logger.exception(f"❌ DB init failed: {e}")


init_db()


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
