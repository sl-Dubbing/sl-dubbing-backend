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
# إعداد السجلات (Logs) لتظهر بوضوح في Railway
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
db.init_app(app)

CORS(app, resources={r"/*": {"origins": "*"}}) # للتسهيل حالياً

executor = ThreadPoolExecutor(max_workers=10)
DUB_URL = os.environ.get("MODAL_DUB_URL", "").rstrip('/')

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').split()[-1] if 'Authorization' in request.headers else None
        if not token: return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_user = User.query.get(data['user_id'])
        except: return jsonify({'error': 'Invalid token'}), 401
        return f(current_user, *args, **kwargs)
    return decorated

# ⚙️ محرك المهام الخلفية المطور
def run_background_task(job_id, service_type, payload, cost, user_id):
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        user = User.query.get(user_id)
        if not job: return
        
        try:
            full_url = f"{DUB_URL}/upload"
            logger.info(f"🚀 Sending Job {job_id} to Modal...")

            file_path = payload.pop("_file_path")
            voice_path = payload.pop("_voice_path", None)

            with open(file_path, 'rb') as f_media:
                files = {'media_file': f_media}
                if voice_path:
                    files['voice_sample'] = open(voice_path, 'rb')

                # إرسال الطلب مع مهلة زمنية طويلة للدبلجة
                res = requests.post(full_url, data=payload, files=files, timeout=1800)
                
                if voice_path: files['voice_sample'].close()

            # 🔍 هنا الإصلاح: التأكد من الرد قبل التحويل لـ JSON
            logger.info(f"📡 Modal Response Status: {res.status_code}")
            
            if res.status_code != 200:
                logger.error(f"❌ Modal Error Content: {res.text[:500]}")
                raise Exception(f"Modal Server returned {res.status_code}")

            data = res.json()
            if data.get("success"):
                job.output_url = data.get("audio_url")
                job.status = 'completed'
                logger.info(f"✅ Job {job_id} Done!")
            else: 
                raise Exception(data.get("error", "Unknown AI Error"))

        except Exception as e:
            logger.error(f"❌ Background Task Failed: {str(e)}")
            job.status = 'failed'
            if user: user.credits += cost 
        finally:
            if os.path.exists(file_path): os.remove(file_path)
            db.session.commit()

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
@token_required
def upload_dub(current_user):
    if request.method == 'OPTIONS': return jsonify({}), 200
    try:
        cost = 100
        if current_user.credits < cost: return jsonify({"error": "No Credits"}), 402
        
        file = request.files['media_file']
        temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_{file.filename}")
        file.save(temp_path)
        
        voice_val = request.form.get('voice_id', 'original')
        safe_mode = "cloudinary_voice" if voice_val.startswith('http') else voice_val
        
        job = DubbingJob(
            id=str(uuid.uuid4()), user_id=current_user.id, status='processing', 
            language=request.form.get('lang', 'ar'), voice_mode=safe_mode[:50], credits_used=cost
        )
        current_user.credits -= cost
        db.session.add(job)
        db.session.commit()

        payload = {"lang": job.language, "voice_id": voice_val, "_file_path": temp_path}
        if 'voice_sample' in request.files:
            v_file = request.files['voice_sample']
            v_path = os.path.join(tempfile.gettempdir(), f"v_{uuid.uuid4()}.wav")
            v_file.save(v_path)
            payload["_voice_path"] = v_path

        executor.submit(run_background_task, job.id, "dub", payload, cost, current_user.id)
        return jsonify({"success": True, "job_id": job.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/progress/<job_id>')
def get_progress(job_id):
    def generate():
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job: break
                yield f"data: {json.dumps({'status': job.status, 'audio_url': job.output_url})}\n\n"
                if job.status in ['completed', 'failed']: break
            time.sleep(2)
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
