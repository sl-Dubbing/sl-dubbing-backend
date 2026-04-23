import os, uuid, json, logging, time, requests, tempfile
from functools import wraps
import jwt
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor
from models import db, User, DubbingJob
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
db.init_app(app)

# ⚡ 1. السماح بالاتصالات
CORS(app, resources={
    r"/*": {
        "origins": ["https://sl-dubbing.github.io", "http://localhost:5500", "http://127.0.0.1:5500"],
        "supports_credentials": True,
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization", "X-Requested-With"]
    }
})

executor = ThreadPoolExecutor(max_workers=10)
DUB_URL = os.environ.get("MODAL_DUB_URL", "").rstrip('/')
TTS_URL = os.environ.get("MODAL_TTS_URL", "").rstrip('/')

# ⚡ 2. حماية المسارات
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return jsonify({}), 200
            
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
# 🔐 مسارات المصادقة (Auth)
# ==========================================
@app.route('/api/auth/google', methods=['POST', 'OPTIONS'])
def google_auth():
    if request.method == 'OPTIONS': return jsonify({}), 200
    data = request.json
    token = data.get('credential')
    
    google_res = requests.get(f"https://oauth2.googleapis.com/tokeninfo?id_token={token}")
    if google_res.status_code != 200: return jsonify({'error': 'Invalid Google token'}), 401
        
    g_data = google_res.json()
    email = g_data.get('email')
    name = g_data.get('name')
    avatar = g_data.get('picture', '👤')

    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, name=name, avatar=avatar, auth_method='google', credits=1000)
        db.session.add(user)
        db.session.commit()

    my_token = jwt.encode({'user_id': user.id, 'exp': time.time() + (86400 * 7)}, app.config['SECRET_KEY'], algorithm="HS256")
    return jsonify({'success': True, 'token': my_token, 'user': user.to_dict()})

@app.route('/api/user', methods=['GET', 'OPTIONS'])
@token_required
def get_user_data(current_user):
    return jsonify({'success': True, 'user': current_user.to_dict()})

# ==========================================
# ⚙️ مسارات الذكاء الاصطناعي المحدثة والمحمية من الانهيار
# ==========================================
def run_background_task(job_id, service_type, payload, cost, user_id):
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        user = User.query.get(user_id)
        if not job or not user: return
        
        try:
            target_base = DUB_URL if service_type == "dub" else TTS_URL
            endpoint = "/upload" if service_type == "dub" else "/tts"
            full_url = f"{target_base}{endpoint}"

            if service_type == "dub":
                file_path = payload.pop("_file_path")
                voice_path = payload.pop("_voice_path", None)

                files = {'media_file': open(file_path, 'rb')}
                if voice_path:
                    files['voice_sample'] = open(voice_path, 'rb')

                res = requests.post(full_url, data=payload, files=files, timeout=1800)

                # إغلاق وحذف الملفات المؤقتة
                files['media_file'].close()
                if os.path.exists(file_path): os.remove(file_path)
                
                if voice_path:
                    files['voice_sample'].close()
                    if os.path.exists(voice_path): os.remove(voice_path)
            else:
                res = requests.post(full_url, json=payload, timeout=600)

            data = res.json()
            if data.get("success"):
                job.output_url = data.get("audio_url")
                job.extra_data = data.get("translated_text") or data.get("final_text")
                job.status = 'completed'
            else: 
                raise Exception(data.get("error", "Error from AI Server"))

        except Exception as e:
            logging.error(f"Task Failed: {e}")
            job.status = 'failed'
            user.credits = (user.credits or 0) + cost # استرجاع الرصيد
        finally:
            db.session.commit()

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@token_required
def upload_dub(current_user):
    try:
        cost = 100
        user_credits = current_user.credits if current_user.credits is not None else 0
        if user_credits < cost: 
            return jsonify({"error": "رصيد غير كافٍ"}), 402
        
        if 'media_file' not in request.files:
            return jsonify({"error": "الرجاء رفع ملف"}), 400

        file = request.files['media_file']
        # استخدام tempfile لتجنب أخطاء النظام
        temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{file.filename}")
        file.save(temp_path)
        
        job_id = str(uuid.uuid4())
        lang = request.form.get('lang', 'ar')
        job = DubbingJob(id=job_id, user_id=current_user.id, status='processing', language=lang)
        
        current_user.credits = user_credits - cost
        db.session.add(job)
        db.session.commit()

        payload = {
            "lang": lang, 
            "voice_id": request.form.get('voice_id', 'original'), 
            "_file_path": temp_path
        }
        
        # معالجة العينة الصوتية المرفوعة
        if 'voice_sample' in request.files:
            v_file = request.files['voice_sample']
            v_path = os.path.join(tempfile.gettempdir(), f"voice_{uuid.uuid4()}_{v_file.filename}")
            v_file.save(v_path)
            payload["_voice_path"] = v_path
            payload["voice_id"] = "custom"
        
        executor.submit(run_background_task, job_id, "dub", payload, cost, current_user.id)
        return jsonify({"success": True, "job_id": job_id})
        
    except Exception as e:
        logging.error(f"Upload Error: {str(e)}")
        # نرجع الخطأ بصيغة JSON لمنع عطل المتصفح
        return jsonify({"success": False, "error": f"Internal Server Error: {str(e)}"}), 500

@app.route('/api/progress/<job_id>', methods=['GET', 'OPTIONS'])
def get_progress(job_id):
    if request.method == 'OPTIONS': return jsonify({}), 200
    def generate():
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job:
                    yield f"data: {json.dumps({'status': 'error'})}\n\n"
                    break
                
                yield f"data: {json.dumps({'status': job.status, 'audio_url': job.output_url, 'extra_data': job.extra_data})}\n\n"
                
                if job.status in ['completed', 'failed']: break
            time.sleep(2)
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
