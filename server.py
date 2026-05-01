# server.py — V2.10 (Secure REST Supabase Auth, Atomic Credits, R2 Safety)
import os
import uuid
import logging
from functools import wraps
import requests
from werkzeug.utils import secure_filename

import boto3
from botocore.client import Config
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

from models import db, User, DubbingJob, CreditTransaction
from tasks import process_dub

load_dotenv()

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s"
)
logger = logging.getLogger("sl-dubbing")

# Basic config
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

# Supabase REST config
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

# R2 / S3 client
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
R2_PUBLIC_BASE = os.environ.get('R2_PUBLIC_BASE')

# CORS
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', 'https://sl-dubbing.github.io')
CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS.split(','))
COOKIE_NAME = os.environ.get('COOKIE_NAME', 'session')

# File restrictions
ALLOWED_EXTENSIONS = {'mp4', 'mp3', 'wav'}
MAX_FILE_SIZE_MB = int(os.environ.get('MAX_FILE_SIZE_MB', 200))  # example default 200MB

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_token_from_request():
    auth_header = request.headers.get('Authorization', '') or ''
    if auth_header.lower().startswith('bearer '):
        return auth_header.split()[1]
    return request.cookies.get(COOKIE_NAME)

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = get_token_from_request()
        if not token:
            return jsonify({'error': 'Unauthorized - Missing Token'}), 401

        if not SUPABASE_URL or not SUPABASE_KEY:
            logger.error("SUPABASE_URL or SUPABASE_KEY missing")
            return jsonify({'error': 'Server configuration error'}), 500

        try:
            headers = {
                'Authorization': f'Bearer {token}',
                'apikey': SUPABASE_KEY
            }
            # timeout to avoid hanging requests
            resp = requests.get(f"{SUPABASE_URL.rstrip('/')}/auth/v1/user", headers=headers, timeout=5)
            if resp.status_code != 200:
                logger.warning("Supabase auth rejected token status=%s", resp.status_code)
                return jsonify({'error': 'Invalid or expired session'}), 401

            user_data = resp.json() or {}
            # Supabase may return user object directly or nested; handle common shapes
            email = user_data.get('email') or (user_data.get('user') or {}).get('email')
            meta = user_data.get('user_metadata') or (user_data.get('user') or {}).get('user_metadata') or {}

            if not email:
                logger.warning("Supabase returned no email in user payload")
                return jsonify({'error': 'Invalid user data'}), 401

            current_user = User.query.filter_by(email=email).first()
            if not current_user:
                current_user = User(
                    email=email,
                    name=meta.get('full_name', meta.get('name', email.split('@')[0])),
                    avatar=meta.get('avatar_url'),
                    credits=500
                )
                db.session.add(current_user)
                db.session.commit()
        except requests.Timeout:
            logger.error("Timeout while contacting Supabase for token validation")
            return jsonify({'error': 'Auth provider timeout'}), 503
        except Exception as e:
            logger.exception("Auth logic failure: %s", e)
            return jsonify({'error': 'Server error during auth'}), 500

        return f(current_user, *args, **kwargs)
    return decorated

def deduct_credits_atomic(user_id, amount, job_id=None):
    try:
        user = User.query.with_for_update().get(user_id)
        if not user or (user.credits or 0) < amount:
            return False
        user.credits -= amount
        tx = CreditTransaction(user_id=user_id, amount=amount, transaction_type='debit', job_id=job_id)
        db.session.add(tx)
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        logger.exception("Credit deduction failed for user_id=%s job_id=%s: %s", user_id, job_id, e)
        return False

@app.route('/api/user', methods=['GET'])
@app.route('/api/user/credits', methods=['GET'])
@token_required
def get_user_data(current_user):
    user_dict = current_user.to_dict()
    avatar_key = getattr(current_user, 'avatar_key', None)
    if avatar_key and R2_PUBLIC_BASE:
        user_dict['avatar'] = f"{R2_PUBLIC_BASE.rstrip('/')}/{avatar_key}"
    return jsonify({'success': True, 'user': user_dict})

@app.route('/api/dubbing', methods=['POST'])
@token_required
def start_dubbing_route(current_user):
    cost = int(os.environ.get('DUB_COST', 100))

    # File presence and basic validation
    file = request.files.get('media_file')
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    filename = secure_filename(file.filename or "")
    if not allowed_file(filename):
        return jsonify({"error": "Unsupported file type"}), 400

    # Check size if provided (werkzeug FileStorage may have content_length)
    content_length = request.content_length or 0
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    if content_length and content_length > max_bytes:
        return jsonify({"error": "File too large"}), 413

    # Create job early so we can link transactions
    job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='processing',
        language=request.form.get('lang', 'ar'),
        credits_used=cost
    )
    db.session.add(job)
    db.session.flush()  # get job.id without commit

    # Deduct credits atomically and link to job
    if not deduct_credits_atomic(current_user.id, cost, job_id=job.id):
        db.session.rollback()
        return jsonify({"error": "Insufficient credits"}), 402

    # Upload file to R2 with error handling
    file_key = f"uploads/{uuid.uuid4()}_{filename}"
    try:
        # ensure file pointer at start
        file.stream.seek(0)
        s3_client.upload_fileobj(file.stream, R2_BUCKET_NAME, file_key)
    except Exception as e:
        db.session.rollback()
        logger.exception("File upload failed for user_id=%s job_id=%s: %s", current_user.id, job.id, e)
        return jsonify({"error": "File upload failed"}), 500

    # finalize job record
    try:
        job.file_key = file_key
        db.session.add(job)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception("Failed to finalize job record job_id=%s: %s", job.id, e)
        return jsonify({"error": "Server error creating job"}), 500

    # enqueue processing
    try:
        process_dub.delay({
            'job_id': job.id,
            'file_key': file_key,
            'lang': job.language,
            'voice_id': request.form.get('voice_id', 'source')
        })
    except Exception as e:
        logger.exception("Failed to enqueue job_id=%s: %s", job.id, e)

    return jsonify({"success": True, "job_id": job.id})

@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def get_job_status(current_user, job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'status': job.status,
        'audio_url': getattr(job, 'output_url', None),
        'credits_used': getattr(job, 'credits_used', None)
    })

@app.route('/api/health', methods=['GET'])
def health():
    db_ok = True
    try:
        db.session.execute("SELECT 1")
    except Exception:
        db_ok = False
    return jsonify({
        'status': 'online',
        'server': os.environ.get('PLATFORM', 'unknown'),
        'db': 'ok' if db_ok else 'error',
        'supabase_configured': bool(SUPABASE_URL and SUPABASE_KEY)
    })

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
