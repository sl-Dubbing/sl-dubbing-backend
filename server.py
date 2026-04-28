# server.py — V2.3 (معدّل لاستخدام HttpOnly cookies)
import os
import uuid
import json
import logging
import time
import base64
import datetime as _dt
from functools import wraps

import jwt
import requests
import boto3
from botocore.client import Config
from flask import Flask, request, jsonify, Response, make_response
from flask_cors import CORS
from dotenv import load_dotenv

from models import db, User, DubbingJob, CreditTransaction

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# الإعدادات الأساسية
# ==========================================
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# إعداد Cloudflare R2 Client
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME', 'sl-dubbing-media')
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)

# Origins المسموح بها
ALLOWED_ORIGINS = [
    "https://sl-dubbing.github.io",
    "https://sl-dubbing-frontend.vercel.app",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5500",
]
extra_origins = os.environ.get('EXTRA_CORS_ORIGINS', '')
if extra_origins:
    ALLOWED_ORIGINS += [o.strip() for o in extra_origins.split(',') if o.strip()]

# CORS مع دعم credentials لأننا نستخدم HttpOnly cookies
CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS)

from tasks import process_dub, process_smart_tts

MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', 100))
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

# إعدادات الكوكي من env
COOKIE_NAME = os.environ.get('COOKIE_NAME', 'session')
COOKIE_MAX_AGE = int(os.environ.get('COOKIE_MAX_AGE', 86400 * 7))  # 7 أيام افتراضياً
COOKIE_SAMESITE = os.environ.get('COOKIE_SAMESITE', 'None')  # 'Lax' أو 'Strict' أو 'None'
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', 'true').lower() in ('1', 'true', 'yes')
COOKIE_DOMAIN = os.environ.get('COOKIE_DOMAIN') or None

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

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header:
            parts = auth_header.split()
            if len(parts) == 2 and parts[0].lower() == 'bearer':
                token = parts[1]

        # fallback: read from HttpOnly cookie
        if not token:
            token = request.cookies.get(COOKIE_NAME)

        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_user = User.query.get(data['user_id'])
            if not current_user:
                raise Exception("User not found")
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except Exception as e:
            logger.warning(f"Token decode error: {e}")
            return jsonify({'error': 'Invalid token'}), 401
        return f(current_user, *args, **kwargs)
    return decorated

@app.route('/api/auth/google', methods=['POST'])
def google_auth():
    try:
        data = request.json or {}
        google_token = data.get('credential')
        if not google_token:
            return jsonify({'error': 'No credential provided'}), 400

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
            user.last_login = _dt.datetime.utcnow()
            db.session.commit()

        # أنشئ JWT قصير العمر (مثال: 1 يوم)
        token_exp = int(time.time()) + (86400 * 1)
        my_token = jwt.encode(
            {'user_id': user.id, 'exp': token_exp},
            app.config['SECRET_KEY'],
            algorithm="HS256",
        )

        # أعد JSON مع بيانات المستخدم فقط، وضع التوكن في HttpOnly cookie
        resp = make_response(jsonify({'success': True, 'user': user.to_dict()}))
        # Flask set_cookie يقبل samesite كـ 'None' string في الإصدارات الحديثة
        resp.set_cookie(
            COOKIE_NAME,
            my_token,
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite=COOKIE_SAMESITE,
            path='/',
            domain=COOKIE_DOMAIN
        )
        return resp

    except Exception as e:
        logger.error(f"Google Auth Error: {e}")
        return jsonify({'error': 'حدث خطأ في السيرفر أثناء تسجيل الدخول'}), 500

@app.route('/api/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie(
        COOKIE_NAME,
        '',
        expires=0,
        max_age=0,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path='/',
        domain=COOKIE_DOMAIN
    )
    return resp

@app.route('/api/user', methods=['GET'])
@token_required
def get_user_data(current_user):
    return jsonify({'success': True, 'user': current_user.to_dict()})

@app.route('/api/dub', methods=['POST'])
@token_required
def upload_dub(current_user):
    try:
        cost = int(os.environ.get('DUB_COST', 100))
        if (current_user.credits or 0) < cost:
            return jsonify({"error": "رصيد غير كافٍ"}), 402

        if 'media_file' not in request.files:
            return jsonify({"error": "يرجى اختيار ملف"}), 400

        file = request.files['media_file']
        if not file or not file.filename:
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

        current_user.credits -= cost
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
        process_dub.delay(payload)

        return jsonify({"success": True, "job_id": job.id})

    except Exception as e:
        logger.error(f"Upload Error: {e}")
        return jsonify({"error": "حدث خطأ أثناء الرفع للسحابة"}), 500

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

        cost = max(10, (len(text) // 100) * 10)
        if (current_user.credits or 0) < cost:
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
            'voice_id': voice_name,
            'sample_b64': sample_b64,
        }
        process_smart_tts.delay(payload)

        return jsonify({"success": True, "job_id": job.id})

    except Exception as e:
        logger.error(f"TTS Error: {e}")
        return jsonify({"error": "حدث خطأ أثناء معالجة النص"}), 500

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

@app.route('/api/tts/quick', methods=['POST'])
@token_required
def tts_quick(current_user):
    try:
        data = request.json or {}
        text = (data.get('text') or '').strip()
        if not text:
            return jsonify({"error": "النص فارغ"}), 400
        cost = max(5, len(text) // 200 * 5)
        if (current_user.credits or 0) < cost:
            return jsonify({"error": "رصيد غير كافٍ"}), 402
        current_user.credits -= cost
        db.session.add(CreditTransaction(
            user_id=current_user.id,
            transaction_type='debit',
            amount=cost,
            reason='Quick TTS',
        ))
        db.session.commit()

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
            current_user.credits += cost
            db.session.commit()
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

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': int(time.time())})

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        'service': 'sl-dubbing-backend',
        'status': 'running',
    })

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
