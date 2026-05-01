import os
import uuid
import logging
from functools import wraps
import requests
import boto3
from botocore.client import Config
from flask import Flask, request, jsonify, make_response
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
# 🛠️ إصلاح إعدادات CORS للسماح بالاتصال من GitHub Pages
# ==========================================
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', 'https://sl-dubbing.github.io').split(',')
CORS(app, 
     supports_credentials=True, 
     origins=ALLOWED_ORIGINS,
     allow_headers=["Content-Type", "Authorization", "X-Requested-With", "Accept"],
     methods=["GET", "POST", "OPTIONS"])

# ==========================================
# دوال المساعدة (Helpers)
# ==========================================

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. التعامل مع طلبات Preflight (OPTIONS) الخاصة بالمتصفح
        if request.method == "OPTIONS":
            return make_response(jsonify({"success": True}), 200)

        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header.lower().startswith('bearer '):
            token = auth_header.split()[1]
        
        if not token:
            token = request.cookies.get(os.environ.get('COOKIE_NAME', 'session'))

        if not token:
            logger.warning("No token provided in request.")
            return jsonify({'error': 'Unauthorized: No token provided'}), 401
            
        try:
            if not SUPABASE_URL or not SUPABASE_KEY:
                logger.error("SUPABASE_URL or SUPABASE_KEY is missing!")
                return jsonify({'error': 'Server config error'}), 500

            headers = {
                'Authorization': f'Bearer {token}',
                'apikey': SUPABASE_KEY
            }
            
            # التحقق من صحة التوكن مع Supabase
            auth_response = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=headers)
            
            if auth_response.status_code != 200:
                logger.warning(f"Supabase Auth rejected token. Status: {auth_response.status_code}")
                return jsonify({'error': 'Invalid or expired Session'}), 401
                
            user_data = auth_response.json()
            email = user_data.get('email')
            
            if not email:
                return jsonify({'error': 'Invalid user data from Supabase'}), 401
                
            # 🛠️ البحث عن المستخدم في قاعدة البيانات
            current_user = User.query.filter_by(email=email).first()
            
            # 🛠️ إنشاء مستخدم جديد إذا لم يكن موجوداً
            if not current_user:
                meta = user_data.get('user_metadata', {})
                current_user = User(
                    email=email,
                    name=meta.get('full_name', email.split('@')[0]),
                    avatar=meta.get('avatar_url'),
                    credits=500, # منح 500 نقطة للمستخدمين الجدد
                    supabase_id=user_data.get('id') # حفظ معرف Supabase
                )
                db.session.add(current_user)
                db.session.commit()
                logger.info(f"Created new user in DB: {email}")

        except Exception as e:
            logger.error(f"Auth Logic Failure in token_required: {e}")
            return jsonify({'error': 'Server error during authentication'}), 500
            
        return f(current_user, *args, **kwargs)
    return decorated

# ==========================================
# مسارات الـ API
# ==========================================

@app.route('/api/user/credits', methods=['GET', 'OPTIONS'])
@app.route('/api/user', methods=['GET', 'OPTIONS'])
@token_required
def get_user_data(current_user):
    try:
        # 🛠️ جلب بيانات المستخدم وتجهيزها بشكل آمن للـ JSON
        user_dict = {
            'id': current_user.id,
            'email': current_user.email,
            'name': current_user.name,
            'credits': current_user.credits,
            'avatar': current_user.avatar
        }

        # التحقق من وجود صورة رمزية (Avatar) في R2
        avatar_key = getattr(current_user, 'avatar_key', None)
        if avatar_key and R2_PUBLIC_BASE:
            user_dict['avatar'] = f"{R2_PUBLIC_BASE.rstrip('/')}/{avatar_key}"

        return jsonify({'success': True, 'user': user_dict}), 200
    except Exception as e:
        logger.error(f"Error fetching user data: {e}")
        return jsonify({'error': 'Failed to fetch user data'}), 500

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
        # التأكد من إنشاء الجداول إذا لم تكن موجودة
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
