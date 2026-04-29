import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from functools import wraps
from supabase import create_client, Client

# استيراد قاعدة البيانات والجداول وملف المهام الخاص بك
from models import db, User, DubbingJob, CreditTransaction
from tasks import process_smart_tts

app = Flask(__name__)
# السماح للواجهة الأمامية بالاتصال
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ==========================================
# 1. إعدادات قاعدة البيانات و Supabase
# ==========================================
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# ==========================================
# 2. نظام المصادقة الذكي (Supabase Middleware)
# ==========================================
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            token = auth_header.split(" ")[1] if "Bearer" in auth_header else auth_header

        if not token:
            return jsonify({'success': False, 'message': 'التوكن مفقود!'}), 401

        try:
            user_response = supabase.auth.get_user(token)
            supabase_user = user_response.user

            if not supabase_user:
                return jsonify({'success': False, 'message': 'توكن غير صالح!'}), 401

            current_user = User.query.filter_by(supabase_id=supabase_user.id).first()

            if not current_user:
                # إنشاء حساب تلقائي للمستخدم الجديد
                meta = supabase_user.user_metadata or {}
                current_user = User(
                    supabase_id=supabase_user.id,
                    email=supabase_user.email,
                    name=meta.get('full_name', supabase_user.email.split('@')[0]),
                    avatar=meta.get('avatar_url', '👤'),
                    credits=50000,
                    auth_method='supabase'
                )
                db.session.add(current_user)
                db.session.flush()
                
                welcome_transaction = CreditTransaction(
                    user_id=current_user.id,
                    transaction_type='bonus',
                    amount=50000,
                    reason='نقاط ترحيبية'
                )
                db.session.add(welcome_transaction)
                db.session.commit()

        except Exception as e:
            return jsonify({'success': False, 'message': 'جلسة غير صالحة'}), 401

        return f(current_user, *args, **kwargs)
    return decorated

# ==========================================
# 3. محرك التوجيه (The Gateway)
# ==========================================

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "message": "Gateway Online"}), 200

@app.route('/api/user', methods=['GET'])
@token_required
def get_user(current_user):
    return jsonify({'success': True, 'user': current_user.to_dict()})

# المسار المحدث: Async Dispatch TTS
@app.route('/api/tts', methods=['POST'])
@token_required
def start_tts(current_user):
    data = request.json
    if not data or 'text' not in data:
        return jsonify({"success": False, "error": "النص غير موجود"}), 400

    # 1. فحص الرصيد (التحقق السريع)
    if current_user.credits <= 0:
        return jsonify({"success": False, "error": "رصيدك غير كافٍ"}), 402

    try:
        # 2. إنشاء "سجل المهمة" في قاعدة البيانات فوراً بحالة 'pending'
        new_job = DubbingJob(
            user_id=current_user.id, 
            status='pending',
            language=data.get('lang', 'en'),
            voice_mode=data.get('voice_id', 'default'),
            text_length=len(data['text'])
        )
        db.session.add(new_job)
        db.session.commit()

        # 3. تحضير Payload للعمال (Workers)
        payload = {
            'job_id': str(new_job.id),
            'text': data['text'],
            'lang': data.get('lang', 'en'),
            'voice_id': data.get('voice_id', ''),
            'sample_b64': data.get('sample_b64', ''),
            'edge_voice': data.get('edge_voice', ''),
            'translate': data.get('translate', True),
            'rate': data.get('rate', '+0%'),
            'pitch': data.get('pitch', '+0Hz')
        }

        # 4. التوجيه الذكي (Async Dispatch)
        # نستخدم .spawn() لإرسال المهمة لـ Modal في الخلفية دون انتظار
        process_smart_tts.spawn(payload) 

        # 5. الرد الفوري على المستخدم (بنية ElevenLabs)
        return jsonify({
            "success": True, 
            "message": "بدأت المعالجة في الخلفية",
            "job_id": str(new_job.id),
            "status": "processing"
        }), 202 # كود 202 يعني Accepted (تم قبول الطلب للمعالجة)

    except Exception as e:
        db.session.rollback()
        print(f"❌ Gateway Dispatch Error: {e}")
        return jsonify({"success": False, "error": "فشل توجيه المهمة"}), 500

@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def check_job(current_user, job_id):
    try:
        job = DubbingJob.query.get(job_id)
        if not job or job.user_id != current_user.id:
            return jsonify({"status": "failed", "error": "غير مصرح لك"}), 403
        return jsonify(job.to_dict())
    except Exception as e:
        return jsonify({"status": "failed", "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
