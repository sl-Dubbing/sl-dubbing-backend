# server.py — V3.3 The Master Version
import os
import uuid
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
import boto3
from botocore.client import Config
import requests as _requests

from models import db, User, DubbingJob 

# تجنب الخطأ إذا كان stt غير موجود حالياً في tasks
try:
    from tasks import process_dub, process_tts, process_stt
except ImportError:
    from tasks import process_dub, process_tts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sl-dubbing-server")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL'].replace("postgres://", "postgresql://")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me')
db.init_app(app)

s3 = boto3.client('s3', 
                  endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
                  aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
                  aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
                  config=Config(signature_version='s3v4'), 
                  region_name='auto')
R2_BUCKET = os.environ.get('R2_BUCKET_NAME', 'sl-dubbing-media')

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else None
        if not token: return jsonify({'error': 'Token missing'}), 401
        r = _requests.get(f"{os.environ.get('SUPABASE_URL')}/auth/v1/user", 
                          headers={"Authorization": f"Bearer {token}", 
                                   "apikey": os.environ.get('SUPABASE_KEY', '')})
        if r.status_code != 200: return jsonify({'error': 'Invalid token'}), 401
        supa_user = r.json()
        user = User.query.filter_by(email=supa_user.get('email')).first()
        if not user:
            user = User(email=supa_user.get('email'), name=supa_user.get('email').split('@')[0], credits=10)
            db.session.add(user)
            db.session.commit()
        return f(user, *args, **kwargs)
    return decorated

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'version': 'v3.3-master'})

@app.route('/api/user/credits', methods=['GET'])
@token_required
def get_credits(user):
    return jsonify({'success': True, 'credits': user.credits})

# =================📂 مدير الملفات=================
@app.route('/api/history', methods=['GET'])
@token_required
def get_history(user):
    limit_date = datetime.utcnow() - timedelta(days=15)
    jobs = DubbingJob.query.filter(DubbingJob.user_id == user.id, DubbingJob.created_at >= limit_date)\
                           .order_by(DubbingJob.created_at.desc()).all()
    return jsonify({
        "success": True,
        "history": [{
            "id": j.id,
            "name": j.custom_name or (j.input_key.split('/')[-1] if j.input_key else ("نص إلى صوت" if j.kind == 'tts' else "معالجة")),
            "folder": j.folder_name or "الرئيسية",
            "lang": j.lang,
            "status": j.status,
            "audio_url": j.audio_url,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "expires_in": max(0, 15 - (datetime.utcnow() - j.created_at).days) if j.created_at else 0
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
    return jsonify({"error": "Not found"}), 404

@app.route('/api/file/move', methods=['POST'])
@token_required
def move_file(user):
    data = request.json
    job = DubbingJob.query.filter_by(id=data.get('id'), user_id=user.id).first()
    if job:
        job.folder_name = data.get('folder_name')
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"error": "Not found"}), 404

# =================🚀 الرفع والدبلجة=================
@app.route('/api/upload-url', methods=['POST'])
@token_required
def get_upload_url(user):
    data = request.get_json() or {}
    ext = data.get('filename', 'file.mp4').split('.')[-1]
    file_key = f"uploads/u{user.id}/{uuid.uuid4().hex}.{ext}"
    url = s3.generate_presigned_url('put_object', Params={'Bucket': R2_BUCKET, 'Key': file_key, 'ContentType': data.get('content_type', 'video/mp4')}, ExpiresIn=3600, HttpMethod='PUT')
    return jsonify({'success': True, 'upload_url': url, 'file_key': file_key})

@app.route('/api/dub', methods=['POST'])
@token_required
def start_dubbing(user):
    data = request.get_json() or {}
    file_key = data.get('file_key')
    if not file_key: return jsonify({'error': 'file_key missing'}), 400
    
    media_url = s3.generate_presigned_url('get_object', Params={'Bucket': R2_BUCKET, 'Key': file_key}, ExpiresIn=7200)
    
    job = DubbingJob(
        user_id=user.id, kind='dub', lang=data.get('lang', 'en'), 
        voice_id=data.get('voice_id', 'source'), engine=data.get('engine', 'auto'),
        status='queued', input_key=file_key, custom_name=data.get('filename'),
        created_at=datetime.utcnow()
    )
    db.session.add(job)
    db.session.commit()
    
    # إرسال المتغيرات الكاملة لمهمة الـ Celery
    process_dub.delay(
        job_id=job.id, media_url=media_url, lang=job.lang, 
        voice_id=job.voice_id, sample_b64=data.get('sample_b64', ''), engine=job.engine
    )
    return jsonify({'success': True, 'job_id': job.id})

# =================🎙️ تحويل النص=================
@app.route('/api/tts', methods=['POST'])
@token_required
def start_tts(user):
    data = request.get_json() or {}
    text = data.get('text', '').strip()
    if not text: return jsonify({'error': 'Text required'}), 400
    
    job = DubbingJob(user_id=user.id, kind='tts', lang=data.get('lang', 'ar'), 
                     status='queued', custom_name=data.get('filename', text[:20]),
                     created_at=datetime.utcnow())
    db.session.add(job)
    db.session.commit()
    
    process_tts.delay(job_id=job.id, text=text, lang=job.lang, 
                      voice_id=data.get('voice_id', ''), sample_b64=data.get('sample_b64', ''))
    return jsonify({'success': True, 'job_id': job.id})

@app.route('/api/tts/quick', methods=['POST'])
@token_required
def tts_quick(user):
    from flask import Response, stream_with_context
    import edge_tts
    import asyncio

    data = request.get_json() or {}
    text = data.get('text', '').strip()
    lang = data.get('lang', 'ar')

    voice = "ar-SA-HamedNeural"
    if lang.startswith("en"): voice = "en-US-AriaNeural"
    
    def generate():
        async def stream():
            comm = edge_tts.Communicate(text, voice)
            async for chunk in comm.stream():
                if chunk["type"] == "audio": yield chunk["data"]
        loop = asyncio.new_event_loop()
        agen = stream()
        while True:
            try: yield loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration: break
        loop.close()

    return Response(stream_with_context(generate()), mimetype='audio/mpeg')

@app.route('/api/job/<int:job_id>', methods=['GET'])
@token_required
def job_status(user, job_id):
    job = DubbingJob.query.filter_by(id=job_id, user_id=user.id).first()
    if not job: return jsonify({'error': 'Not found'}), 404
    return jsonify({'id': job.id, 'status': job.status, 'audio_url': job.audio_url, 'error': job.error})

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
