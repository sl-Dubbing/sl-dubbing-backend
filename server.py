# server.py — V8.0 (Microservices Router Edition)
import os, uuid, json, logging, time, requests, jwt
from flask import Flask, request, jsonify, Response, make_response
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor
from models import db, User, DubbingJob
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL').replace('postgres://', 'postgresql://', 1)
db.init_app(app)

CORS(app, supports_credentials=True, origins=['https://sl-dubbing.github.io', 'http://localhost:5500'])
executor = ThreadPoolExecutor(max_workers=10)

# ⚡ جلب الروابط المنفصلة من المتغيرات
DUB_URL = os.environ.get("MODAL_DUB_URL", "").rstrip('/')
TTS_URL = os.environ.get("MODAL_TTS_URL", "").rstrip('/')

def run_background_task(job_id, service_type, payload, cost, user_id):
    with app.app_context():
        job, user = DubbingJob.query.get(job_id), User.query.get(user_id)
        if not job or not user: return
        
        try:
            # 🎯 اختيار الرابط الصحيح بناءً على نوع الخدمة
            target_base = DUB_URL if service_type == "dub" else TTS_URL
            endpoint = "/upload" if service_type == "dub" else "/tts"
            full_url = f"{target_base}{endpoint}"

            if service_type == "dub":
                file_path = payload.pop("_file_path")
                with open(file_path, "rb") as f:
                    res = requests.post(full_url, data=payload, files={'media_file': f}, timeout=600)
                if os.path.exists(file_path): os.remove(file_path)
            else:
                res = requests.post(full_url, json=payload, timeout=120)

            data = res.json()
            if data.get("success"):
                job.output_url = data.get("audio_url")
                job.extra_data = data.get("translated_text") or data.get("final_text")
                job.status = 'completed'
            else: raise Exception("Factory error")

        except Exception as e:
            logging.error(f"Task Failed: {e}")
            job.status = 'failed'
            user.credits += cost # إعادة الرصيد في حال الفشل
        finally:
            db.session.commit()

@app.route('/api/dub', methods=['POST'])
def upload_dub():
    user = request.user # (بافتراض وجود auth middleware)
    cost = 100
    if user.credits < cost: return jsonify({"error": "No credits"}), 402
    
    file = request.files['media_file']
    temp_path = f"/tmp/{uuid.uuid4()}_{file.filename}"
    file.save(temp_path)
    
    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, status='processing', language=request.form.get('lang', 'en'))
    user.credits -= cost
    db.session.add(job); db.session.commit()

    payload = {"lang": request.form.get('lang', 'en'), "voice_id": request.form.get('voice_id', ''), "_file_path": temp_path}
    executor.submit(run_background_task, job_id, "dub", payload, cost, user.id)
    return jsonify({"success": True, "job_id": job_id})

@app.route('/api/tts', methods=['POST'])
def tts_route():
    user = request.user
    cost = 20
    if user.credits < cost: return jsonify({"error": "No credits"}), 402
    
    data = request.json
    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, status='processing', language=data.get('lang', 'en'))
    user.credits -= cost
    db.session.add(job); db.session.commit()

    executor.submit(run_background_task, job_id, "tts", data, cost, user.id)
    return jsonify({"success": True, "job_id": job_id})

# ... (باقي دوال الـ Auth و progress تبقى كما هي) ...

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=5000)
