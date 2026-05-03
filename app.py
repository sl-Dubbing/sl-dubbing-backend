# app.py — V3.4 (The "System Online" Fix + Full UUID Sync)
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

# 1. إعدادات CORS المتطورة
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

# 2. ربط قاعدة البيانات
DATABASE_URL = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

# 3. إعدادات R2
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
    region_name='auto'
)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')

# 4. إعدادات Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

# --- 🌟 المسار السحري الذي سيجعل النظام "متصل" ---
@app.route('/api/health', methods=['GET'])
def health_check():
    """هذا المسار يخبر الواجهة الأمامية أن السيرفر حي ويرزق"""
    return jsonify({'status': 'online', 'message': 'Server is running smoothly'}), 200

# --- وظائف الرصيد والمصادقة ---

def get_supabase_user_credits(user_id):
    try:
        url = f"{SUPABASE_URL}/rest/v1/users"
        params = {"id": f"eq.{user_id}", "select": "credits"}
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        resp = requests.get(url, headers=headers, params=params, timeout=5)
        data = resp.json()
        return int(data[0].get("credits", 0)) if data else 0
    except Exception: return 0

def deduct_supabase_credits_atomic(user_id, amount):
    try:
        rpc_url = f"{SUPABASE_URL}/rest/v1/rpc/decrement_credits"
        headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
        payload = {"uid": user_id, "amt": amount}
        resp = requests.post(rpc_url, headers=headers, json=payload, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            return True, int(data[0].get("credits", 0)) if data else (True, 0, None)
        return False, None, "Insufficient credits"
    except Exception as e: return False, None, str(e)

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS': return f(None, *args, **kwargs)
        auth = request.headers.get('Authorization', '')
        token = auth.split()[1] if 'Bearer ' in auth else None
        if not token: return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=['HS256', 'RS256'], options={'verify_aud': False})
            user_id = data.get('sub')
            user = User.query.get(user_id)
            if not user:
                user = User(id=user_id, email=data.get('email', ''))
                db.session.add(user)
                db.session.commit()
            return f(user, *args, **kwargs)
        except Exception: return jsonify({'error': 'Invalid Session'}), 401
    return decorated

# --- باقي المسارات ---

@app.route('/api/user', methods=['GET', 'OPTIONS'])
@token_required
def get_user_info(current_user):
    if request.method == 'OPTIONS': return jsonify({'ok': True})
    credits = get_supabase_user_credits(current_user.id)
    return jsonify({'id': current_user.id, 'email': current_user.email, 'credits': credits})

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@token_required
def start_dub(current_user):
    if request.method == 'OPTIONS': return jsonify({'ok': True})
    data = request.json or {}
    cost = 150 if data.get('with_lipsync') else 100
    success, new_balance, err = deduct_supabase_credits_atomic(current_user.id, cost)
    if not success: return jsonify({'error': err}), 402
    
    job_id = str(uuid.uuid4())
    new_job = DubbingJob(id=job_id, user_id=current_user.id, status='pending', credits_used=cost)
    db.session.add(new_job)
    db.session.commit()

    process_dub.delay({'job_id': job_id, 'file_key': data.get('file_key'), 'lang': data.get('lang', 'ar')})
    return jsonify({'success': True, 'job_id': job_id})

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=5000)
