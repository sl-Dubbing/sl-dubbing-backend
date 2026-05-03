# server.py — V3.1 Direct Upload + History Support
import os
import uuid
import logging
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import boto3
from botocore.client import Config
import requests as _requests

# Models & Tasks
from models import db, User, DubbingJob
from tasks import process_dub, process_tts, celery_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sl-dubbing-server")

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL'].replace("postgres://", "postgresql://")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me')
db.init_app(app)

# R2 Client Configuration
s3 = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
    region_name='auto'
)
R2_BUCKET = os.environ.get('R2_BUCKET_NAME', 'sl-dubbing-media')

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else None
        if not token: return jsonify({'error': 'Token missing'}), 401
        
        # Verify with Supabase
        r = _requests.get(f"{os.environ.get('SUPABASE_URL')}/auth/v1/user", 
                          headers={"Authorization": f"Bearer {token}", "apikey": os.environ.get('SUPABASE_KEY', '')})
        if r.status_code != 200: return jsonify({'error': 'Invalid token'}), 401
        
        supa_user = r.json()
        user = User.query.filter_by(email=supa_user.get('email')).first()
        if not user:
            user = User(email=supa_user.get('email'), name=supa_user.get('email').split('@')[0], 
                        supabase_id=supa_user.get('id'), credits=10)
            db.session.add(user)
            db.session.commit()
        return f(user, *args, **kwargs)
    return decorated

@app.route('/api/history', methods=['GET'])
@token_required
def get_history(user):
    jobs = DubbingJob.query.filter_by(user_id=user.id).order_by(DubbingJob.created_at.desc()).limit(50).all()
    history = [{
        "id": j.id, "lang": j.lang, "kind": j.kind or 'dub', "status": j.status,
        "audio_url": j.audio_url, "filename": j.input_key.split('/')[-1] if j.input_key else "معالجة",
        "created_at": j.created_at.isoformat() if j.created_at else None
    } for j in jobs]
    return jsonify({"success": True, "history": history})

@app.route('/api/upload-url', methods=['POST'])
@token_required
def get_upload_url(user):
    data = request.get_json() or {}
    file_key = f"uploads/u{user.id}/{uuid.uuid4().hex}.mp4"
    try:
        url = s3.generate_presigned_url('put_object', Params={'Bucket': R2_BUCKET, 'Key': file_key, 'ContentType': data.get('content_type', 'video/mp4')}, ExpiresIn=3600, HttpMethod='PUT')
        return jsonify({'success': True, 'upload_url': url, 'file_key': file_key})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/dub', methods=['POST'])
@token_required
def start_dubbing(user):
    data = request.get_json() or {}
    file_key = data.get('file_key')
    media_url = s3.generate_presigned_url('get_object', Params={'Bucket': R2_BUCKET, 'Key': file_key}, ExpiresIn=7200)
    
    job = DubbingJob(user_id=user.id, kind='dub', lang=data.get('lang', 'en'), voice_id=data.get('voice_id', 'source'), 
                     engine=data.get('engine', ''), status='queued', input_key=file_key, created_at=datetime.utcnow())
    db.session.add(job)
    db.session.commit()
    
    process_dub.delay(job_id=job.id, media_url=media_url, lang=job.lang, voice_id=job.voice_id, sample_b64=data.get('sample_b64', ''), engine=job.engine)
    return jsonify({'success': True, 'job_id': job.id})

@app.route('/api/job/<int:job_id>', methods=['GET'])
@token_required
def job_status(user, job_id):
    job = DubbingJob.query.filter_by(id=job_id, user_id=user.id).first()
    if not job: return jsonify({'error': 'Not found'}), 404
    return jsonify({'id': job.id, 'status': job.status, 'audio_url': job.audio_url, 'error': job.error})

@app.route('/api/user/credits', methods=['GET'])
@token_required
def get_credits(user):
    return jsonify({'success': True, 'credits': user.credits})

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
