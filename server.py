# server.py — sl-Dubbing Backend (Enterprise Edition - SECURED & ENHANCED)
import os, uuid, time, logging, subprocess, re, json, gc, sys
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# FLASK & CORS SETUP
# ============================================================================

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

# ============================================================================
# DATABASE CONFIGURATION
# ============================================================================

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    logger.warning("⚠️ DATABASE_URL not set, using SQLite (dev only)")
    DATABASE_URL = 'sqlite:///sl_dubbing.db'

if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JSON_SORT_KEYS'] = False

db = SQLAlchemy(app)

# ============================================================================
# DATABASE MODELS
# ============================================================================

class User(db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    avatar = db.Column(db.String(500), default='👤')
    credits = db.Column(db.Integer, default=50000) 
    password_hash = db.Column(db.String(255), nullable=True)
    auth_method = db.Column(db.String(50), default='oauth')
    last_login = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    jobs = db.relationship('DubbingJob', backref='user', lazy=True, cascade='all, delete-orphan')
    credit_history = db.relationship('CreditTransaction', backref='user', lazy=True, cascade='all, delete-orphan')
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'name': self.name,
            'avatar': self.avatar,
            'credits': self.credits,
            'auth_method': self.auth_method,
            'created_at': self.created_at.isoformat()
        }

class DubbingJob(db.Model):
    __tablename__ = 'dubbing_jobs'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    status = db.Column(db.String(20), default='pending')
    language = db.Column(db.String(10), nullable=False)
    voice_mode = db.Column(db.String(50), nullable=False) 
    voice_id = db.Column(db.String(100), nullable=True)
    
    text_length = db.Column(db.Integer, default=0)
    credits_used = db.Column(db.Integer, default=0)
    
    input_url = db.Column(db.String(500), nullable=True)
    output_url = db.Column(db.String(500), nullable=True)
    
    error_message = db.Column(db.Text, nullable=True)
    processing_time = db.Column(db.Float, nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'status': self.status,
            'language': self.language,
            'voice_mode': self.voice_mode,
            'text_length': self.text_length,
            'credits_used': self.credits_used,
            'output_url': self.output_url,
            'error': self.error_message,
            'processing_time': self.processing_time,
            'created_at': self.created_at.isoformat()
        }

class CreditTransaction(db.Model):
    __tablename__ = 'credit_transactions'
    
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    transaction_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(200), nullable=False)
    
    job_id = db.Column(db.String(36), nullable=True)
    payment_id = db.Column(db.String(100), nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    def to_dict(self):
        return {
            'id': self.id,
            'type': self.transaction_type,
            'amount': self.amount,
            'reason': self.reason,
            'created_at': self.created_at.isoformat()
        }

# ============================================================================
# DIRECTORY SETUP
# ============================================================================

AUDIO_DIR = Path('/tmp/sl_audio')
VOICE_DIR = Path('/tmp/sl_voices')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
VOICE_DIR.mkdir(parents=True, exist_ok=True)
VOICE_CACHE = {}

# ============================================================================
# SMART ENGINE MANAGER (LAZY LOADING)
# ============================================================================

XTTS_MODEL = None
COSY_MODEL = None
ACTIVE_ENGINE = None

def unload_engines():
    global XTTS_MODEL, COSY_MODEL, ACTIVE_ENGINE
    if XTTS_MODEL is not None or COSY_MODEL is not None:
        logger.info("🧹 Unloading AI Models to free VRAM...")
        XTTS_MODEL = None
        COSY_MODEL = None
        ACTIVE_ENGINE = None
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()

def get_cosy():
    global COSY_MODEL, ACTIVE_ENGINE
    if ACTIVE_ENGINE == 'xtts': 
        unload_engines()
    
    if COSY_MODEL is None:
        try:
            from modelscope import snapshot_download
            if '/app/CosyVoice' not in sys.path:
                sys.path.append('/app/CosyVoice')
            from cosyvoice.cli.cosyvoice import CosyVoice
            logger.info("⏳ Loading CosyVoice 3.0...")
            model_dir = snapshot_download('iic/CosyVoice-300M')
            COSY_MODEL = CosyVoice(model_dir)
            ACTIVE_ENGINE = 'cosy'
            logger.info("✅ CosyVoice 3.0 loaded successfully")
        except Exception as e:
            logger.error(f"❌ CosyVoice initialization failed: {e}")
            return None
    return COSY_MODEL

def get_xtts():
    global XTTS_MODEL, ACTIVE_ENGINE
    if ACTIVE_ENGINE == 'cosy': 
        unload_engines()
        
    if XTTS_MODEL is None:
        try:
            from TTS.api import TTS
            logger.info("⏳ Loading XTTS v2...")
            XTTS_MODEL = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=True)
            ACTIVE_ENGINE = 'xtts'
            logger.info("✅ XTTS v2 loaded successfully")
        except Exception as e:
            logger.error(f"❌ XTTS initialization failed: {e}")
            return None
    return XTTS_MODEL

import threading
init_thread = threading.Thread(target=get_xtts, daemon=True)
init_thread.start()

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'active_engine': ACTIVE_ENGINE,
        'database': 'connected' if db.session.execute(db.text('SELECT 1')) else 'error'
    })

# ── مسار التحقق الرسمي من جوجل ──
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "497619073475-6vjelufub8gci231ettdhmk5pv0cdde3.apps.googleusercontent.com")

@app.route('/api/auth/google', methods=['POST', 'OPTIONS'])
def google_auth():
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    
    try:
        data = request.get_json()
        token = data.get('credential')
        
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        
        email = idinfo['email']
        name = idinfo.get('name', email.split('@')[0])
        picture = idinfo.get('picture', '👤')
        
        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(
                email=email,
                name=name,
                avatar=picture,
                auth_method='google',
                credits=50000
            )
            db.session.add(user)
            logger.info(f"✅ New real Google user created: {email}")
        else:
            user.last_login = datetime.utcnow()
            user.avatar = picture 
            logger.info(f"✅ Google user logged in: {email}")
            
        db.session.commit()
        return jsonify({'success': True, 'user': user.to_dict()}), 200
        
    except ValueError as e:
        logger.error(f"❌ Invalid Google Token: {e}")
        return jsonify({'success': False, 'error': 'توكن غير صالح من جوجل'}), 401
    except Exception as e:
        logger.error(f"❌ Google Auth Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user', methods=['GET'])
def get_user():
    email = request.headers.get('X-User-Email')
    if not email: return jsonify({'error': 'Unauthorized'}), 401
    user = User.query.filter_by(email=email).first()
    if not user: return jsonify({'error': 'User not found'}), 404
    return jsonify({'success': True, 'user': user.to_dict(), 'credits': user.credits}), 200

# باقي الدوال المساعدة للترجمة والتوليد (نفسها تماماً)
def deduct_credits(user, text_length):
    if user.credits < text_length: return False, "رصيدك غير كافٍ"
    user.credits -= text_length
    transaction = CreditTransaction(user_id=user.id, transaction_type='usage', amount=-text_length, reason=f'Text generation: {text_length} characters')
    db.session.add(transaction)
    db.session.commit()
    return True, user.credits

def fetch_voice_sample(voice_url, voice_id):
    if voice_id in VOICE_CACHE and Path(VOICE_CACHE[voice_id]).exists(): return VOICE_CACHE[voice_id]
    try:
        import urllib.request
        local_path = VOICE_DIR / f"{voice_id}.wav"
        if not local_path.exists():
            tmp = VOICE_DIR / f"{voice_id}.tmp.mp3"
            urllib.request.urlretrieve(voice_url, str(tmp))
            subprocess.run(['ffmpeg', '-y', '-i', str(tmp), '-ar', '22050', '-ac', '1', str(local_path)], capture_output=True, timeout=30)
            tmp.unlink(missing_ok=True)
        if local_path.exists():
            VOICE_CACHE[voice_id] = str(local_path)
            return str(local_path)
    except Exception as e: logger.error(f"Voice download error: {e}")
    return None

def synthesize_cosy(text, voice_path, output_path):
    try:
        engine = get_cosy()
        if engine is None: return None, "CosyVoice not ready"
        from cosyvoice.utils.file_utils import load_wav
        import torchaudio
        prompt_speech_16k = load_wav(voice_path, 16000)
        output = engine.inference_zero_shot(text, "نص العينة للمحاكاة", prompt_speech_16k)
        torchaudio.save(output_path, output['tts_speech'], 22050)
        if Path(output_path).exists(): return output_path, "cosyvoice"
        return None, "Empty output"
    except Exception as e:
        logger.error(f"CosyVoice error: {e}")
        return None, str(e)

def synthesize_xtts(text, lang, voice_path, output_path):
    try:
        engine = get_xtts()
        if engine is None: return None, "XTTS not ready"
        engine.tts_to_file(text=text, speaker_wav=voice_path, language=lang[:2], file_path=output_path)
        if Path(output_path).exists(): return output_path, "xtts"
        return None, "Empty output"
    except Exception as e:
        logger.error(f"XTTS error: {e}")
        return None, str(e)

def synthesize_gtts(text, lang, output_path):
    try:
        from gtts import gTTS
        gTTS(text=text, lang=lang[:2]).save(output_path)
        return output_path, "gtts"
    except Exception as e:
        logger.error(f"gTTS error: {e}")
        return None, str(e)

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
def dub():
    if request.method == 'OPTIONS': return jsonify({'ok': True}), 200
    try:
        data = request.get_json(force=True) or {}
        email = data.get('email', '').lower().strip()
        text = data.get('text', '').strip()
        srt = data.get('srt', '').strip()
        lang = data.get('lang', 'ar')
        voice_mode = data.get('voice_mode', 'gtts')
        voice_id = data.get('voice_id', '')
        voice_url = data.get('voice_url', '')
        
        user = User.query.filter_by(email=email).first()
        if not user: return jsonify({'success': False, 'error': 'User not found'}), 404
        
        text_length = len(text) if text else len(srt)
        if text_length < 5: return jsonify({'success': False, 'error': 'Text too short'}), 400
        
        if user.credits < text_length: return jsonify({'success': False, 'error': 'رصيدك غير كافٍ'}), 402
        
        job_id = str(uuid.uuid4())
        job = DubbingJob(id=job_id, user_id=user.id, language=lang, voice_mode=voice_mode, text_length=text_length)
        db.session.add(job)
        
        success, result = deduct_credits(user, text_length)
        if not success: return jsonify({'success': False, 'error': result}), 402
        
        job.credits_used = text_length
        job.status = 'processing'
        db.session.commit()
        
        t0 = time.time()
        voice_path = fetch_voice_sample(voice_url, voice_id) if voice_url else None
        output_path = str(AUDIO_DIR / f"dub_{job_id}.mp3")
        method = "none"
        
        if voice_mode == 'cosy' and voice_path:
            output_path, method = synthesize_cosy(text, voice_path, output_path)
        elif voice_mode == 'xtts' and voice_path:
            output_path, method = synthesize_xtts(text, lang, voice_path, output_path)
        else:
            output_path, method = synthesize_gtts(text, lang, output_path)
        
        if not output_path or not Path(output_path).exists():
            job.status = 'failed'
            db.session.commit()
            return jsonify({'success': False, 'error': 'فشل توليد الصوت'}), 500
        
        audio_url = f"https://{request.host}/api/file/{Path(output_path).name}"
        job.output_url = audio_url
        job.status = 'completed'
        job.processing_time = time.time() - t0
        db.session.commit()
        
        return jsonify({'success': True, 'job_id': job_id, 'audio_url': audio_url, 'method': method, 'remaining_credits': user.credits}), 200
    
    except Exception as e:
        logger.error(f"DUB error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/file/<filename>')
def get_file(filename):
    p = AUDIO_DIR / filename
    if not p.exists(): return jsonify({'error': 'File not found'}), 404
    mime = 'audio/wav' if str(p).endswith('.wav') else 'audio/mpeg'
    return send_file(str(p), mimetype=mime, as_attachment=False)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
