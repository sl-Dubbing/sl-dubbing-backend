# app.py — V2.4 (Supabase Real-time Sync & Credit Protection)
import os
import uuid
import jwt
import boto3
import requests
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from models import db, User, DubbingJob, CreditTransaction
from tasks import process_dub

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

# إعدادات Supabase للربط المباشر
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

# إعدادات قاعدة البيانات المحلية والـ S3
DATABASE_URL = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

s3_client = boto3.client('s3', endpoint_url=os.environ.get('R2_ENDPOINT_URL'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    config=boto3.session.Config(signature_version='s3v4'), region_name='auto')

# --- دالة جلب الرصيد الحقيقي من Supabase ---
def get_real_credits_from_supabase(user_id):
    """تستعلم هذه الدالة عن الرصيد مباشرة من جدول users في Supabase"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/users?id=eq.{user_id}&select=credits"
        headers = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"
        }
        resp = requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        if data and len(data) > 0:
            return data[0].get("credits", 0)
    except Exception as e:
        print(f"Error fetching credits from Supabase: {e}")
    return 0

# --- نظام المصادقة المطور ---
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS': return f(None, *args, **kwargs)
        auth = request.headers.get('Authorization', '')
        token = auth.split()[1] if 'Bearer ' in auth else None
        
        if not token: return jsonify({'error': 'Unauthorized'}), 401
            
        try:
            # فك التوكن باستخدام السر المشترك
            data = jwt.decode(token, os.environ.get('SUPABASE_JWT_SECRET'), 
                              algorithms=['HS256', 'HS384', 'HS512', 'RS256'], 
                              options={'verify_aud': False})
            
            user_id = data.get('sub') 
            email = data.get('email', '')
            
            # التأكد من وجود المستخدم في قاعدة البيانات المحلية (لربط المهام والعمليات)
            user = User.query.filter_by(id=user_id).first()
            if not user:
                user = User(id=user_id, email=email, credits=0) # الرصيد سيُحدث من Supabase
                db.session.add(user)
                db.session.commit()
            
            # 💡 جلب الرصيد الحي من Supabase ووضعه في كائن المستخدم الحالي
            user.real_credits = get_real_credits_from_supabase(user_id)
                
            return f(user, *args, **kwargs)
        except Exception as e:
            print(f"Auth Error: {e}")
            return jsonify({'error': 'Invalid Session'}), 401
    return decorated

# --- Endpoints ---

@app.route('/api/user', methods=['GET', 'OPTIONS'])
@token_required
def get_user_info(current_user):
    if request.method == 'OPTIONS': return jsonify({'ok': True})
    # نرجع الرصيد الحي القادم من Supabase مباشرة
    return jsonify({
        'id': current_user.id,
        'email': current_user.email,
        'credits': current_user.real_credits 
    })

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@token_required
def start_dub(current_user):
    if request.method == 'OPTIONS': return jsonify({'ok': True})
    data = request.json or {}
    
    cost = 150 if data.get('with_lipsync') else 100
    
    # التحقق من الرصيد الحقيقي (Supabase) قبل البدء
    if current_user.real_credits < cost:
        return jsonify({'error': 'Insufficient credits', 'current': current_user.real_credits}), 402

    # [ملاحظة]: هنا يفضل عمل طلب PATCH لـ Supabase لخصم الرصيد هناك أيضاً لضمان المزامنة
    job_id = str(uuid.uuid4())
    new_job = DubbingJob(id=job_id, user_id=current_user.id, status='pending', 
                         language=data.get('lang', 'ar'), file_key=data.get('file_key'), credits_used=cost)
    db.session.add(new_job)
    db.session.commit()

    process_dub.delay({
        'job_id': job_id, 'file_key': data.get('file_key'), 'lang': data.get('lang', 'ar'),
        'voice_id': data.get('voice_id', 'source'), 'sample_b64': data.get('sample_b64', ''),
        'with_lipsync': data.get('with_lipsync', False), 'video_output': data.get('return_video', True)
    })
    return jsonify({'success': True, 'job_id': job_id}), 202

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=5000)
