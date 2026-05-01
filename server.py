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

# استيراد المكونات المحلية
from models import db, User, DubbingJob, CreditTransaction
from tasks import process_dub

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# ⚙️ الإعدادات الأساسية
# ==========================================
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# مفاتيح Supabase للتحقق من الهوية
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

# ==========================================
# ☁️ إعداد Cloudflare R2 (تخزين الملفات)
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
# 🔐 إعدادات CORS (للسماح بالاتصال من GitHub Pages)
# ==========================================
ALLOWED_ORIGINS = os.environ.get('ALLOWED_ORIGINS', 'https://sl-dubbing.github.io').split(',')
CORS(app, 
     supports_credentials=True, 
     origins=ALLOWED_ORIGINS,
     allow_headers=["Content-Type", "Authorization", "X-Requested-With", "Accept"],
     methods=["GET", "POST", "OPTIONS"])

# ==========================================
# 🛡️ مزود الحماية (token_required)
# ==========================================
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == "OPTIONS":
            return make_response(jsonify({"success": True}), 200)

        token = None
        auth_header = request.headers.get('Authorization', '')
        if auth_header.lower().startswith('bearer '):
            token = auth_header.split()[1]
        
        if not token:
            return jsonify({'error': 'Unauthorized: No token provided'}), 401
            
        try:
            headers = {'Authorization': f'Bearer {token}', 'apikey': SUPABASE_KEY}
            auth_response = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=headers)
            
            if auth_response.status_code != 200:
                return jsonify({'error': 'Invalid Session'}), 401
                
            user_data = auth_response.json()
            email = user_data.get('email')
            
            # البحث عن المستخدم أو إنشاؤه برصيد افتراضي 500
            current_user = User.query.filter_by(email=email).first()
            if not current_user:
                meta = user_data.get('user_metadata', {})
                current_user = User(
                    email=email,
                    name=meta.get('full_name', email.split('@')[0]),
                    avatar=meta.get('avatar_url'),
                    credits=500,
                    supabase_id=user_data.get('id')
                )
                db.session.add(current_user)
                db.session.commit()
                logger.info(f"New user created: {email}")

        except Exception as e:
            logger.error(f"Auth error: {e}")
            return jsonify({'error': 'Authentication failed'}), 500
            
        return f(current_user, *args, **kwargs)
    return decorated

# ==========================================
# 🚀 مسارات الـ API
# ==========================================

@app.route('/api/user/credits', methods=['GET'])
@token_required
def get_user_credits(current_user):
    return jsonify({
        'success': True, 
        'user': {
            'id': current_user.id,
            'email': current_user.email,
            'credits': current_user.credits,
            'name': current_user.name
        }
    })

@app.route('/api/dubbing', methods=['POST'])
@token_required
def start_dubbing_route(current_user):
    cost = int(os.environ.get('DUB_COST', 100))
    
    # 1. التحقق من الرصيد
    if (current_user.credits or 0) < cost:
        return jsonify({"error": "Insufficient credits"}), 402

    # 2. التحقق من الملف
    file = request.files.get('media_file') or (list(request.files.values())[0] if request.files else None)
    if not file or file.filename == '':
        return jsonify({"error": "No file uploaded"}), 400

    # 3. رفع الملف إلى Cloudflare R2
    try:
        file_key = f"uploads/{uuid.uuid4()}_{secure_filename(file.filename)}"
        s3_client.upload_fileobj(file, R2_BUCKET_NAME, file_key)
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        return jsonify({"error": "Storage upload failed"}), 500

    # 4. المعاملة المالية وحفظ الوظيفة (Atomicity)
    try:
        job_id = str(uuid.uuid4())
        
        # إنشاء سجل الوظيفة
        job = DubbingJob(
            id=job_id,
            user_id=current_user.id,
            status='processing',
            language=request.form.get('lang', 'ar'),
            voice_mode=request.form.get('voice_mode', 'source')
        )
        
        # سجل الخصم
        transaction = CreditTransaction(
            user_id=current_user.id,
            job_id=job_id,
            amount=cost,
            transaction_type='debit',
            reason=f"Dubbing job {job_id}"
        )

        # خصم النقاط
        current_user.credits -= cost

        db.session.add(job)
        db.session.add(transaction)
        db.session.commit() # هنا يتم الحفظ النهائي

        # 5. تشغيل العامل (Worker) في الخلفية
        process_dub.delay({
            'job_id': job.id,
            'file_key': file_key,
            'lang': job.language
        })

        return jsonify({"success": True, "job_id": job.id})

    except Exception as e:
        db.session.rollback() # تراجع عن الخصم إذا فشل الحفظ في DB
        logger.error(f"Database error: {e}")
        return jsonify({"error": "Database error: Job not started, credits not deducted"}), 500

@app.route('/api/health')
def health():
    return jsonify({'status': 'online', 'server': 'Railway'})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
