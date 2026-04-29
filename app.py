import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from functools import wraps
from supabase import create_client, Client

# استيراد قاعدة البيانات والجداول وملف المهام
from models import db, User, DubbingJob
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

# تهيئة قاعدة البيانات مع التطبيق
db.init_app(app)

# تهيئة عميل Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("⚠️ تحذير: مفاتيح Supabase غير موجودة في بيئة التشغيل!")

# إنشاء نقطة الاتصال مع خوادم Supabase
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
            return jsonify({'success': False, 'message': 'التوكن مفقود! يرجى تسجيل الدخول.'}), 401

        try:
            # 1. التحقق من صحة التوكن عبر خوادم Supabase
            user_response = supabase.auth.get_user(token)
            supabase_user = user_response.user

            if not supabase_user:
                return jsonify({'success': False, 'message': 'توكن غير صالح!'}), 401

            # 2. البحث عن المستخدم في قاعدة بياناتنا المحلية في Railway
            current_user = User.query.filter_by(supabase_id=supabase_user.id).first()

            # 3. إذا كان المستخدم جديداً (سجل للتو)، ننشئ له حساباً لدينا فوراً
            if not current_user:
                current_user = User(
                    supabase_id=supabase_user.id,
                    email=supabase_user.email,
                    credits=100  # اعطائه 100 نقطة مجانية كبداية
                )
                db.session.add(current_user)
                db.session.commit()

        except Exception as e:
            print(f"❌ Auth Error: {e}")
            return jsonify({'success': False, 'message': 'جلسة غير صالحة، يرجى تسجيل الدخول مجدداً.'}), 401

        return f(current_user, *args, **kwargs)
    return decorated

# ==========================================
# 3. مسارات الـ API (محمية بالكامل)
# ==========================================

# أ. جلب بيانات المستخدم للشريط الجانبي
@app.route('/api/user', methods=['GET'])
@token_required
def get_user(current_user):
    return jsonify({
        'success': True, 
        'user': {
            'id': current_user.id,
            'email': current_user.email, 
            'credits': current_user.credits
        }
    })

# ب. إرسال طلب التوليد (يرسل المهمة إلى Celery)
@app.route('/api/tts', methods=['POST'])
@token_required
def start_tts(current_user):
    data = request.json
    if not data or 'text' not in data:
        return jsonify({"success": False, "error": "النص غير موجود"})

    # التحقق من الرصيد
    if current_user.credits <= 0:
        return jsonify({"success": False, "error": "رصيدك غير كافٍ"})

    try:
        # إنشاء مهمة في قاعدة البيانات
        new_job = DubbingJob(user_id=current_user.id, status='processing')
        db.session.add(new_job)
        db.session.commit()

        # تجهيز البيانات للعامل (Worker)
        payload = {
            'job_id': new_job.id,
            'text': data['text'],
            'lang': data.get('lang', 'en'),
            'voice_id': data.get('voice_id', ''),
            'sample_b64': data.get('sample_b64', ''),
            'edge_voice': data.get('edge_voice', ''),
            'translate': data.get('translate', True),
            'rate': data.get('rate', '+0%'),
            'pitch': data.get('pitch', '+0Hz')
        }

        # إرسال المهمة إلى Celery
        process_smart_tts.delay(payload)

        return jsonify({"success": True, "job_id": new_job.id})

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error starting TTS job: {e}")
        return jsonify({"success": False, "error": "حدث خطأ أثناء إرسال العملية."})

# ج. فحص حالة المهمة
@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def check_job(current_user, job_id):
    try:
        job = DubbingJob.query.get(job_id)
        
        if not job:
            return jsonify({"status": "failed", "error": "المهمة غير موجودة"})

        # التأكد من أن المهمة تخص المستخدم نفسه (أمان إضافي)
        if job.user_id != current_user.id:
            return jsonify({"status": "failed", "error": "غير مصرح لك بمشاهدة هذه المهمة"})

        if job.status == 'completed':
            return jsonify({"status": "completed", "audio_url": job.output_url})
        elif job.status == 'failed':
            return jsonify({"status": "failed", "error": "فشلت المعالجة"})
        else:
            return jsonify({"status": "processing"})

    except Exception as e:
        print(f"❌ Job Check Error: {e}")
        return jsonify({"status": "failed", "error": "خطأ داخلي في السيرفر"})

# ==========================================
# 4. تشغيل السيرفر
# ==========================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
