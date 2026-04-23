# server.py — النسخة الفولاذية V7.3 (أمان + فوترة + حماية من الاختناق والانهيار)
import os, uuid, json, base64, logging, time, requests, jwt
from flask import Flask, request, jsonify, Response, make_response
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import wraps
from models import db, User, DubbingJob
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# إعدادات الأمان والذاكرة
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sl-mega-secret-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# تقليل الحد الأقصى للملفات قليلاً إلى 100MB لحماية RAM السيرفر في Railway
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024 

ALLOWED_ORIGINS = ['https://sl-dubbing.github.io', 'http://localhost:5500', 'http://127.0.0.1:5500']
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)

db.init_app(app)
executor = ThreadPoolExecutor(max_workers=50)
MODAL_URL = os.environ.get("MODAL_URL")

# ── 1. حماية المسارات (Middleware) ──
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS': return f(*args, **kwargs)
        token = request.cookies.get('sl_auth_token')
        if not token: return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            user = User.query.get(data.get('user_id'))
            if not user: raise Exception()
            request.user = user
        except: return jsonify({'error': 'Session expired'}), 401
        return f(*args, **kwargs)
    return decorated

def _make_cookie(user):
    token = jwt.encode({
        'user_id': user.id, 'exp': datetime.utcnow() + timedelta(hours=24)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    resp = make_response(jsonify({'success': True, 'user': user.to_dict()}))
    resp.set_cookie('sl_auth_token', token, httponly=True, secure=True, samesite='None', max_age=86400)
    return resp

# ── 2. مسارات المصادقة (Auth) ──
@app.route('/api/auth/register', methods=['POST'])
def register():
    d = request.get_json(silent=True) or {}
    email = d.get('email')
    password = d.get('password')
    if not email or not password: return jsonify({'error': 'Missing credentials'}), 400
    
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email exists'}), 400
    
    user = User(email=email, name=d.get('name', email.split('@')[0]), credits=500) # رصيد مجاني مبدئي
    user.set_password(password)
    db.session.add(user); db.session.commit()
    return _make_cookie(user)

@app.route('/api/auth/login', methods=['POST'])
def login():
    d = request.get_json(silent=True) or {}
    user = User.query.filter_by(email=d.get('email')).first()
    if not user or not user.check_password(d.get('password')):
        return jsonify({'error': 'Invalid credentials'}), 401
    return _make_cookie(user)

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('sl_auth_token', '', expires=0, httponly=True, secure=True, samesite='None')
    return resp

@app.route('/api/user')
@require_auth
def get_user():
    return jsonify({'success': True, 'user': request.user.to_dict()})

# ── 3. محرك المعالجة الخلفية ──
def run_background_task(job_id, endpoint, payload, cost, user_id):
    with app.app_context():
        job = DubbingJob.query.get(job_id)
        user = User.query.get(user_id)
        if not job or not user: return
        
        try:
            target = f"{MODAL_URL.rstrip('/')}/{endpoint.lstrip('/')}"
            res = requests.post(target, json=payload, timeout=900)
            data = res.json()
            
            if data.get("success"):
                job.output_url = data.get("audio_url")
                job.extra_data = data.get("translated_text") or data.get("final_text")
                job.status = 'completed'
            else:
                raise Exception(data.get("error", "Unknown factory error"))
                
        except Exception as e:
            logging.error(f"Task Failed: {e}")
            job.status = 'failed'
            # 🟢 استرجاع الرصيد للمستخدم في حال فشل الذكاء الاصطناعي
            user.credits += cost 
            
        db.session.commit()

# ── 4. مسارات الدبلجة و TTS ──
@app.route('/api/dub', methods=['POST'])
@require_auth
def dub():
    user = request.user
    cost = 100
    
    # 🟢 الحماية المالية: التأكد من وجود رصيد كافٍ
    if user.credits < cost:
        return jsonify({"error": "Insufficient credits"}), 402

    if 'media_file' not in request.files or request.files['media_file'].filename == '':
        return jsonify({"error": "Valid media file is required"}), 400

    file = request.files['media_file']
    file_b64 = base64.b64encode(file.read()).decode('utf-8')
    
    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, status='processing', 
                     language=request.form.get('lang', 'en'), voice_mode=request.form.get('voice_id', 'xtts'))
    
    # 🟢 خصم الرصيد مقدماً
    user.credits -= cost
    db.session.add(job); db.session.commit()
    
    payload = {
        "file_b64": file_b64, 
        "lang": request.form.get('lang', 'en'),
        "voice_id": request.form.get('voice_id', ''),
        "sample_b64": request.form.get('sample_b64', '')
    }
    executor.submit(run_background_task, job_id, "", payload, cost, user.id)
    return jsonify({"success": True, "job_id": job_id})

@app.route('/api/tts', methods=['POST'])
@require_auth
def tts():
    user = request.user
    cost = 20
    
    if user.credits < cost:
        return jsonify({"error": "Insufficient credits"}), 402

    d = request.get_json(silent=True) or {}
    if not d.get('text'): return jsonify({"error": "Text is required"}), 400

    job_id = str(uuid.uuid4())
    job = DubbingJob(id=job_id, user_id=user.id, status='processing', 
                     language=d.get('lang', 'en'), voice_mode='tts')
    
    user.credits -= cost
    db.session.add(job); db.session.commit()
    
    executor.submit(run_background_task, job_id, "tts", d, cost, user.id)
    return jsonify({"success": True, "job_id": job_id})

@app.route('/api/progress/<job_id>')
@require_auth
def progress(job_id):
    def stream():
        while True:
            with app.app_context():
                job = DubbingJob.query.get(job_id)
                if not job or job.user_id != request.user.id: 
                    yield f"data: {json.dumps({'status': 'error', 'error': 'Unauthorized'})}\n\n"
                    break
                
                payload = {
                    'status': job.status, 
                    'progress': 100 if job.status=='completed' else 50, 
                    'audio_url': job.output_url, 
                    'final_text': job.extra_data
                }
                yield f"data: {json.dumps(payload)}\n\n"
                if job.status in ['completed', 'failed', 'error']: break
            # وقت الانتظار
            time.sleep(2)
    return Response(stream(), mimetype='text/event-stream')

@app.route('/api/job/<job_id>')
@require_auth
def get_job(job_id):
    job = DubbingJob.query.get(job_id)
    if not job or job.user_id != request.user.id: return jsonify({'error': 'Unauthorized'}), 403
    return jsonify({'status': job.status, 'audio_url': job.output_url})

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({"error": "File is too large. Maximum size is 100MB."}), 413

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
