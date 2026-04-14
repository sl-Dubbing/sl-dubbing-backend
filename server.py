# server.py — sl-Dubbing Backend (Enterprise Edition - SECURED & ENHANCED)
import os, uuid, time, logging, subprocess, re, json
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

# Fix for PostgreSQL URI format
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
    avatar = db.Column(db.String(10), default='👤')
    credits = db.Column(db.Integer, default=50000)  # 50,000 characters free
    password_hash = db.Column(db.String(255), nullable=True)  # For email signup
    auth_method = db.Column(db.String(50), default='oauth')  # 'oauth' or 'email'
    last_login = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    # Relationships
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
    
    status = db.Column(db.String(20), default='pending')  # pending, processing, completed, failed
    language = db.Column(db.String(10), nullable=False)
    voice_mode = db.Column(db.String(50), nullable=False)  # 'xtts', 'gtts', 'source'
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
    
    transaction_type = db.Column(db.String(20), nullable=False)  # 'usage', 'purchase', 'refund', 'bonus'
    amount = db.Column(db.Integer, nullable=False)  # positive or negative
    reason = db.Column(db.String(200), nullable=False)
    
    job_id = db.Column(db.String(36), nullable=True)  # reference to DubbingJob if usage
    payment_id = db.Column(db.String(100), nullable=True)  # reference to payment gateway
    
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
# AUTHENTICATION MIDDLEWARE
# ============================================================================

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        email = request.headers.get('X-User-Email')
        if not email:
            return jsonify({'error': 'Unauthorized'}), 401
        
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        request.user = user
        return f(*args, **kwargs)
    
    return decorated_function

# ============================================================================
# DIRECTORY SETUP
# ============================================================================

AUDIO_DIR = Path('/tmp/sl_audio')
VOICE_DIR = Path('/tmp/sl_voices')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
VOICE_DIR.mkdir(parents=True, exist_ok=True)

VOICE_CACHE = {}
XTTS_MODEL = None

# ============================================================================
# XTTS MODEL INITIALIZATION
# ============================================================================

def init_xtts():
    global XTTS_MODEL
    if XTTS_MODEL is not None:
        return True
    try:
        from TTS.api import TTS
        logger.info("⏳ Loading XTTS v2...")
        XTTS_MODEL = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=True)
        logger.info("✅ XTTS v2 loaded successfully")
        return True
    except Exception as e:
        logger.error(f"❌ XTTS initialization failed: {e}")
        XTTS_MODEL = None
        return False

import threading
init_thread = threading.Thread(target=init_xtts, daemon=True)
init_thread.start()

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def deduct_credits(user, text_length):
    """Deduct credits from user account"""
    if user.credits < text_length:
        return False, "رصيدك غير كافٍ"
    
    user.credits -= text_length
    transaction = CreditTransaction(
        user_id=user.id,
        transaction_type='usage',
        amount=-text_length,
        reason=f'Text generation: {text_length} characters'
    )
    db.session.add(transaction)
    db.session.commit()
    return True, user.credits

def fetch_voice_sample(voice_url, voice_id):
    """Download and cache voice sample"""
    if voice_id in VOICE_CACHE and Path(VOICE_CACHE[voice_id]).exists():
        return VOICE_CACHE[voice_id]
    
    try:
        import urllib.request
        local_path = VOICE_DIR / f"{voice_id}.wav"
        if not local_path.exists():
            tmp = VOICE_DIR / f"{voice_id}.tmp.mp3"
            urllib.request.urlretrieve(voice_url, str(tmp))
            subprocess.run(
                ['ffmpeg', '-y', '-i', str(tmp), '-ar', '22050', '-ac', '1', str(local_path)],
                capture_output=True,
                timeout=30
            )
            tmp.unlink(missing_ok=True)
        
        if local_path.exists():
            VOICE_CACHE[voice_id] = str(local_path)
            return str(local_path)
    except Exception as e:
        logger.error(f"Voice download error: {e}")
    
    return None

def extract_source_voice(media_url, job_id):
    """Extract voice from YouTube URL"""
    tmp_audio = AUDIO_DIR / f"raw_{job_id}.wav"
    ref_audio = VOICE_DIR / f"ref_{job_id}.wav"
    
    try:
        logger.info(f"⏳ Downloading YouTube audio: {media_url}")
        subprocess.run(
            ['yt-dlp', '-x', '--audio-format', 'wav', '-o', str(tmp_audio), media_url],
            check=True,
            timeout=120
        )
        
        # Take first 15 seconds as voice sample
        subprocess.run(
            ['ffmpeg', '-y', '-i', str(tmp_audio), '-t', '15', '-ac', '1', '-ar', '22050', str(ref_audio)],
            check=True,
            timeout=30
        )
        
        logger.info(f"✅ Voice extracted: {ref_audio}")
        return str(ref_audio)
    except Exception as e:
        logger.error(f"Source extraction error: {e}")
        return None

def synthesize_xtts(text, lang, voice_path, output_path):
    """Generate speech using XTTS v2"""
    global XTTS_MODEL
    try:
        if XTTS_MODEL is None:
            return None, "XTTS not ready"
        
        XTTS_MODEL.tts_to_file(
            text=text,
            speaker_wav=voice_path,
            language=lang[:2],
            file_path=output_path
        )
        
        if Path(output_path).exists():
            return output_path, "xtts"
        return None, "Empty output"
    except Exception as e:
        logger.error(f"XTTS error: {e}")
        return None, str(e)

def synthesize_gtts(text, lang, output_path):
    """Generate speech using Google TTS"""
    try:
        from gtts import gTTS
        gTTS(text=text, lang=lang[:2]).save(output_path)
        return output_path, "gtts"
    except Exception as e:
        logger.error(f"gTTS error: {e}")
        return None, str(e)

def srt_time(s):
    """Parse SRT timestamp"""
    s = s.replace(",", ".")
    p = s.split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])

def parse_srt(content):
    """Parse SRT subtitle file"""
    blocks, cur = [], None
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            if cur:
                blocks.append(cur)
            cur = None
        elif re.match(r"^\d+$", line):
            if cur:
                blocks.append(cur)
            cur = {"i": int(line), "start": 0, "end": 0, "text": ""}
        elif "-->" in line and cur:
            p = line.split("-->")
            cur["start"] = srt_time(p[0].strip())
            cur["end"] = srt_time(p[1].strip())
        elif cur:
            cur["text"] += line + " "
    
    if cur:
        blocks.append(cur)
    return blocks

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/api/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'xtts_ready': XTTS_MODEL is not None,
        'database': 'connected' if db.session.execute(db.text('SELECT 1')) else 'error'
    })

@app.route('/api/sync-user', methods=['POST', 'OPTIONS'])
def sync_user():
    """Sync user from frontend"""
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'}), 200
    
    try:
        data = request.get_json()
        email = data.get('email', '').lower().strip()
        name = data.get('name', 'User')
        avatar = data.get('avatar', '👤')
        auth_method = data.get('method', 'oauth')
        
        if not email or '@' not in email:
            return jsonify({'error': 'Invalid email'}), 400
        
        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(
                email=email,
                name=name,
                avatar=avatar,
                auth_method=auth_method,
                credits=50000  # 50K free characters
            )
            db.session.add(user)
            logger.info(f"✅ New user created: {email}")
        else:
            user.last_login = datetime.utcnow()
            logger.info(f"✅ User logged in: {email}")
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'User synced',
            'user': user.to_dict()
        }), 200
    
    except Exception as e:
        logger.error(f"Sync error: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/user', methods=['GET'])
def get_user():
    """Get current user info"""
    email = request.headers.get('X-User-Email')
    if not email:
        return jsonify({'error': 'Unauthorized'}), 401
    
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    return jsonify({
        'success': True,
        'user': user.to_dict(),
        'credits': user.credits
    }), 200

@app.route('/api/credits/history', methods=['GET'])
def get_credit_history():
    """Get user's credit transaction history"""
    email = request.headers.get('X-User-Email')
    if not email:
        return jsonify({'error': 'Unauthorized'}), 401
    
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    limit = request.args.get('limit', 50, type=int)
    transactions = CreditTransaction.query.filter_by(user_id=user.id) \
        .order_by(CreditTransaction.created_at.desc()) \
        .limit(limit) \
        .all()
    
    return jsonify({
        'success': True,
        'credits': user.credits,
        'transactions': [t.to_dict() for t in transactions]
    }), 200

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
def dub():
    """Process dubbing request"""
    if request.method == 'OPTIONS':
        return jsonify({'ok': True}), 200
    
    try:
        data = request.get_json(force=True) or {}
        
        # Validate input
        email = data.get('email', '').lower().strip()
        text = data.get('text', '').strip()
        srt = data.get('srt', '').strip()
        lang = data.get('lang', 'ar')
        voice_mode = data.get('voice_mode', 'muhamed')
        voice_id = data.get('voice_id', '')
        voice_url = data.get('voice_url', '')
        media_url = data.get('media_url', '').strip()
        
        if not email or '@' not in email:
            return jsonify({'success': False, 'error': 'Invalid email'}), 400
        
        # Get user
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Validate text
        if not text and not srt:
            return jsonify({'success': False, 'error': 'Text or SRT required'}), 400
        
        text_length = len(text) if text else len(srt)
        if text_length < 5:
            return jsonify({'success': False, 'error': 'Text too short'}), 400
        
        if text_length > 50000:
            text = text[:50000]
            logger.warning(f"Text truncated to 50k chars for {email}")
        
        # Check credits
        if user.credits < text_length:
            return jsonify({
                'success': False,
                'error': 'رصيدك غير كافٍ',
                'credits_needed': text_length,
                'credits_available': user.credits
            }), 402
        
        # Create job record
        job_id = str(uuid.uuid4())
        job = DubbingJob(
            id=job_id,
            user_id=user.id,
            language=lang,
            voice_mode=voice_mode,
            voice_id=voice_id,
            text_length=text_length,
            input_url=media_url or 'local_file'
        )
        db.session.add(job)
        
        # Deduct credits
        success, result = deduct_credits(user, text_length)
        if not success:
            return jsonify({'success': False, 'error': result}), 402
        
        job.credits_used = text_length
        job.status = 'processing'
        db.session.commit()
        
        logger.info(f"🎯 Processing job {job_id} for {email} ({text_length} chars)")
        
        # Process audio
        t0 = time.time()
        voice_path = None
        use_xtts = False
        
        if voice_mode == 'source' and media_url:
            voice_path = extract_source_voice(media_url, job_id)
            use_xtts = voice_path is not None
        elif voice_mode == 'xtts' and voice_url:
            voice_path = fetch_voice_sample(voice_url, voice_id)
            use_xtts = voice_path is not None
        
        output_path = str(AUDIO_DIR / f"dub_{job_id}.mp3")
        
        if use_xtts and voice_path:
            output_path, method = synthesize_xtts(text, lang, voice_path, output_path)
        else:
            output_path, method = synthesize_gtts(text, lang, output_path)
        
        if not output_path or not Path(output_path).exists():
            job.status = 'failed'
            job.error_message = 'Audio generation failed'
            db.session.commit()
            return jsonify({'success': False, 'error': 'فشل توليد الصوت'}), 500
        
        # Upload to CDN or generate public URL
        audio_url = f"https://{request.host}/api/file/{Path(output_path).name}"
        job.output_url = audio_url
        job.status = 'completed'
        job.processing_time = time.time() - t0
        db.session.commit()
        
        logger.info(f"✅ Job {job_id} completed in {job.processing_time:.1f}s")
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'audio_url': audio_url,
            'processing_time': job.processing_time,
            'remaining_credits': user.credits,
            'message': 'الدبلجة جاهزة للتحميل!'
        }), 200
    
    except Exception as e:
        logger.error(f"DUB error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/file/<filename>')
def get_file(filename):
    """Download processed audio file"""
    p = AUDIO_DIR / filename
    if not p.exists():
        return jsonify({'error': 'File not found'}), 404
    
    mime = 'audio/mpeg'  # MP3 by default
    return send_file(str(p), mimetype=mime, as_attachment=False)

@app.route('/api/jobs', methods=['GET'])
def get_jobs():
    """Get user's dubbing jobs"""
    email = request.headers.get('X-User-Email')
    if not email:
        return jsonify({'error': 'Unauthorized'}), 401
    
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    limit = request.args.get('limit', 20, type=int)
    jobs = DubbingJob.query.filter_by(user_id=user.id) \
        .order_by(DubbingJob.created_at.desc()) \
        .limit(limit) \
        .all()
    
    return jsonify({
        'success': True,
        'jobs': [j.to_dict() for j in jobs]
    }), 200

# ============================================================================
# DATABASE INITIALIZATION
# ============================================================================

with app.app_context():
    db.create_all()
    logger.info("✅ Database tables initialized")

# ============================================================================
# RUN SERVER
# ============================================================================

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"
    app.run(host='0.0.0.0', port=port, debug=debug, threaded=True)
