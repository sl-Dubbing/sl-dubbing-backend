# server.py — V3.0 Direct Upload Architecture
"""
🚀 المعمارية الجديدة:
  Browser → presigned URL → R2 (مباشر، بدون Railway)
  Browser → /api/dub (file_key) → Celery → Modal (يقرأ من R2)
  
✅ Railway bandwidth: 0%
✅ سرعة الرفع: 3x أسرع
✅ يدعم ملفات حتى 5GB
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
R2_PUBLIC_URL = os.environ.get('R2_PUBLIC_URL', '')  # اختياري للتنزيل المباشر

s3 = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    config=Config(signature_version='s3v4'),
    region_name='auto'
)

# ==========================================
# 🔐 Auth: Supabase JWT verification
# ==========================================
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://ckjkkxrlgisjdolwddfg.supabase.co')

def verify_supabase_token(token):
    """التحقق من Supabase JWT بالاتصال بـ Supabase API"""
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

        # تحقّق من Supabase
        supa_user = verify_supabase_token(token)
        if not supa_user:
            return jsonify({'error': 'Invalid token'}), 401

        # ابحث/أنشئ المستخدم في DB المحلي
        user = User.query.filter_by(email=supa_user.get('email')).first()
        if not user:
            user = User(
                email=supa_user.get('email'),
                name=supa_user.get('user_metadata', {}).get('full_name') or supa_user.get('email').split('@')[0],
                supabase_id=supa_user.get('id'),
                credits=10  # ترحيب
            )
            db.session.add(user)
            db.session.commit()

        return f(user, *args, **kwargs)
    return decorated

# ==========================================
# 📊 Endpoints
# ==========================================
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'version': 'v3.0-direct-upload'})


@app.route('/api/user', methods=['GET'])
@token_required
def get_user(user):
    return jsonify({
        'success': True,
        'user': {
            'id': user.id,
            'email': user.email,
            'name': user.name,
            'credits': user.credits,
            'created_at': user.created_at.isoformat() if user.created_at else None
        }
    })


# ==========================================
# 🚀 NEW: Direct Upload Endpoints
# ==========================================
@app.route('/api/upload-url', methods=['POST'])
@token_required
def get_upload_url(user):
    """
    🎯 يولّد presigned URL ليرفع المتصفح ملفه مباشرة لـ R2
    
    Request: { "filename": "video.mp4", "content_type": "video/mp4", "size": 52000000 }
    Response: { "upload_url": "...", "file_key": "uploads/abc123.mp4", "expires_in": 3600 }
    """
    data = request.get_json() or {}
    filename = data.get('filename', 'file')
    content_type = data.get('content_type', 'application/octet-stream')
    size = int(data.get('size', 0))

    # حدّ الحجم (5GB)
    MAX_SIZE = 5 * 1024 * 1024 * 1024
    if size > MAX_SIZE:
        return jsonify({'error': f'File too large (max {MAX_SIZE//(1024**3)}GB)'}), 413

    # تحقق رصيد المستخدم
    if user.credits < 1:
        return jsonify({'error': 'Insufficient credits'}), 402

    # توليد file_key فريد
    ext = filename.rsplit('.', 1)[-1] if '.' in filename else 'mp4'
    file_key = f"uploads/u{user.id}/{uuid.uuid4().hex}.{ext}"

    try:
        # توليد presigned URL للـ PUT (المتصفح يرفع مباشرة)
        upload_url = s3.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': R2_BUCKET,
                'Key': file_key,
                'ContentType': content_type,
            },
            ExpiresIn=3600,  # ساعة
            HttpMethod='PUT'
        )

        logger.info(f"[user={user.id}] presigned URL: {file_key}")

        return jsonify({
            'success': True,
            'upload_url': upload_url,
            'file_key': file_key,
            'expires_in': 3600,
            'method': 'PUT',
            'headers': {
                'Content-Type': content_type
            }
        })
    except Exception as e:
        logger.exception("upload-url failed")
        return jsonify({'error': str(e)}), 500


@app.route('/api/dub', methods=['POST'])
@token_required
def start_dubbing(user):
    """
    🎬 يبدأ مهمة دبلجة بعد الرفع المباشر
    
    Request: {
      "file_key": "uploads/u1/abc.mp4",
      "lang": "ar",
      "voice_id": "source",
      "sample_b64": "" (اختياري),
      "engine": "" (auto)
    }
    """
    data = request.get_json() or {}
    file_key = data.get('file_key')
    lang = data.get('lang', 'en')
    voice_id = data.get('voice_id', 'source')
    sample_b64 = data.get('sample_b64', '')
    engine = data.get('engine', '')

    if not file_key:
        return jsonify({'error': 'file_key required'}), 400

    # تحقق وجود الملف في R2
    try:
        s3.head_object(Bucket=R2_BUCKET, Key=file_key)
    except Exception:
        return jsonify({'error': 'File not found in storage'}), 404

    # تحقق رصيد
    if user.credits < 1:
        return jsonify({'error': 'Insufficient credits'}), 402

    # توليد رابط مؤقت يقرأ منه Modal
    media_url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': R2_BUCKET, 'Key': file_key},
        ExpiresIn=7200  # ساعتان لـ Modal
    )

    # إنشاء job
    job = DubbingJob(
        user_id=user.id,
        lang=lang,
        voice_id=voice_id,
        engine=engine,
        status='queued',
        input_key=file_key,
        created_at=datetime.utcnow()
    )
    db.session.add(job)
    db.session.commit()

    # إرسال للـ Celery (لا يمر بأي ملف!)
    process_dub.delay(
        job_id=job.id,
        media_url=media_url,   # رابط R2 مباشر
        lang=lang,
        voice_id=voice_id,
        sample_b64=sample_b64,
        engine=engine,
    )

    logger.info(f"[user={user.id}] queued job={job.id} lang={lang}")

    return jsonify({
        'success': True,
        'job_id': job.id,
        'status': 'queued',
        'lang': lang,
        'engine': engine or 'auto'
    })


@app.route('/api/job/<int:job_id>', methods=['GET'])
@token_required
def job_status(user, job_id):
    job = DubbingJob.query.filter_by(id=job_id, user_id=user.id).first()
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    return jsonify({
        'id': job.id,
        'status': job.status,
        'lang': job.lang,
        'engine': job.engine,
        'audio_url': job.audio_url,
        'error': job.error,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'completed_at': job.completed_at.isoformat() if job.completed_at else None,
    })


@app.route('/api/jobs', methods=['GET'])
@token_required
def list_jobs(user):
    """ملفاتي — قائمة بآخر 50 مهمة"""
    jobs = DubbingJob.query.filter_by(user_id=user.id) \
                            .order_by(DubbingJob.created_at.desc()) \
                            .limit(50).all()
    return jsonify({
        'success': True,
        'jobs': [{
            'id': j.id,
            'lang': j.lang,
            'engine': j.engine,
            'status': j.status,
            'audio_url': j.audio_url,
            'created_at': j.created_at.isoformat() if j.created_at else None,
        } for j in jobs]
    })


# ==========================================
# 🎙️ TTS endpoint (يبقى كما هو، لا يحتاج upload)
# ==========================================
@app.route('/api/tts', methods=['POST'])
@token_required
def start_tts(user):
    data = request.get_json() or {}
    text = data.get('text', '').strip()
    lang = data.get('lang', 'ar')

    if not text:
        return jsonify({'error': 'Text required'}), 400
    if user.credits < 1:
        return jsonify({'error': 'Insufficient credits'}), 402

    job = DubbingJob(
        user_id=user.id, lang=lang, voice_id=data.get('voice_id', ''),
        status='queued', kind='tts', created_at=datetime.utcnow()
    )
    db.session.add(job)
    db.session.commit()

    process_tts.delay(
        job_id=job.id, text=text, lang=lang,
        sample_b64=data.get('sample_b64', ''),
        voice_id=data.get('voice_id', ''),
        rate=data.get('rate', '+0%'),
        pitch=data.get('pitch', '+0Hz'),
    )

    return jsonify({'success': True, 'job_id': job.id, 'status': 'queued'})


@app.route('/api/tts/quick', methods=['POST'])
@token_required
def tts_quick(user):
    """⚡ Quick streaming TTS"""
    from flask import Response, stream_with_context
    import edge_tts
    import asyncio

    data = request.get_json() or {}
    text = data.get('text', '').strip()
    lang = data.get('lang', 'ar')
    rate = data.get('rate', '+0%')
    pitch = data.get('pitch', '+0Hz')

    if not text:
        return jsonify({'error': 'Text required'}), 400
    if user.credits < 1:
        return jsonify({'error': 'Insufficient credits'}), 402

    voice_map = {
        "ar": "ar-SA-HamedNeural", "en": "en-US-AriaNeural",
        "fr": "fr-FR-DeniseNeural", "es": "es-ES-AlvaroNeural",
        "de": "de-DE-KatjaNeural", "tr": "tr-TR-EmelNeural",
    }
    voice = voice_map.get(lang.split('-')[0], "en-US-AriaNeural")

    def generate():
        async def stream():
            comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]

        loop = asyncio.new_event_loop()
        try:
            agen = stream()
            while True:
                try:
                    yield loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    break
        finally:
            loop.close()

    # خصم رصيد
    user.credits = max(0, user.credits - 1)
    db.session.commit()

    return Response(
        stream_with_context(generate()),
        mimetype='audio/mpeg',
        headers={'X-Remaining-Credits': str(user.credits)}
    )


# ==========================================
# 🚪 Logout
# ==========================================
@app.route('/api/logout', methods=['POST'])
def logout():
    return jsonify({'success': True})


# ==========================================
# 🚀 Main
# ==========================================
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
