import os
import uuid
import logging
from functools import wraps
import requests # 🚀 سنعتمد على هذه المكتبة المستقرة جداً للتحقق من التوكن

import boto3
from botocore.client import Config
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

from models import db, User, DubbingJob, CreditTransaction
from tasks import process_dub

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# الإعدادات الأساسية
# ==========================================
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# مفاتيح Supabase
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

# ==========================================
# إعداد Cloudflare R2
# ==========================================
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4'),
)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')
R2_PUBLIC_BASE = os.environ.get('R2_PUBLIC_BASE')

# ==========================================
# إعداد CORS
# ==========================================
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', 'https://sl-dubbing.github.io')
CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS.split(','))

# ==========================================
# دوال المساعدة (Helpers)
# ==========================================

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header.lower().startswith('bearer '):
            token = auth_header.split()[1]
        
        if not token:
            token = request.cookies.get(os.environ.get('COOKIE_NAME', 'session'))

        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
            
        try:
            # 🚀 استدعاء مباشر لـ Supabase للتحقق من التوكن (REST API)
            if not SUPABASE_URL or not SUPABASE_KEY:
                logger.error("SUPABASE_URL or SUPABASE_KEY is missing!")
                return jsonify({'error': 'Server config error'}), 500

            headers = {
                'Authorization': f'Bearer {token}',
                'apikey': SUPABASE_KEY
            }
            
            # نرسل التوكن لـ Supabase لتأكيده وإرجاع بيانات المستخدم
            auth_response = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=headers)
            
            if auth_response.status_code != 200:
                logger.warning(f"Supabase Auth rejected token. Status: {auth_response.status_code}")
                return jsonify({'error': 'Invalid or expired Session'}), 401
                
            user_data = auth_response.json()
            email = user_data.get('email')
            
            if not email:
                return jsonify({'error': 'Invalid user data'}), 401
                
            current_user = User.query.filter_by(email=email).first()
            
            if not current_user:
                meta = user_data.get('user_metadata', {})
                current_user = User(
                    email=email,
                    name=meta.get('full_name', email.split('@')[0]),
                    avatar=meta.get('avatar_url'),
                    credits=500
                )
                db.session.add(current_user)
                db.session.commit()

        except Exception as e:
            logger.error(f"Auth Logic Failure: {e}")
            return jsonify({'error': 'Server error during auth'}), 500
            
        return f(current_user, *args, **kwargs)
    return decorated

# ==========================================
# مسارات الـ API
# ==========================================

@app.route('/api/user/credits', methods=['GET'])
@app.route('/api/user', methods=['GET'])
@token_required
def get_user_data(current_user):
    user_dict = current_user.to_dict()
    if current_user.avatar_key and R2_PUBLIC_BASE:
        user_dict['avatar'] = f"{R2_PUBLIC_BASE.rstrip('/')}/{current_user.avatar_key}"
    return jsonify({'success': True, 'user': user_dict})

@app.route('/api/dubbing', methods=['POST'])
@token_required
def start_dubbing_route(current_user):
    cost = int(os.environ.get('DUB_COST', 100))
    if (current_user.credits or 0) < cost:
        return jsonify({"error": "Insufficient credits"}), 402

    file = request.files.get('media_file')
    if not file: return jsonify({"error": "No file uploaded"}), 400

    file_key = f"uploads/{uuid.uuid4()}_{secure_filename(file.filename)}"
    s3_client.upload_fileobj(file, R2_BUCKET_NAME, file_key)

    current_user.credits -= cost
    job = DubbingJob(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        status='processing',
        language=request.form.get('lang', 'ar')
    )
    db.session.add(job)
    db.session.add(CreditTransaction(user_id=current_user.id, amount=cost, transaction_type='debit'))
    db.session.commit()

    process_dub.delay({
        'job_id': job.id,
        'file_key': file_key,
        'lang': job.language
    })

    return jsonify({"success": True, "job_id": job.id})

@app.route('/api/health')
def health():
    return jsonify({'status': 'online', 'server': 'Railway'})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
