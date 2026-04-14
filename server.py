# server.py — sl-Dubbing Backend (Enterprise Edition)
import os, uuid, time, logging, subprocess, re
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, ngrok-skip-browser-warning, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

# ==========================================
# ── إعدادات قاعدة البيانات (PostgreSQL) ──
# ==========================================
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///local_users.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100))
    credits = db.Column(db.Integer, default=10000) # 10 آلاف حرف مجاني كبداية
# ==========================================

AUDIO_DIR = Path('/tmp/sl_audio')
VOICE_DIR = Path('/tmp/sl_voices')
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
VOICE_DIR.mkdir(parents=True, exist_ok=True)

VOICE_CACHE = {}
XTTS_MODEL = None

def init_xtts():
    global XTTS_MODEL
    if XTTS_MODEL is not None:
        return True
    try:
        from TTS.api import TTS
        logger.info("⏳ Loading XTTS v2...")
        XTTS_MODEL = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=True)
        logger.info("✅ XTTS ready")
        return True
    except Exception as e:
        logger.error(f"XTTS load error: {e}")
        return False

# تشغيل المحرك في الخلفية عند بدء السيرفر
import threading
threading.Thread(target=init_xtts, daemon=True).start()

# ── دوال مساعدة ──
def fetch_voice_sample(voice_url, voice_id):
    if voice_id in VOICE_CACHE and Path(VOICE_CACHE[voice_id]).exists():
        return VOICE_CACHE[voice_id]
    try:
        import urllib.request
        local_path = VOICE_DIR / f"{voice_id}.wav"
        if not local_path.exists():
            tmp = VOICE_DIR / f"{voice_id}.tmp.mp3"
            urllib.request.urlretrieve(voice_url, str(tmp))
            subprocess.run(['ffmpeg', '-y', '-i', str(tmp), '-ar', '22050', '-ac', '1', str(local_path)], capture_output=True)
            tmp.unlink(missing_ok=True)
        if local_path.exists():
            VOICE_CACHE[voice_id] = str(local_path)
            return str(local_path)
    except Exception as e:
        logger.error(f"Voice download error: {e}")
    return None

def extract_source_voice(media_url, job_id):
    """استخراج بصمة صوتية من رابط يوتيوب"""
    tmp_audio = AUDIO_DIR / f"raw_{job_id}.wav"
    ref_audio = VOICE_DIR / f"ref_{job_id}.wav"
    try:
        logger.info(f"⏳ Downloading Youtube audio for cloning: {media_url}")
        subprocess.run(['yt-dlp', '-x', '--audio-format', 'wav', '-o', str(tmp_audio), media_url], check=True)
        # أخذ أول 15 ثانية كبصمة صوتية
        subprocess.run(['ffmpeg', '-y', '-i', str(tmp_audio), '-t', '15', '-ac', '1', '-ar', '22050', str(ref_audio)], check=True)
        return str(ref_audio)
    except Exception as e:
        logger.error(f"Source extraction error: {e}")
        return None

def synthesize_xtts(text, lang, voice_path, output_path):
    global XTTS_MODEL
    try:
        if XTTS_MODEL is None: return None, "xtts_init_failed"
        XTTS_MODEL.tts_to_file(text=text, speaker_wav=voice_path, language=lang[:2], file_path=output_path)
        if Path(output_path).exists(): return output_path, "xtts"
        return None, "xtts_empty"
    except Exception as e:
        return None, f"xtts_error"

def synthesize_gtts(text, lang, output_path):
    try:
        from gtts import gTTS
        gTTS(text=text, lang=lang[:2]).save(output_path)
        return output_path, "gtts"
    except Exception:
        return None, "gtts_error"

def synthesize_voice(text, lang, use_xtts=False, voice_path=None):
    output_path = str(AUDIO_DIR / f"audio_{uuid.uuid4().hex[:8]}.wav")
    if use_xtts and voice_path:
        result, method = synthesize_xtts(text, lang, voice_path, output_path)
        if result: return result, method
    return synthesize_gtts(text, lang, output_path)

def srt_time(s):
    s = s.replace(",", ".")
    p = s.split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])

def parse_srt(content):
    blocks, cur = [], None
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            if cur: blocks.append(cur)
            cur = None
        elif re.match(r"^\d+$", line):
            cur = {"i": int(line), "start": 0, "end": 0, "text": ""}
        elif "-->" in line and cur:
            p = line.split("-->")
            cur["start"] = srt_time(p[0].strip())
            cur["end"] = srt_time(p[1].strip())
        elif cur:
            cur["text"] += line + " "
    if cur: blocks.append(cur)
    return blocks

def assemble_srt(blocks, lang, use_xtts=False, voice_path=None):
    from pydub import AudioSegment
    if not blocks: return None, "no_blocks"

    total_ms = int(blocks[-1]["end"] * 1000) + 2000
    timeline = AudioSegment.silent(duration=total_ms)

    for i, b in enumerate(blocks):
        text = b["text"].strip()
        if not text: continue
        res_path, method = synthesize_voice(text, lang, use_xtts, voice_path)
        if res_path and Path(res_path).exists():
            seg = AudioSegment.from_file(res_path)
            duration = b["end"] - b["start"]
            if len(seg) / 1000.0 > duration:
                seg = seg[:int(duration * 1000)]
            timeline = timeline.overlay(seg, position=int(b["start"] * 1000))

    out = str(AUDIO_DIR / f"dub_{uuid.uuid4().hex[:8]}.mp3")
    timeline.export(out, format="mp3", bitrate="128k")
    return out, "xtts" if use_xtts else "gtts"

# ==========================================
# ── API Routes ──
# ==========================================

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'xtts_ready': XTTS_MODEL is not None})

@app.route('/api/sync-user', methods=['POST', 'OPTIONS'])
def sync_user():
    if request.method == 'OPTIONS': return jsonify({"status": "ok"}), 200
    data = request.json
    user = User.query.filter_by(email=data.get('email')).first()
    if not user:
        user = User(email=data.get('email'), name=data.get('name', 'User'), credits=10000)
        db.session.add(user)
        db.session.commit()
    return jsonify({"message": "Success", "credits": user.credits}), 200

@app.route('/api/dub', methods=['POST', 'OPTIONS'])
def dub():
    if request.method == 'OPTIONS': return jsonify({'ok': True})
    try:
        data = request.get_json(force=True) or {}
        text = data.get('text', '').strip()
        srt = data.get('srt', '')
        lang = data.get('lang', 'ar')
        user_email = data.get('email')
        voice_mode = data.get('voice_mode')
        voice_id = data.get('voice_id')
        voice_url = data.get('voice_url')
        media_url = data.get('media_url')

        # 1. التحقق من رصيد المستخدم
        user = User.query.filter_by(email=user_email).first()
        if user:
            char_count = len(text)
            if user.credits < char_count:
                return jsonify({'success': False, 'error': 'رصيدك غير كافٍ. يرجى الشحن.'}), 402
            # خصم الرصيد
            user.credits -= char_count
            db.session.commit()

        t0 = time.time()
        voice_path = None
        use_xtts = False

        # 2. تحديد مصدر الصوت (رابط يوتيوب أو ملف مسبق)
        if voice_mode == 'source' and media_url:
            voice_path = extract_source_voice(media_url, uuid.uuid4().hex[:8])
            use_xtts = True if voice_path else False
        elif voice_mode == 'xtts' and voice_url:
            voice_path = fetch_voice_sample(voice_url, voice_id)
            use_xtts = True if voice_path else False

        # 3. معالجة الدبلجة
        if srt.strip():
            blocks = parse_srt(srt)
            out, method = assemble_srt(blocks, lang, use_xtts, voice_path)
            synced = True
        else:
            out, method = synthesize_voice(text, lang, use_xtts, voice_path)
            synced = False

        if not out or not Path(out).exists():
            return jsonify({'success': False, 'error': 'فشل توليد الصوت'}), 500

        audio_url = f"https://{request.host}/api/file/{Path(out).name}"
        
        return jsonify({
            'success': True, 
            'audio_url': audio_url, 
            'method': method,
            'synced': synced,
            'time_sec': round(time.time() - t0, 1),
            'remaining_credits': user.credits if user else 0
        })
    except Exception as e:
        logger.error(f"DUB error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/file/<filename>')
def get_file(filename):
    p = AUDIO_DIR / filename
    if not p.exists(): return jsonify({'error': 'not found'}), 404
    mime = 'audio/wav' if str(p).endswith('.wav') else 'audio/mpeg'
    return send_file(str(p), mimetype=mime, as_attachment=False)

if __name__ == '__main__':
    with app.app_context():
        db.create_all() # إنشاء جداول قاعدة البيانات عند التشغيل
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
