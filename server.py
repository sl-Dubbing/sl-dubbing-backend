# server.py — النسخة V7.0 (المستقبل العملاق)
import os, uuid, json, base64, logging, requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor
from models import db, User, DubbingJob
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
CORS(app, supports_credentials=True)

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL').replace('postgres://', 'postgresql://', 1)
db.init_app(app)

# استخدام ThreadPool للتوازي الداخلي
executor = ThreadPoolExecutor(max_workers=50)
MODAL_URL = os.environ.get("MODAL_URL")

def run_background_task(job_id, endpoint, payload):
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        try:
            res = requests.post(f"{MODAL_URL}/{endpoint}", json=payload, timeout=600)
            data = res.json()
            if data.get("success"):
                job.output_url = data.get("audio_url")
                job.extra_data = data.get("translated_text") or data.get("final_text")
                job.status = 'completed'
            else: job.status = 'failed'
        except: job.status = 'failed'
        db.session.commit()

@app.route('/api/dub', methods=['POST'])
def dub():
    # استلام الملف وتحويله لـ Base64 فوراً
    file = request.files['media_file']
    file_b64 = base64.b64encode(file.read()).decode('utf-8')
    
    job_id = str(uuid.uuid4())
    # إنشاء المهمة في DB (افترضنا وجود مستخدم بـ ID=1 للتجربة)
    job = DubbingJob(id=job_id, user_id=1, status='processing', language=request.form.get('lang'), voice_mode='xtts')
    db.session.add(job); db.session.commit()
    
    payload = {"file_b64": file_b64, "lang": request.form.get('lang')}
    executor.submit(run_background_task, job_id, "", payload)
    return jsonify({"success": True, "job_id": job_id})

@app.route('/api/progress/<job_id>')
def progress(job_id):
    def stream():
        import time
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job: break
                yield f"data: {json.dumps({'status': job.status, 'progress': 100 if job.status=='completed' else 50, 'audio_url': job.output_url, 'final_text': job.extra_data})}\n\n"
                if job.status in ['completed', 'failed']: break
            time.sleep(2)
    return Response(stream(), mimetype='text/event-stream')

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=5000)
