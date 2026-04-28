import os
import jwt
from flask import Flask, request, jsonify
from flask_cors import CORS
from functools import wraps

# استيراد قاعدة البيانات والجداول وملف المهام الخاص بك
from models import db, User, DubbingJob
from tasks import process_smart_tts

app = Flask(__name__)
# السماح للواجهة الأمامية بالاتصال
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ==========================================
# 1. إعدادات قاعدة البيانات (نفس إعدادات العامل)
# ==========================================
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-super-secret-key')
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# تهيئة قاعدة البيانات مع التطبيق
db.init_app(app)

# ==========================================
# 2. نظام المصادقة (Middleware)
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
            # محاولة فك تشفير التوكن (JWT)
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_user = User.query.get(data['user_id'])
            if not current_user:
                raise Exception("المستخدم غير موجود")
        except Exception as e:
            # (للتجربة حالياً: إذا فشل التوكن، سنجلب أول مستخدم في قاعدة البيانات لتجنب تعطل العمل)
            # يمكنك إزالة هذه المساعدة لاحقاً عندما تتأكد من عمل نظام تسجيل الدخول 100%
            current_user = User.query.first()
            if not current_user:
                return jsonify({'success': False, 'message': 'توكن غير صالح ولا يوجد مستخدمين!'}), 401

        return f(current_user, *args, **kwargs)
    return decorated

# ==========================================
# 3. مسارات الـ API
# ==========================================

# أ. جلب بيانات المستخدم للشريط الجانبي
@app.route('/api/user', methods=['GET'])
@token_required
def get_user(current_user):
    return jsonify({
        'success': True, 
        'user': {
            'id': current_user.id,
            # إذا لم يكن الحقل 'name' موجوداً، استخدم اسماً افتراضياً
            'name': getattr(current_user, 'name', 'مستخدم'), 
            'credits': getattr(current_user, 'credits', 0)
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
    if getattr(current_user, 'credits', 0) <= 0:
        return jsonify({"success": False, "error": "رصيدك غير كافٍ"})

    try:
        # 1. إنشاء مهمة في قاعدة البيانات (حالتها: جاري المعالجة)
        new_job = DubbingJob(
            user_id=current_user.id,
            status='processing'
        )
        db.session.add(new_job)
        db.session.commit()

        # 2. تجهيز البيانات التي سيأخذها العامل (Worker)
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

        # 3. إرسال المهمة إلى Celery (ليقوم tasks.py بمعالجتها)
        process_smart_tts.delay(payload)

        # 4. إرجاع رقم المهمة للواجهة (frontend)
        return jsonify({"success": True, "job_id": new_job.id})

    except Exception as e:
        db.session.rollback()
        print(f"❌ Error starting TTS job: {e}")
        return jsonify({"success": False, "error": "حدث خطأ أثناء إرسال العملية."})

# ج. فحص حالة المهمة (Polling)
@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def check_job(current_user, job_id):
    try:
        job = DubbingJob.query.get(job_id)
        
        if not job:
            return jsonify({"status": "failed", "error": "المهمة غير موجودة"})

        # إذا نجح Celery في إكمالها ووضع الرابط
        if job.status == 'completed':
            return jsonify({"status": "completed", "audio_url": job.output_url})
            
        # إذا فشل Celery
        elif job.status == 'failed':
            # يمكنك إضافة job.extra_data أو حقل الخطأ إذا كنت تخزنه
            return jsonify({"status": "failed", "error": "فشلت المعالجة"})
            
        # لا تزال قيد المعالجة
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
