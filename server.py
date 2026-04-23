import os, uuid, json, logging, time, requests, tempfile
from functools import wraps
import jwt
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor
from models import db, User, DubbingJob
from dotenv import load_dotenv

# تحميل المتغيرات البيئية
load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# الإعدادات الأساسية
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# 🟢 إعدادات CORS - تسمح لموقعك بالاتصال وإرسال التوكنات بأمان
CORS(app, supports_credentials=True, origins=[
    "https://sl-dubbing.github.io",
    "https://sl-dubbing.github.io/"
])

executor = ThreadPoolExecutor(max_workers=10)
DUB_URL = os.environ.get("MODAL_DUB_URL", "").rstrip('/')

# 🔐 ديكوريتور حماية المسارات (يتحقق من وجود التوكن)
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            parts = request.headers['Authorization'].split()
            if len(parts) == 2: token = parts[1]
            
        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
            
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_user = User.query.get(data['user_id'])
            if not current_user: raise Exception("User not found")
        except Exception:
            return jsonify({'error': 'Invalid token'}), 401
            
        return f(current_user, *args, **kwargs)
    return decorated

# ==========================================
# 🔐 مسارات المصادقة (Auth Routes)
# ==========================================

@app.route('/api/auth/google', methods=['POST'])
def google_auth():
    try:
        data = request.json
        google_token = data.get('credential')
        
        # التحقق من توكن جوجل عبر سيرفراتهم الرسمية
        google_res = requests.get(f"https://oauth2.googleapis.com/tokeninfo?id_token={google_token}")
        if google_res.status_code != 200:
            return jsonify({'error': 'فشل التحقق من حساب جوجل'}), 401
            
        g_data = google_res.json()
        email = g_data.get('email')
        
        # البحث عن المستخدم أو إنشاء واحد جديد
        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(
                email=email,
                name=g_data.get('name'),
                avatar=g_data.get('picture', '👤'),
                auth_method='google',
                credits=1000  # رصيد ترحيبي
            )
            db.session.add(user)
            db.session.commit()

        # إنشاء توكن JWT خاص بموقعنا يدوم أسبوعاً
        my_token = jwt.encode(
            {'user_id': user.id, 'exp': time.time() + (86400 * 7)},
            app.config['SECRET_KEY'],
            algorithm="HS256"
        )
        
        return jsonify({
            'success': True,
            'token': my_token,
            'user': user.to_dict()
        })
    except Exception as e:
        logger.error(f"Google Auth Error: {str(e)}")
        return jsonify({'error': 'حدث خطأ في السيرفر أثناء تسجيل الدخول'}), 500

@app.route('/api/user', methods=['GET'])
@token_required
def get_user_data(current_user):
    return jsonify({'success': True, 'user': current_user.to_dict()})

# ==========================================
# ⚙️ محرك المهام الخلفية (Background Engine)
# ==========================================

def run_background_task(job_id, service_type, payload, cost, user_id):
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        user = User.query.get(user_id)
        if not job: return
        
        try:
            full_url = f"{DUB_URL}/upload"
            file_path = payload.pop("_file_path")
            voice_path = payload.pop("_voice_path", None)

            with open(file_path, 'rb') as f_media:
                files = {'media_file': f_media}
                if voice_path:
                    files['voice_sample'] = open(voice_path, 'rb')

                # إرسال الطلب لسيرفر Modal (الذكاء الاصطناعي)
                res = requests.post(full_url, data=payload, files=files, timeout=1800)
                if voice_path: files['voice_sample'].close()

            logger.info(f"📡 Modal Status: {res.status_code}")
            
            if res.status_code != 200:
                raise Exception(f"AI Server Error: {res.status_code}")

            data = res.json()
            if data.get("success"):
                job.output_url = data.get("audio_url")
                job.status = 'completed'
            else: 
                raise Exception(data.get("error", "AI Process Failed"))

        except Exception as e:
            logger.error(f"❌ Task {job_id} Failed: {str(e)}")
            job.status = 'failed'
            # استرجاع الرصيد للمستخدم في حال الفشل
            if user: user.credits = (user.credits or 0) + cost 
        finally:
            if os.path.exists(file_path): os.remove(file_path)
            db.session.commit()

# ==========================================
# 🎙️ مسارات الدبلجة (Dubbing Routes)
# ==========================================

@app.route('/api/dub', methods=['POST'])
@token_required
def upload_dub(current_user):
    try:
        cost = 100
        if (current_user.credits or 0) < cost:
            return jsonify({"error": "رصيد غير كافٍ"}), 402
        
        if 'media_file' not in request.files:
            return jsonify({"error": "يرجى اختيار ملف"}), 400

        file = request.files['media_file']
        temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{file.filename}")
        file.save(temp_path)
        
        voice_val = request.form.get('voice_id', 'original')
        # قص الرابط الطويل إذا كان من كلاوديناري ليناسب قاعدة البيانات
        safe_mode = "cloudinary_voice" if voice_val.startswith('http') else voice_val
        
        job = DubbingJob(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            status='processing',
            language=request.form.get('lang', 'ar'),
            voice_mode=safe_mode[:50],
            credits_used=cost
        )
        
        current_user.credits -= cost
        db.session.add(job)
        db.session.commit()

        payload = {"lang": job.language, "voice_id": voice_val, "_file_path": temp_path}
        
        # إذا رفع المستخدم عينة صوتية مخصصة
        if 'voice_sample' in request.files:
            v_file = request.files['voice_sample']
            v_path = os.path.join(tempfile.gettempdir(), f"v_{uuid.uuid4()}.wav")
            v_file.save(v_path)
            payload["_voice_path"] = v_path

        # تشغيل المهمة في الخلفية لكي لا تنتظر الصفحة طويلاً
        executor.submit(run_background_task, job.id, "dub", payload, cost, current_user.id)
        
        return jsonify({"success": True, "job_id": job.id})
    except Exception as e:
        logger.error(f"Upload Error: {str(e)}")
        return jsonify({"error": "حدث خطأ أثناء الرفع"}), 500

@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    def generate():
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job: break
                # إرسال الحالة الحالية للمتصفح
                yield f"data: {json.dumps({'status': job.status, 'audio_url': job.output_url})}\n\n"
                if job.status in ['completed', 'failed']: break
            time.sleep(2)
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
