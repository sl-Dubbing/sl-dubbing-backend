# server.py — V2.5 (Supabase Auth Integration)
import os
import uuid
import json
import logging
import time
import base64
import datetime as _dt
from functools import wraps
from io import BytesIO

import jwt
import requests
import boto3
from botocore.client import Config
from flask import Flask, request, jsonify, Response, make_response, stream_with_context
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from sqlalchemy.orm import Session

import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from models import db, User, DubbingJob, CreditTransaction
from tasks import process_dub, process_smart_tts

# Optional: Pillow for image validation
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
# الإعدادات الأساسية
# ==========================================
# مفتاح Supabase الجديد لفك تشفير التوكن
SUPABASE_JWT_SECRET = os.environ.get('SUPABASE_JWT_SECRET')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# إعداد Cloudflare R2 Client (S3 compatible)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME', 'sl-dubbing-media')
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)

R2_PUBLIC_BASE = os.environ.get('R2_PUBLIC_BASE')

ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS',
                                "https://sl-dubbing.github.io,https://sl-dubbing-frontend.vercel.app,http://localhost:3000,http://localhost:5173,http://127.0.0.1:5500").split(',')
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]

CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS)

MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', 100))
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

COOKIE_NAME = os.environ.get('COOKIE_NAME', 'session')
COOKIE_MAX_AGE = int(os.environ.get('COOKIE_MAX_AGE', 86400 * 7))
COOKIE_SAMESITE = os.environ.get('COOKIE_SAMESITE', 'None')
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', 'true').lower() in ('1', 'true', 'yes')
COOKIE_DOMAIN = os.environ.get('COOKIE_DOMAIN') or None

# إعدادات SMTP
SMTP_HOST = os.environ.get('SMTP_HOST')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASS = os.environ.get('SMTP_PASS')
EMAIL_FROM = os.environ.get('EMAIL_FROM', 'no-reply@example.com')

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

MAX_TTS_LENGTH = int(os.environ.get('MAX_TTS_LENGTH', 5000))

def _extract_voice_name(value: str) -> str:
    if not value:
        return "source"
    v = value.strip()
    if v in ("original", "source", ""):
        return "source"
    if v == "custom":
        return "custom"
    if v.startswith('http'):
        try:
            tail = v.rsplit('/', 1)[-1]
            name = tail.rsplit('.', 1)[0]
            return name or "source"
        except Exception:
            return "source"
    return v

# ==========================================
# دالة التحقق من التوكن (محدثة لـ Supabase)
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
            token = request.cookies.get(COOKIE_NAME)

        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
            
        try:
            if not SUPABASE_JWT_SECRET:
                logger.error("SUPABASE_JWT_SECRET is missing from environment variables!")
                raise Exception("Server configuration error")

            # فك تشفير التوكن الخاص بـ Supabase
            data = jwt.decode(
                token, 
                SUPABASE_JWT_SECRET, 
                algorithms=["HS256"], 
                audience="authenticated"
            )
            
            # جلب الإيميل من التوكن
            email = data.get('email')
            if not email:
                return jsonify({'error': 'Token missing email'}), 401

            # البحث عن المستخدم في قاعدة بياناتنا أو إنشاؤه إذا كان جديداً
            current_user = User.query.filter_by(email=email).first()
            if not current_user:
                user_metadata = data.get('user_metadata', {})
                name = user_metadata.get('name', user_metadata.get('full_name', email.split('@')[0]))
                avatar_url = user_metadata.get('avatar_url')

                current_user = User(
                    email=email,
                    name=name,
                    avatar=avatar_url,
                    auth_method='supabase',
                    credits=1000,
                )
                db.session.add(current_user)
                db.session.commit()

        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except Exception as e:
            logger.warning(f"Token decode error: {e}")
            return jsonify({'error': 'Invalid token'}), 401
            
        return f(current_user, *args, **kwargs)
    return decorated

# ---------------------------
# Helper: atomic credit deduction
# ---------------------------
def deduct_credits_atomic(user_id: int, amount: int) -> bool:
    session: Session = db.session
    try:
        with session.begin():
            user = session.query(User).with_for_update().filter(User.id == user_id).one_or_none()
            if not user:
                return False
            if (user.credits or 0) < amount:
                return False
            user.credits -= amount
            tx = CreditTransaction(
                user_id=user.id,
                transaction_type='debit',
                amount=amount,
                reason=f'deduction {int(time.time())}'
            )
            session.add(tx)
        return True
    except Exception as e:
        logger.error(f"Atomic deduction failed: {e}")
        session.rollback()
        return False

# ---------------------------
# Routes
# ---------------------------
# هذه النهاية الطرفية (Endpoint) أصبحت تعمل كنقطة "مزامنة" للواجهة الأمامية
@app.route('/api/auth/google', methods=['POST'])
@token_required
def google_auth(current_user):
    """
    بما أن الواجهة الأمامية تتعامل مع Supabase للحصول على التوكن، 
    هذا الرابط يتأكد فقط من مزامنة المستخدم وإرجاع بياناته.
    """
    try:
        current_user.last_login = _dt.datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True, 'user': current_user.to_dict()})
    except Exception as e:
        logger.error(f"Auth Sync Error: {e}")
        return jsonify({'error': 'حدث خطأ في السيرفر أثناء المزامنة'}), 500

@app.route('/api/logout', methods=['POST'])
def logout():
    # Supabase يتعامل مع تسجيل الخروج في الواجهة الأمامية، لكننا نمسح الكوكيز احتياطياً
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie(COOKIE_NAME, '', expires=0, max_age=0, path='/')
    return resp

@app.route('/api/user', methods=['GET'])
@token_required
def get_user_data(current_user):
    avatar_url = None
    try:
        avatar_key = getattr(current_user, 'avatar_key', None)
        if avatar_key:
            if R2_PUBLIC_BASE:
                base = R2_PUBLIC_BASE.rstrip('/')
                avatar_url = f"{base}/{avatar_key}"
            else:
                try:
                    avatar_url = s3_client.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': R2_BUCKET_NAME, 'Key': avatar_key},
                        ExpiresIn=3600
                    )
                except Exception as e:
                    logger.warning(f"Presigned generation failed: {e}")
                    avatar_url = None
        if not avatar_url and getattr(current_user, 'avatar', None):
            avatar_url = current_user.avatar
    except Exception as e:
        logger.warning(f"Error generating avatar URL: {e}")
        avatar_url = getattr(current_user, 'avatar', None)

    user_dict = current_user.to_dict()
    user_dict['avatar'] = avatar_url
    can_send = bool(SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_FROM)
    return jsonify({'success': True, 'user': user_dict, 'can_send_email': can_send})

# ---------------------------
# Avatar upload endpoint
# ---------------------------
@app.route('/api/user/avatar', methods=['POST'])
@token_required
def upload_avatar(current_user):
    try:
        if 'avatar' not in request.files:
            return jsonify({'error': 'No file'}), 400
        f = request.files['avatar']
        if f.filename == '' or not allowed_file(f.filename):
            return jsonify({'error': 'Invalid file'}), 400

        f.stream.seek(0)
        file_bytes = f.read()
        if PIL_AVAILABLE:
            try:
                img = Image.open(BytesIO(file_bytes))
                img.verify()
            except Exception:
                return jsonify({'error': 'Invalid image file'}), 400

        file_stream = BytesIO(file_bytes)
        filename = secure_filename(f.filename)
        key = f"avatars/{current_user.id}/{uuid.uuid4()}_{filename}"

        try:
            s3_client.upload_fileobj(file_stream, R2_BUCKET_NAME, key)
        except Exception as e:
            logger.warning(f"upload_fileobj failed: {e}")
            try:
                file_stream.seek(0)
                s3_client.upload_fileobj(file_stream, R2_BUCKET_NAME, key)
            except Exception as e2:
                logger.error(f"Second upload attempt failed: {e2}")
                return jsonify({'error': 'Upload failed'}), 500

        current_user.avatar_key = key
        current_user.avatar = None
        db.session.commit()

        avatar_url = None
        if R2_PUBLIC_BASE:
            avatar_url = f"{R2_PUBLIC_BASE.rstrip('/')}/{key}"
        else:
            try:
                avatar_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': R2_BUCKET_NAME, 'Key': key},
                    ExpiresIn=3600
                )
            except Exception as e:
                logger.warning(f"Presigned generation after upload failed: {e}")
                avatar_url = None

        user_dict = current_user.to_dict()
        user_dict['avatar'] = avatar_url
        return jsonify({'success': True, 'user': user_dict})
    except Exception as e:
        logger.error(f"Avatar upload error: {e}")
        return jsonify({'error': 'Upload failed'}), 500

# ---------------------------
# Send avatar in email
# ---------------------------
def send_avatar_email(user):
    try:
        avatar_key = getattr(user, 'avatar_key', None)
        avatar_url = None
        if avatar_key:
            if R2_PUBLIC_BASE:
                avatar_url = f"{R2_PUBLIC_BASE.rstrip('/')}/{avatar_key}"
            else:
                try:
                    avatar_url = s3_client.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': R2_BUCKET_NAME, 'Key': avatar_key},
                        ExpiresIn=3600
                    )
                except Exception as e:
                    avatar_url = None
        if not avatar_url and getattr(user, 'avatar', None):
            avatar_url = user.avatar

        if not avatar_url:
            return {'error': 'No avatar'}

        r = requests.get(avatar_url, timeout=15)
        if r.status_code != 200:
            return {'error': 'Failed to fetch avatar'}

        img_data = r.content
        content_type = r.headers.get('Content-Type', 'image/png')
        maintype, subtype = content_type.split('/', 1) if '/' in content_type else ('image', 'png')
        cid = make_msgid(domain='sl-dubbing.local')

        msg = EmailMessage()
        msg['Subject'] = 'Your profile image'
        msg['From'] = EMAIL_FROM
        msg['To'] = user.email
        msg.set_content(f"Hello {user.name},\n\nThis email contains your profile image.\n")

        html_body = f"""\
        <html>
          <body>
            <p>مرحباً {user.name},</p>
            <p>هذه صورتك الشخصية:</p>
            <img src="cid:{cid[1:-1]}" alt="avatar" style="max-width:300px;border-radius:8px;"/>
          </body>
        </html>
        """
        msg.add_alternative(html_body, subtype='html')
        msg.get_payload()[1].add_related(img_data, maintype=maintype, subtype=subtype, cid=cid)

        if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
            return {'error': 'smtp_not_configured'}

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        return {'success': True}
    except Exception as e:
        logger.error(f"Send avatar email error: {e}")
        return {'error': 'send_failed'}

@app.route('/api/user/send-avatar-email', methods=['POST'])
@token_required
def send_avatar_email_endpoint(current_user):
    res = send_avatar_email(current_user)
    if res.get('success'):
        return jsonify({'success': True})
    return jsonify({'error': res.get('error', 'failed')}), 500

# ---------------------------
# Upload dubbing 
# ---------------------------
@app.route('/api/dub', methods=['POST'])
@token_required
def upload_dub(current_user):
    try:
        cost = int(os.environ.get('DUB_COST', 100))
        if not deduct_credits_atomic(current_user.id, cost):
            return jsonify({"error": "رصيد غير كافٍ"}), 402

        if 'media_file' not in request.files:
            session = db.session
            with session.begin():
                user = session.query(User).with_for_update().filter(User.id == current_user.id).one_or_none()
                if user:
                    user.credits += cost
                    session.add(CreditTransaction(user_id=user.id, transaction_type='credit', amount=cost, reason='refund missing file'))
            return jsonify({"error": "يرجى اختيار ملف"}), 400

        file = request.files['media_file']
        if not file or not file.filename:
            session = db.session
            with session.begin():
                user = session.query(User).with_for_update().filter(User.id == current_user.id).one_or_none()
                if user:
                    user.credits += cost
                    session.add(CreditTransaction(user_id=user.id, transaction_type='credit', amount=cost, reason='refund invalid file'))
            return jsonify({"error": "ملف غير صالح"}), 400

        safe_name = f"{uuid.uuid4()}_{os.path.basename(file.filename)}"
        file_key = f"uploads/{safe_name}"
        s3_client.upload_fileobj(file, R2_BUCKET_NAME, file_key)

        raw_voice = request.form.get('voice_id', 'original')
        voice_name = _extract_voice_name(raw_voice)

        sample_b64 = ""
        if 'voice_sample' in request.files:
            v_file = request.files['voice_sample']
            if v_file and v_file.filename:
                sample_bytes = v_file.read()
                sample_b64 = base64.b64encode(sample_bytes).decode('utf-8')
                voice_name = "source"

        job = DubbingJob(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            status='processing',
            language=request.form.get('lang', 'ar'),
            voice_mode=voice_name[:50],
            credits_used=cost,
            method='dubbing',
        )

        db.session.add(job)
        db.session.add(CreditTransaction(
            user_id=current_user.id,
            transaction_type='debit',
            amount=cost,
            reason=f'Dubbing job {job.id}',
        ))
        db.session.commit()

        payload = {
            'job_id': job.id,
            'file_key': file_key,
            'lang': job.language,
            'voice_id': voice_name,
            'sample_b64': sample_b64,
        }
        try:
            if hasattr(process_dub, 'delay'):
                process_dub.delay(payload)
            elif hasattr(process_dub, 'spawn'):
                process_dub.spawn(payload)
            else:
                process_dub(payload)
        except Exception as e:
            logger.error(f"Failed to dispatch process_dub: {e}")
            try:
                job.status = 'failed'
                db.session.commit()
            except Exception:
                db.session.rollback()
            session = db.session
            with session.begin():
                user = session.query(User).with_for_update().filter(User.id == current_user.id).one_or_none()
                if user:
                    user.credits += cost
                    session.add(CreditTransaction(user_id=user.id, transaction_type='credit', amount=cost, reason=f'refund dispatch failure {job.id}'))
            return jsonify({"error": "فشل توجيه المهمة"}), 500

        return jsonify({"success": True, "job_id": job.id})

    except Exception as e:
        logger.error(f"Upload Error: {e}")
        return jsonify({"error": "حدث خطأ أثناء الرفع للسحابة"}), 500

# ---------------------------
# Upload TTS
# ---------------------------
@app.route('/api/tts', methods=['POST'])
@token_required
def upload_tts(current_user):
    try:
        data = request.json or {}
        text = (data.get('text') or '').strip()
        lang = data.get('lang', 'en')
        raw_voice = data.get('voice_id', '')
        voice_name = _extract_voice_name(raw_voice) if raw_voice else ''
        sample_b64 = data.get('sample_b64', '') or ''

        if not text:
            return jsonify({"error": "النص فارغ"}), 400
        if len(text) > MAX_TTS_LENGTH:
            return jsonify({"error": f"النص طويل جداً (الحد {MAX_TTS_LENGTH} حرف)"}), 400

        cost = max(10, (len(text) // 100) * 10)
        if not deduct_credits_atomic(current_user.id, cost):
            return jsonify({"error": "رصيد غير كافٍ"}), 402

        job = DubbingJob(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            status='processing',
            language=lang,
            voice_mode=(voice_name or 'default')[:50],
            credits_used=cost,
            text_length=len(text),
            method='tts',
        )

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
            'voice_id': voice_name,
            'sample_b64': sample_b64,
        }
        try:
            if hasattr(process_smart_tts, 'delay'):
                process_smart_tts.delay(payload)
            elif hasattr(process_smart_tts, 'spawn'):
                process_smart_tts.spawn(payload)
            else:
                process_smart_tts(payload)
        except Exception as e:
            logger.error(f"Failed to dispatch smart tts: {e}")
            try:
                job.status = 'failed'
                db.session.commit()
            except Exception:
                db.session.rollback()
            session = db.session
            with session.begin():
                user = session.query(User).with_for_update().filter(User.id == current_user.id).one_or_none()
                if user:
                    user.credits += cost
                    session.add(CreditTransaction(user_id=user.id, transaction_type='credit', amount=cost, reason=f'refund dispatch failure {job.id}'))
            return jsonify({"error": "فشل توجيه المهمة"}), 500

        return jsonify({"success": True, "job_id": job.id})

    except Exception as e:
        logger.error(f"TTS Error: {e}")
        return jsonify({"error": "حدث خطأ أثناء معالجة النص"}), 500

# ---------------------------
# Get job
# ---------------------------
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

# ---------------------------
# Quick TTS via Modal fast stream 
# ---------------------------
@app.route('/api/tts/quick', methods=['POST'])
@token_required
def tts_quick(current_user):
    try:
        data = request.json or {}
        text = (data.get('text') or '').strip()
        if not text:
            return jsonify({"error": "النص فارغ"}), 400
        if len(text) > MAX_TTS_LENGTH:
            return jsonify({"error": f"النص طويل جداً (الحد {MAX_TTS_LENGTH} حرف)"}), 400

        cost = max(5, len(text) // 200 * 5)
        if not deduct_credits_atomic(current_user.id, cost):
            return jsonify({"error": "رصيد غير كافٍ"}), 402

        modal_fast_url = os.environ.get(
            'MODAL_TTS_FAST_URL',
            'https://your_workspace--sl-tts-factory-fasttts-fastapi-app.modal.run'
        )
        stream_url = f"{modal_fast_url.rstrip('/')}/tts/stream"
        modal_response = requests.post(
            stream_url,
            json={
                'text': text,
                'lang': data.get('lang', 'en'),
                'edge_voice': data.get('edge_voice', ''),
                'translate': data.get('translate', True),
                'rate': data.get('rate', '+0%'),
                'pitch': data.get('pitch', '+0Hz'),
            },
            stream=True,
            timeout=60,
        )

        if modal_response.status_code != 200:
            session = db.session
            with session.begin():
                user = session.query(User).with_for_update().filter(User.id == current_user.id).one_or_none()
                if user:
                    user.credits += cost
                    session.add(CreditTransaction(user_id=user.id, transaction_type='credit', amount=cost, reason='refund modal error'))
            return jsonify({"error": f"Modal error: {modal_response.status_code}"}), 500

        def generate():
            try:
                for chunk in modal_response.iter_content(chunk_size=4096):
                    if chunk:
                        yield chunk
            except Exception as e:
                logger.error(f"Stream error: {e}")

        return Response(
            generate(),
            mimetype='audio/mpeg',
            headers={
                'X-Voice': modal_response.headers.get('X-Voice', ''),
                'X-Cost': str(cost),
                'X-Remaining-Credits': str(current_user.credits),
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
        )
    except Exception as e:
        logger.error(f"Quick TTS Error: {e}")
        return jsonify({"error": "خطأ أثناء التوليد"}), 500

# ---------------------------
# Progress SSE
# ---------------------------
@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    def generate():
        last_status = None
        deadline = time.time() + 1800
        while time.time() < deadline:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job:
                    yield f"data: {json.dumps({'status': 'not_found'})}\n\n"
                    break
                payload = {'status': job.status, 'audio_url': job.output_url}
                if payload != last_status:
                    yield f"data: {json.dumps(payload)}\n\n"
                    last_status = payload
                if job.status in ('completed', 'failed'):
                    break
            time.sleep(2)
    return Response(generate(), mimetype='text/event-stream')

# ---------------------------
# Health & root
# ---------------------------
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': int(time.time())})

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        'service': 'sl-dubbing-backend',
        'status': 'running',
    })

# ---------------------------
# Run
# ---------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
