# app.py — V3.2 (Supabase Atomic Credits Deduction — RPC + Safe Fallback)
# Notes:
# 1) For true atomicity you should create the SQL function `decrement_credits` in your Supabase DB:
#    CREATE FUNCTION public.decrement_credits(uid uuid, amt integer)
#    RETURNS TABLE(credits integer) AS $$
#    UPDATE public.users
#    SET credits = credits - amt
#    WHERE id = uid AND credits >= amt
#    RETURNING credits;
#    $$ LANGUAGE sql VOLATILE;
#
#    Then the code below will call the RPC endpoint /rest/v1/rpc/decrement_credits
#
# 2) If you cannot create the RPC, the code falls back to a conditional PATCH using PostgREST filters.
#    That fallback is best-effort but not perfectly race-free. Use the RPC approach for production.

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
from models import db, User, DubbingJob, CreditTransaction
from tasks import process_dub

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')

# CORS
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

# Database
DATABASE_URL = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

# S3 (Cloudflare R2)
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
    region_name='auto'
)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'mp3', 'wav', 'ogg', 'm4a'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Supabase settings
SUPABASE_URL = os.environ.get("SUPABASE_URL")  # e.g. https://xyz.supabase.co
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # service role key required for updates
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

REQUEST_TIMEOUT = 8  # seconds for HTTP calls to Supabase

def get_supabase_user_credits(user_id):
    """Fetch user credits from Supabase users table via REST."""
    try:
        url = f"{SUPABASE_URL}/rest/v1/users"
        params = {"id": f"eq.{user_id}"}
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}"
        }
        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, list):
            return int(data[0].get("credits", 0))
        return 0
    except Exception as e:
        logging.exception("Failed to fetch credits from Supabase")
        # On error, return local cached value if available (best-effort)
        try:
            user = User.query.filter_by(id=user_id).first()
            return int(user.credits) if user else 0
        except Exception:
            return 0

def deduct_supabase_credits_atomic(user_id, amount):
    """
    Attempt to atomically deduct credits using an RPC function (recommended).
    Fallback to a conditional PATCH if RPC is not available.
    Returns (success: bool, new_balance: int or None, error_message: str or None)
    """
    # 1) Try RPC call to decrement_credits(uid, amt)
    try:
        rpc_url = f"{SUPABASE_URL}/rest/v1/rpc/decrement_credits"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        payload = {"uid": user_id, "amt": amount}
        resp = requests.post(rpc_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        # If RPC exists and returns representation, it will be 200 with JSON array
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                new_credits = int(data[0].get("credits", 0))
                return True, new_credits, None
            # RPC returned empty -> insufficient funds
            return False, None, "Insufficient credits"
        # If RPC endpoint not found or other error, fall through to fallback
        logging.info("RPC call status %s, body: %s", resp.status_code, resp.text)
    except requests.exceptions.RequestException as e:
        logging.info("RPC call failed or not available: %s", str(e))
    except Exception as e:
        logging.exception("Unexpected error calling RPC")

    # 2) Fallback: conditional PATCH using PostgREST filter credits=gte.{amount}
    # This will only update if current credits >= amount. It is a single UPDATE statement on the server.
    # Note: this fallback computes new_balance from the server's returned representation if available.
    try:
        patch_url = f"{SUPABASE_URL}/rest/v1/users"
        # Query string: id=eq.{user_id}&credits=gte.{amount}
        params = {"id": f"eq.{user_id}", "credits": f"gte.{amount}"}
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        # We will set credits to current_server_credits - amount.
        # To avoid race as much as possible we first request the current value with the same filter.
        get_resp = requests.get(patch_url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if get_resp.status_code != 200:
            logging.info("Fallback GET before PATCH failed: %s %s", get_resp.status_code, get_resp.text)
            return False, None, "Failed to verify credits"
        rows = get_resp.json()
        if not rows:
            return False, None, "Insufficient credits"
        server_current = int(rows[0].get("credits", 0))
        new_balance = max(server_current - amount, 0)
        # Now attempt PATCH with the same filter; if another concurrent update changed credits below amount,
        # the filter will prevent the PATCH from matching any row.
        patch_resp = requests.patch(patch_url, headers=headers, params=params, json={"credits": new_balance}, timeout=REQUEST_TIMEOUT)
        # PostgREST returns 200 with representation if Prefer=return=representation and update succeeded.
        if patch_resp.status_code in (200, 204):
            # If 200, parse returned representation to get actual credits
            if patch_resp.status_code == 200:
                patched = patch_resp.json()
                if patched and isinstance(patched, list) and len(patched) > 0:
                    actual = int(patched[0].get("credits", new_balance))
                    return True, actual, None
            # 204 means no content but update succeeded; return our computed new_balance
            return True, new_balance, None
        else:
            logging.info("Fallback PATCH failed: %s %s", patch_resp.status_code, patch_resp.text)
            return False, None, "Failed to deduct credits"
    except Exception as e:
        logging.exception("Fallback deduction error")
        return False, None, "Deduction error"

# --- Auth Decorator ---
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return f(None, *args, **kwargs)

        auth = request.headers.get('Authorization', '')
        token = auth.split()[1] if 'Bearer ' in auth else request.cookies.get('session')
        if not token:
            return jsonify({'error': 'Unauthorized'}), 401

        try:
            data = jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=['HS256', 'HS384', 'HS512', 'RS256'],
                options={'verify_aud': False}
            )
            user_id = data.get('sub')
            email = data.get('email', '')

            if not user_id:
                return jsonify({'error': 'Invalid token payload'}), 401

            user = User.query.filter_by(id=user_id).first()
            if not user:
                # create local user record as cache/logging only
                user = User(id=user_id, email=email, credits=200000)
                db.session.add(user)
                db.session.commit()

            return f(user, *args, **kwargs)
        except Exception as e:
            logging.exception("Auth Error")
            return jsonify({'error': 'Invalid Session'}), 401
    return decorated

# --- Endpoints ---
@app.route('/api/user', methods=['GET', 'OPTIONS'])
@token_required
def get_user_info(current_user):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})
    supabase_credits = get_supabase_user_credits(current_user.id)
    # Keep local cache in sync (best-effort)
    try:
        current_user.credits = supabase_credits
        db.session.add(current_user)
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify({
        'id': current_user.id,
        'email': current_user.email,
        'credits': supabase_credits
    })

@app.route('/api/upload-url', methods=['POST', 'OPTIONS'])
@token_required
def get_upload_url(current_user):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})
    data = request.json or {}
    filename = data.get('filename', 'file.mp4')
    if not allowed_file(filename):
        return jsonify({'error': 'Invalid format'}), 400
    ext = filename.rsplit('.', 1)[-1].lower()
    file_key = f"uploads/u{current_user.id}/{uuid.uuid4().hex}.{ext}"
    url = s3_client.generate_presigned_url(
        'put_object',
        Params={'Bucket': R2_BUCKET_NAME, 'Key': file_key, 'ContentType': data.get('content_type')},
        ExpiresIn=3600
    )
    return jsonify({'success': True, 'upload_url': url, 'file_key': file_key})

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@token_required
def start_dub(current_user):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})
    data = request.json or {}
    file_key = data.get('file_key')
    with_lipsync = data.get('with_lipsync', False)
    return_video = data.get('return_video', True)
    sample_b64 = data.get('sample_b64', '')

    cost = 150 if with_lipsync else 100
    # Check current Supabase credits
    supabase_credits = get_supabase_user_credits(current_user.id)
    if supabase_credits < cost:
        return jsonify({'error': 'Insufficient credits'}), 402

    # Attempt atomic deduction
    success, new_balance, err = deduct_supabase_credits_atomic(current_user.id, cost)
    if not success:
        return jsonify({'error': err or 'Deduction failed'}), 500

    # Record local transaction and job
    try:
        # Update local cached credits
        current_user.credits = new_balance
        # Create a credit transaction record (if model exists)
        try:
            tx = CreditTransaction(id=str(uuid.uuid4()), user_id=current_user.id, amount=-cost, balance_after=new_balance, description='dubbing')
            db.session.add(tx)
        except Exception:
            # If CreditTransaction model not present or fails, continue without blocking
            logging.info("CreditTransaction record skipped or failed")
        job_id = str(uuid.uuid4())
        new_job = DubbingJob(
            id=job_id,
            user_id=current_user.id,
            status='pending',
            language=data.get('lang', 'ar'),
            method='dubbing',
            voice_id=data.get('voice_id', 'source'),
            file_key=file_key,
            credits_used=cost
        )
        db.session.add(new_job)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logging.exception("Failed to create job or transaction locally after deduction")
        # Attempt to refund in Supabase if local recording failed (best-effort)
        try:
            # Try to add the credits back
            deduct_success, _, _ = deduct_supabase_credits_atomic(current_user.id, -cost)
            if deduct_success:
                logging.info("Refunded credits due to local failure")
        except Exception:
            logging.exception("Refund attempt failed")
        return jsonify({'error': 'Internal error creating job'}), 500

    # Kick off background processing
    process_dub.delay({
        'job_id': job_id,
        'file_key': file_key,
        'lang': data.get('lang', 'ar'),
        'voice_id': data.get('voice_id', 'source'),
        'sample_b64': sample_b64,
        'with_lipsync': with_lipsync,
        'video_output': return_video
    })

    return jsonify({'success': True, 'job_id': job_id, 'credits_left': new_balance}), 202

@app.route('/api/job/<job_id>', methods=['GET', 'OPTIONS'])
@token_required
def check_job(current_user, job_id):
    if request.method == 'OPTIONS':
        return jsonify({'ok': True})
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != current_user.id:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'status': job.status, 'output_url': job.output_url, 'error': job.error_message})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
