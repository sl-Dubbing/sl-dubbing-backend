# server.py — V8.1 (Microservices Router Edition - Optimized)
import os, uuid, json, logging, time, requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor
from models import db, User, DubbingJob
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
db.init_app(app)

# ⚡ السماح للواجهة الأمامية بالاتصال
CORS(app, supports_credentials=True, origins=['https://sl-dubbing.github.io', 'http://localhost:5500'])

# ⚡ محرك المهام الخلفية المدمج (يوفر تكلفة Celery و Redis)
executor = ThreadPoolExecutor(max_workers=10)

DUB_URL = os.environ.get("MODAL_DUB_URL", "").rstrip('/')
TTS_URL = os.environ.get("MODAL_TTS_URL", "").rstrip('/')

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
                with open(file_path, "rb") as f:
                    res = requests.post(full_url, data=payload, files={'media_file': f}, timeout=1800)
                if os.path.exists(file_path): os.remove(file_path) # تنظيف السيرفر
            else:
                res = requests.post(full_url, json=payload, timeout=600)

            data = res.json()
            if data.get("success"):
                job.output_url = data.get("audio_url")
                job.extra_data = data.get("translated_text") or data.get("final_text")
                job.status = 'completed'
            else: 
                raise Exception(data.get("error", "Factory error"))

        except Exception as e:
            logging.error(f"Task Failed: {e}")
            job.status = 'failed'
            user.credits += cost # إعادة الرصيد للمستخدم بشفافية
        finally:
            db.session.commit()

@app.route('/api/dub', methods=['POST'])
def upload_dub():
    # افترضت هنا أنكِ تجلبين المستخدم، يرجى دمج كود الـ Auth الخاص بك هنا
    # user = request.user 
    
    # للتجربة فقط (احذفي هذا السطر بعد دمج الـ Auth)
    user = User.query.first() 
    
    cost = 100
    if not user or user.credits < cost: 
        return jsonify({"error": "No credits"}), 402
    
    file = request.files['media_file']
    temp_path = f"/tmp/{uuid.uuid4()}_{file.filename}"
    file.save(temp_path)
    
    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, status='processing', language=request.form.get('lang', 'en'))
    user.credits -= cost
    db.session.add(job)
    db.session.commit()

    # ⚡ تمت إضافة sample_b64 لدعم استنساخ الصوت المخصص
    payload = {
        "lang": request.form.get('lang', 'en'), 
        "voice_id": request.form.get('voice_id', 'source'), 
        "sample_b64": request.form.get('sample_b64', ''),
        "_file_path": temp_path
    }
    
    executor.submit(run_background_task, job_id, "dub", payload, cost, user.id)
    return jsonify({"success": True, "job_id": job_id})

@app.route('/api/tts', methods=['POST'])
def tts_route():
    # user = request.user 
    user = User.query.first() # للتجربة
    
    cost = 20
    if not user or user.credits < cost: 
        return jsonify({"error": "No credits"}), 402
    
    data = request.json
    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, status='processing', language=data.get('lang', 'en'))
    user.credits -= cost
    db.session.add(job)
    db.session.commit()

    executor.submit(run_background_task, job_id, "tts", data, cost, user.id)
    return jsonify({"success": True, "job_id": job_id})

# ⚡ تذكير: تأكدي من وجود مسار الـ SSE هنا لمتابعة التقدم
@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    def generate():
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job:
                    yield f"data: {json.dumps({'status': 'error'})}\n\n"
                    break
                
                yield f"data: {json.dumps({'status': job.status, 'audio_url': job.output_url, 'extra_data': job.extra_data})}\n\n"
                
                if job.status in ['completed', 'failed']:
                    break
            time.sleep(2)
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=os.environ.get("PORT", 5000))
