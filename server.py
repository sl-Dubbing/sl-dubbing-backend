# server.py — V2.11 (Secure REST Supabase Auth, Atomic Credits, R2 Safety)
import os
import uuid
import logging
from functools import wraps
from datetime import timedelta

import requests
from werkzeug.utils import secure_filename

import boto3
from botocore.client import Config
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from dotenv import load_dotenv

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

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
R2_ENDPOINT_URL = os.environ.get('R2_ENDPOINT_URL')
R2_ACCESS_KEY_ID = os.environ.get('R2_ACCESS_KEY_ID')
R2_SECRET_ACCESS_KEY = os.environ.get('R2_SECRET_ACCESS_KEY')
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
R2_PUBLIC_BASE = os.environ.get('R2_PUBLIC_BASE')

# Validate R2 config at startup (fail fast)
if not all([R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME]):
    logger.warning("R2 configuration incomplete. File uploads will fail until R2 env vars are set.")

s3_client = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version='s3v4'),
)

# CORS
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get('ALLOWED_ORIGINS', 'https://sl-dubbing.github.io').split(',') if o.strip()]
if not ALLOWED_ORIGINS:
    ALLOWED_ORIGINS = ['https://sl-dubbing.github.io']

CORS(
    app,
    supports_credentials=True,
    origins=ALLOWED_ORIGINS,
    allow_headers=['Content-Type', 'Authorization', 'apikey', 'X-Requested-With'],
    expose_headers=['Content-Type', 'Content-Length'],
    methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    max_age=timedelta(hours=1)
)

COOKIE_NAME = os.environ.get('COOKIE_NAME', 'session')

# File restrictions
ALLOWED_EXTENSIONS = {'mp4', 'mp3', 'wav'}
MAX_FILE_SIZE_MB = int(os.environ.get('MAX_FILE_SIZE_MB', 200))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

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
            resp = requests.get(f"{SUPABASE_URL.rstrip('/')}/auth/v1/user", headers=headers, timeout=5)
            if resp.status_code != 200:
                logger.warning("Supabase auth rejected token status=%s", resp.status_code)
                return jsonify({'error': 'Invalid or expired session'}), 401

            user_data = resp.json() or {}
            email = user_data.get('email') or (user_data.get('user') or {}).get('email')
            meta = user_data.get('user_metadata') or (user_data.get('user') or {}).get('user_metadata') or {}

            if not email:
                logger.warning("Supabase returned no email in user payload")
                return jsonify({'error': 'Invalid user data'}), 401

            # Ensure user exists in our DB
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

def deduct_credits_atomic(session, user_id, amount, job_id=None):
    """
    Deduct credits using the provided session and a SELECT ... FOR UPDATE lock.
    Returns True on success, False if insufficient credits or error.
    """
    try:
        stmt = select(User).where(User.id == user_id).with_for_update()
        user = session.execute(stmt).scalar_one_or_none()
        if not user:
            logger.warning("User not found for credit deduction user_id=%s", user_id)
            return False
        if (user.credits or 0) < amount:
            logger.info("Insufficient credits for user_id=%s need=%s have=%s", user_id, amount, user.credits)
            return False
        user.credits -= amount
        tx = CreditTransaction(user_id=user_id, amount=amount, transaction_type='debit', job_id=job_id)
        session.add(tx)
        return True
    except SQLAlchemyError as e:
        logger.exception("SQLAlchemy error during credit deduction for user_id=%s job_id=%s: %s", user_id, job_id, e)
        return False
    except Exception as e:
        logger.exception("Unexpected error during credit deduction for user_id=%s job_id=%s: %s", user_id, job_id, e)
        return False

def get_file_size(file_storage):
    """
    Attempt to determine file size reliably from the stream.
    Returns size in bytes or None if unknown.
    """
    try:
        stream = file_storage.stream
        current = stream.tell()
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(current)
        return size
    except Exception:
        return None

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get('Origin')
    if origin and origin in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

@app.route('/api/user', methods=['GET'])
@app.route('/api/user/credits', methods=['GET'])
@token_required
def get_user_data(current_user):
    user_dict = current_user.to_dict()
    avatar_key = getattr(current_user, 'avatar_key', None)
    if avatar_key and R2_PUBLIC_BASE:
        user_dict['avatar'] = f"{R2_PUBLIC_BASE.rstrip('/')}/{avatar_key}"
    return jsonify({'success': True, 'user': user_dict})

@app.route('/api/dubbing', methods=['POST', 'OPTIONS'])
@token_required
def start_dubbing_route(current_user):
    # Handle preflight quickly
    if request.method == 'OPTIONS':
        resp = make_response()
        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '')
        resp.headers['Access-Control-Allow-Credentials'] = 'true'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, apikey, X-Requested-With'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        return resp

    cost = int(os.environ.get('DUB_COST', 100))

    # File presence and basic validation
    file = request.files.get('media_file')
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    filename = secure_filename(file.filename or "")
    if not filename or not allowed_file(filename):
        return jsonify({"error": "Unsupported file type"}), 400

    # Check size: prefer content_length but fall back to stream size
    content_length = request.content_length or 0
    if content_length and content_length > MAX_FILE_SIZE_BYTES:
        return jsonify({"error": "File too large"}), 413

    size = get_file_size(file)
    if size and size > MAX_FILE_SIZE_BYTES:
        return jsonify({"error": "File too large"}), 413

    # Prepare file key
    file_key = f"uploads/{uuid.uuid4()}_{filename}"

    # Create job and deduct credits in a single DB transaction
    try:
        with db.session.begin():
            job = DubbingJob(
                id=str(uuid.uuid4()),
                user_id=current_user.id,
                status='processing',
                language=request.form.get('lang', 'ar'),
                credits_used=cost
            )
            db.session.add(job)
            db.session.flush()  # ensure job.id is available

            # Deduct credits using the same session
            ok = deduct_credits_atomic(db.session, current_user.id, cost, job_id=job.id)
            if not ok:
                # raising will rollback the transaction
                raise ValueError("Insufficient credits")
            # Do not set file_key yet; upload happens outside transaction
    except ValueError:
        return jsonify({"error": "Insufficient credits"}), 402
    except Exception as e:
        logger.exception("Failed to create job and deduct credits: %s", e)
        return jsonify({"error": "Server error creating job"}), 500

    # Upload file to R2 outside DB transaction to avoid long locks
    try:
        file.stream.seek(0)
        s3_client.upload_fileobj(file.stream, R2_BUCKET_NAME, file_key)
    except Exception as e:
        logger.exception("File upload failed for user_id=%s job_id=%s: %s", current_user.id, job.id, e)
        # Mark job failed and refund in a new transaction
        try:
            with db.session.begin():
                job_db = db.session.get(DubbingJob, job.id)
                if job_db:
                    job_db.status = 'failed'
                    job_db.file_key = None
                    db.session.add(job_db)
                # refund
                refund = CreditTransaction(user_id=current_user.id, amount=cost, transaction_type='credit', job_id=job.id)
                db.session.add(refund)
                user_db = db.session.get(User, current_user.id)
                if user_db:
                    user_db.credits = (user_db.credits or 0) + cost
                    db.session.add(user_db)
        except Exception:
            logger.exception("Failed to refund after upload failure for job_id=%s", job.id)
        return jsonify({"error": "File upload failed"}), 500

    # finalize job record with file_key
    try:
        with db.session.begin():
            job_db = db.session.get(DubbingJob, job.id)
            if not job_db:
                logger.error("Job disappeared before finalizing job_id=%s", job.id)
                return jsonify({"error": "Server error creating job"}), 500
            job_db.file_key = file_key
            db.session.add(job_db)
    except Exception as e:
        logger.exception("Failed to finalize job record job_id=%s: %s", job.id, e)
        # Attempt to mark job failed and refund
        try:
            with db.session.begin():
                job_db = db.session.get(DubbingJob, job.id)
                if job_db:
                    job_db.status = 'failed'
                    db.session.add(job_db)
                refund = CreditTransaction(user_id=current_user.id, amount=cost, transaction_type='credit', job_id=job.id)
                db.session.add(refund)
                user_db = db.session.get(User, current_user.id)
                if user_db:
                    user_db.credits = (user_db.credits or 0) + cost
                    db.session.add(user_db)
        except Exception:
            logger.exception("Failed to refund after finalize failure for job_id=%s", job.id)
        return jsonify({"error": "Server error creating job"}), 500

    # enqueue processing (best-effort)
    try:
        process_dub.delay({
            'job_id': job.id,
            'file_key': file_key,
            'lang': job.language,
            'voice_id': request.form.get('voice_id', 'source')
        })
    except Exception:
        logger.exception("Failed to enqueue job_id=%s", job.id)

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
        'supabase_configured': bool(SUPABASE_URL and SUPABASE_KEY),
        'r2_configured': bool(R2_ENDPOINT_URL and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME)
    })

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
