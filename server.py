"""
sl-Dubbing Server — V8 (Unified Enterprise Edition)
Features:
1. Advanced Dubbing with Dynamic Timeline & Context-Aware Translation.
2. Direct Text-to-Speech (TTS) Endpoint with Long Text Splitting.
3. Cloudflare R2 Integration (Stateless Storage).
4. Studio Loudness Normalization (EBU R128).
5. PostgreSQL Database Integration for User Management & Credits.
"""
import os, uuid, threading, logging, json, time, subprocess, urllib.request, re, unicodedata, random
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from deep_translator import GoogleTranslator
from pydub import AudioSegment
from pydub.silence import detect_nonsilent
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import boto3
from botocore.config import Config

# ── تحميل الإعدادات السحابية (Cloudflare R2) ──
load_dotenv()
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_DOMAIN = os.getenv("R2_PUBLIC_DOMAIN")

USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME])
s3_client = None

if USE_R2:
    s3_client = boto3.client(
        's3',
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version='s3v4'),
        region_name='auto'
    )
    print("✅ تم تفعيل التخزين السحابي Cloudflare R2")

# ── إعدادات البيئة ──
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ==========================================
# ── إعدادات قاعدة البيانات (PostgreSQL) ──
# ==========================================
# نستخدم رابط السحابة، وإذا لم يجده (في جهازك المحلي) يستخدم ملف SQLite للتجربة
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///local_users.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ── تعريف جدول المستخدمين ──
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100))
    credits = db.Column(db.Integer, default=10000) # 10 آلاف حرف مجاني كبداية
# ==========================================

BASE_DIR    = Path(__file__).parent.absolute()
AUDIO_DIR   = BASE_DIR / 'temp_audio'
VOICE_DIR   = BASE_DIR / 'voices'
SPEAKER_DIR = BASE_DIR / 'speakers'
for d in [AUDIO_DIR, VOICE_DIR, SPEAKER_DIR]: d.mkdir(parents=True, exist_ok=True)

XTTS_MODEL, XTTS_READY = None, False
xtts_lock = threading.Lock()
gpu_lock  = threading.Lock()
JOBS: dict = {}
SPEAKERS_META: dict = {}

DEFAULT_SPEAKERS = [{"speaker_id": "muhammad", "label": "محمد", "url": "https://res.cloudinary.com/dxbmvzsiz/video/upload/v1775679718/muhammad.wav"}]

def _patch_torch():
    try:
        import torch
        _orig = torch.load
        def _p(*a, **kw): kw['weights_only'] = False; return _orig(*a, **kw)
        torch.load = _p
    except Exception: pass
_patch_torch()

def get_xtts():
    global XTTS_MODEL, XTTS_READY
    if XTTS_MODEL is not None: return XTTS_MODEL
    with xtts_lock:
        if XTTS_MODEL is not None: return XTTS_MODEL
        import torch
        from TTS.api import TTS
        logger.info("⏳ تحميل XTTS v2...")
        XTTS_MODEL = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=torch.cuda.is_available())
        XTTS_READY = True
        logger.info(f"✅ XTTS جاهز على {'GPU' if torch.cuda.is_available() else 'CPU'}")
        return XTTS_MODEL
threading.Thread(target=get_xtts, daemon=True).start()

# ── دوال المساعدة الصوتية ──
def get_dur_ms(path):
    try:
        r = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1', str(path)], capture_output=True, text=True, timeout=10)
        return int(float(r.stdout.strip())*1000) if r.stdout.strip() else 0
    except: return 0

def _clean_spk(raw, out):
    try:
        subprocess.run(['ffmpeg','-y','-i',str(raw), '-ac','1','-ar','22050','-af','afftdn=nf=-20,loudnorm,highpass=f=80', '-t','12', str(out)], capture_output=True, check=True)
        return True
    except: return False

def prepare_speaker_wav(path):
    p = Path(path); out = p.parent/(p.stem+"_clean.wav")
    if out.exists(): return str(out)
    return str(out) if _clean_spk(p, out) else path

def extract_speaker_reference(orig_path, job_id):
    ref = VOICE_DIR/f"ref_{job_id}.wav"
    tmp_slice = AUDIO_DIR/f"slice_{job_id}.wav"
    try:
        subprocess.run(['ffmpeg','-y','-i',orig_path,'-t','180',str(tmp_slice)], capture_output=True, check=True)
        audio = AudioSegment.from_file(str(tmp_slice)).set_channels(1)
        chunks = detect_nonsilent(audio, min_silence_len=500, silence_thresh=-35)
        clip = None
        for s,e in chunks:
            if 4000 <= (e-s) <= 12000: clip=audio[s:e]; break
        if not clip and chunks:
            best = max(chunks, key=lambda x:x[1]-x[0])
            clip = audio[best[0]:min(best[1],best[0]+10000)]
        if not clip: clip = audio[:min(10000,len(audio))]
        if clip and len(clip) >= 1000:
            clip.export(str(ref), format="wav")
            clean_ref = ref.parent/(ref.stem+"_clean.wav")
            if _clean_spk(ref, clean_ref): return str(clean_ref)
            return str(ref)
    except: pass
    finally:
        if tmp_slice.exists(): tmp_slice.unlink()
    return None

# ── دوال معالجة النصوص ──
_URL_RE = re.compile(r'https?://\S+|www.\S+'); _HASH_RE = re.compile(r'#\w+'); _MENTION_RE = re.compile(r'@\w+'); _HTML_RE = re.compile(r'<[^>]+>'); _BRACKET_RE = re.compile(r'\(.*?\)|\[.*?\]|\{.*?\}') 
_MULTI_PUN = re.compile(r'([.!?,؟،;:؛])\1+'); _ELLIP_RE = re.compile(r'\.{2,}'); _COMMON_EN = {'ai':'الذكاء الاصطناعي','ok':'حسناً','yes':'نعم','no':'لا','youtube':'يوتيوب'}

def sanitize_text(text, lang='ar'):
    if not text: return None
    text = unicodedata.normalize('NFKC', text)
    for rx in [_URL_RE, _HASH_RE, _MENTION_RE, _HTML_RE, _BRACKET_RE]: text = rx.sub('', text)
    if lang in ('ar','fa','ur'):
        for k,v in _COMMON_EN.items(): text = re.sub(r'\b'+k+r'\b', v, text, flags=re.IGNORECASE)
    text = _ELLIP_RE.sub('،', text)
    text = _MULTI_PUN.sub(r'\1', text)
    return text.strip() if text.strip() else None

def split_long_text(text, max_chars=180):
    if len(text) <= max_chars: return [text]
    mid = len(text) // 2
    for sep in ['. ', '؟ ', '! ', '، ', ', ']:
        idx = text.rfind(sep, max(0, mid-60), min(len(text), mid+60))
        if idx != -1:
            l, r = text[:idx+len(sep)].strip(), text[idx+len(sep):].strip()
            if l and r: return split_long_text(l, max_chars) + split_long_text(r, max_chars)
    return [text]

# ── دوال التوليد والمعالجة ──
def tts_generate(xtts, text, wav_ref, lang, out_path, speed=1.0):
    clean = sanitize_text(text, lang)
    if not clean: return False
    temp = 0.75 + random.uniform(-0.02, 0.02)
    try:
        with gpu_lock:
            xtts.tts_to_file(text=clean, speaker_wav=wav_ref, language=lang, file_path=str(out_path), speed=speed, temperature=temp)
        return out_path.exists() and out_path.stat().st_size > 100
    except Exception as e:
        logger.error(f"TTS Generate Error: {e}")
        return False

def process_audio_segment(clip_path, target_ms):
    if target_ms <= 0:
        tmp_ff = AUDIO_DIR / f"ff_{uuid.uuid4().hex[:6]}.wav"
        try:
            subprocess.run(['ffmpeg', '-i', str(clip_path), '-af', "loudnorm=I=-14:TP=-1.5:LRA=11", '-ar', '44100', '-ac', '2', '-y', str(tmp_ff)], capture_output=True)
            return tmp_ff if tmp_ff.exists() else clip_path
        except: return clip_path

    tmp_ff = AUDIO_DIR / f"ff_{uuid.uuid4().hex[:6]}.wav"
    actual_ms = get_dur_ms(str(clip_path))
    filters = ["loudnorm=I=-14:TP=-1.5:LRA=11"]
    
    if actual_ms > target_ms:
        ratio = actual_ms / target_ms
        if ratio > 1.02:
            r = min(ratio, 1.15)
            filters.append(f"atempo={r:.4f}")
            
    try:
        subprocess.run(['ffmpeg', '-i', str(clip_path), '-af', ",".join(filters), '-ar', '44100', '-ac', '2', '-y', str(tmp_ff)], capture_output=True)
        if tmp_ff.exists() and tmp_ff.stat().st_size > 100:
            return tmp_ff
    except: pass
    return clip_path

def upload_to_r2(local_path, object_name):
    if not USE_R2: return None
    try:
        s3_client.upload_file(str(local_path), R2_BUCKET_NAME, object_name, ExtraArgs={'ContentType': 'audio/wav'})
        return f"{R2_PUBLIC_DOMAIN}/{object_name}"
    except Exception as e:
        logger.error(f"❌ R2 Upload Error: {e}")
        return None

# ── محرك الدبلجة ──
def run_dub_job(job_id, data):
    try:
        segments = data.get('segments', [])
        target_lang = data.get('lang', 'ar')
        source_lang = data.get('source_lang', 'auto')
        url = data.get('url','').strip()
        orig_path = data.get('orig_audio_path')
        speaker_id = data.get('speaker_id')

        JOBS[job_id].update({"status": "processing", "progress": 5, "message": "جاري التحضير...", "created": time.time()})

        active_path = orig_path
        if url and not orig_path:
            active_path = str(AUDIO_DIR/f"raw_{job_id}.wav")
            subprocess.run(['yt-dlp','-x','--audio-format','wav','-o',active_path,url], check=True)

        total_len_ms = get_dur_ms(active_path)
        
        wav_ref = prepare_speaker_wav(str(SPEAKER_DIR/f"{speaker_id}.wav")) if speaker_id else extract_speaker_reference(active_path, job_id)
        if not wav_ref: raise ValueError("تعذّر تحديد صوت مرجعي")

        xtts = get_xtts()
        chunk_files = []
        current_pos = 0
        context_window = []
        
        for i, seg in enumerate(segments):
            s_ms = int(seg['start']*1000); e_ms = int(seg['end']*1000)
            gap = s_ms - current_pos
            
            if gap > 200:
                sil_file = AUDIO_DIR / f"gap_{job_id}_{i}.wav"
                subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', f'anoisesrc=d={gap/1000}:c=white:amp=0.0005', '-y', str(sil_file)], capture_output=True)
                chunk_files.append(sil_file)
                current_pos += gap
            
            target_ms = max(0, e_ms - s_ms)
            txt = seg.get('text','').strip()
            
            if txt and target_ms > 0:
                full_context = " ".join(context_window[-2:] + [txt])
                try:
                    translated_full = GoogleTranslator(source=source_lang, target=target_lang).translate(full_context)
                    translated = translated_full.split()[-len(txt.split()):] 
                    translated = " ".join(translated) if translated else translated_full
                except: translated = txt
                context_window.append(txt)

                tmp_p = AUDIO_DIR/f"t_{uuid.uuid4().hex[:6]}.wav"
                
                if tts_generate(xtts, translated, wav_ref, target_lang, tmp_p):
                    actual_gen_ms = get_dur_ms(str(tmp_p))
                    if actual_gen_ms / target_ms > 1.3:
                        tmp_p.unlink(missing_ok=True)
                        tts_generate(xtts, translated, wav_ref, target_lang, tmp_p, speed=1.35)
                    
                    fitted_path = process_audio_segment(tmp_p, target_ms)
                    chunk_files.append(Path(fitted_path))
                    current_pos += get_dur_ms(str(fitted_path))
                    if tmp_p.exists() and str(tmp_p) != str(fitted_path): tmp_p.unlink(missing_ok=True)
                else:
                    err_file = AUDIO_DIR/f"err_{job_id}_{i}.wav"
                    AudioSegment.silent(duration=target_ms).set_frame_rate(44100).set_channels(2).export(str(err_file), format="wav")
                    chunk_files.append(err_file)
                    current_pos += target_ms
            
            JOBS[job_id].update({"progress": int(10+(i/len(segments))*80), "message": f"دبلجة {i+1}/{len(segments)}"})

        if current_pos < total_len_ms:
            tail_f = AUDIO_DIR/f"tail_{job_id}.wav"
            AudioSegment.silent(duration=total_len_ms - current_pos).set_frame_rate(44100).set_channels(2).export(str(tail_f), format="wav")
            chunk_files.append(tail_f)

        list_txt = AUDIO_DIR/f"list_{job_id}.txt"
        with open(list_txt, "w", encoding="utf-8") as f:
            for cf in chunk_files:
                if Path(cf).exists(): f.write(f"file '{Path(cf).resolve().as_posix()}'\n")

        concat_out = AUDIO_DIR/f"concat_{job_id}.wav"
        out_name = f"dub_{job_id}.wav"
        out_path = AUDIO_DIR/out_name

        subprocess.run(['ffmpeg','-f','concat','-safe','0','-i',str(list_txt),'-c','copy','-y',str(concat_out)], capture_output=True, check=True)
        subprocess.run(['ffmpeg','-i',str(concat_out),'-af','compand=0.3|0.3:1|1:-90/-60|-60/-40|-40/-30|-20/-20:6:0:-90:0.2','-ar','44100','-ac','2','-y',str(out_path)], capture_output=True, check=True)
        
        JOBS[job_id].update({"message": "جاري الرفع إلى السحابة..."})
        r2_url = upload_to_r2(out_path, f"dubs/{out_name}")
        final_audio_url = r2_url if r2_url else f"/api/file/{out_name}"

        # التنظيف
        for cf in chunk_files:
            if Path(cf).exists(): Path(cf).unlink(missing_ok=True)
        for f in [list_txt, concat_out]:
            if f.exists(): f.unlink(missing_ok=True)
        if r2_url and out_path.exists():
            out_path.unlink(missing_ok=True)

        JOBS[job_id].update({"status": "done", "progress": 100, "audio_url": final_audio_url, "message": "اكتملت الدبلجة!"})

    except Exception as e:
        logger.error(f"[{job_id}] ❌ {e}")
        JOBS[job_id].update({"status": "error", "error": str(e)})

# ── API Routes ──

# ==========================================
# ── مسار تسجيل/مزامنة المستخدم الجديد ──
# ==========================================
@app.route('/api/sync-user', methods=['POST', 'OPTIONS'])
def sync_user():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
        
    data = request.json
    user_email = data.get('email')
    user_name = data.get('name', 'User')
    
    # البحث هل المستخدم موجود مسبقاً في PostgreSQL
    user = User.query.filter_by(email=user_email).first()
    
    if not user:
        # إذا كان مستخدماً جديداً، ننشئ له حساباً ونعطيه 10,000 حرف مجاني كتجربة!
        new_user = User(email=user_email, name=user_name, credits=10000)
        db.session.add(new_user)
        db.session.commit()
        return jsonify({"message": "تم إنشاء حساب جديد", "credits": 10000}), 201
        
    # إذا كان موجوداً، نرسل رصيده الحالي
    return jsonify({"message": "أهلاً بعودتك", "credits": user.credits}), 200
# ==========================================

@app.route('/api/upload_audio', methods=['POST'])
def upload_audio():
    f = request.files['file']; fp = AUDIO_DIR/f"{uuid.uuid4().hex}_{secure_filename(f.filename)}"
    f.save(str(fp)); return jsonify({"orig_audio_path": str(fp)})

@app.route('/api/upload_speaker', methods=['POST'])
def upload_speaker():
    if 'file' not in request.files: return jsonify({"error": "لم يُرسَل ملف"}), 400
    f = request.files['file']; label = request.form.get('label', f.filename).strip()[:40]
    sid = uuid.uuid4().hex[:10]; raw = SPEAKER_DIR / f"{sid}_raw{Path(f.filename).suffix}"; out = SPEAKER_DIR / f"{sid}.wav"
    f.save(str(raw))
    try:
        subprocess.run(['ffmpeg','-y','-i',str(raw),'-ac','1','-ar','22050','-af','loudnorm,highpass=f=80','-t','12', str(out)], capture_output=True)
        if out.exists(): raw.unlink(missing_ok=True)
        else: raw.rename(out)
        SPEAKERS_META[sid] = label
        return jsonify({"speaker_id": sid, "label": label})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/speakers')
def list_speakers():
    out = []
    for f in sorted(SPEAKER_DIR.glob("*.wav")):
        if any(x in f.stem for x in ('_raw', '_clean')): continue
        sid = f.stem
        out.append({"speaker_id": sid, "label": SPEAKERS_META.get(sid, sid), "is_default": any(d["speaker_id"] == sid for d in DEFAULT_SPEAKERS)})
    return jsonify(out)

@app.route('/api/dub', methods=['POST'])
def dub():
    if not XTTS_READY: return jsonify({"error": "المحرك قيد التحميل"}), 503
    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "starting", "created": time.time()}
    threading.Thread(target=run_dub_job, args=(job_id, request.json), daemon=True).start()
    return jsonify({"job_id": job_id})

# 🚀 مسار TTS المباشر
@app.route('/api/tts', methods=['POST'])
def tts_api():
    if not XTTS_READY: return jsonify({"error": "المحرك قيد التحميل"}), 503
    data = request.json
    text = data.get('text', '').strip()
    lang = data.get('lang', 'en')
    speaker_id = data.get('speaker_id', 'muhammad')
    speed = float(data.get('speed', 1.0))

    if not text: return jsonify({"error": "النص مطلوب"}), 400

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "starting", "progress": 0, "message": "بدء التوليد...", "created": time.time()}

    def run_tts_job(jid, txt, lng, spk, spd):
        try:
            JOBS[jid].update({"status": "processing", "progress": 10, "message": "تحضير بصمة الصوت..."})
            wav_ref = prepare_speaker_wav(str(SPEAKER_DIR / f"{spk}.wav"))
            if not wav_ref: raise ValueError("لم يتم العثور على صوت المتحدث")

            xtts = get_xtts()
            out_name = f"tts_{jid}.wav"
            out_path = AUDIO_DIR / out_name

            JOBS[jid].update({"progress": 40, "message": "جاري توليد الصوت..."})
            
            parts = []
            chunks = split_long_text(sanitize_text(txt, lng) or txt)
            
            for i, chunk in enumerate(chunks):
                if not chunk.strip(): continue
                tmp_p = AUDIO_DIR / f"ck_{jid}_{i}.wav"
                if tts_generate(xtts, chunk, wav_ref, lng, tmp_p, speed=spd):
                    parts.append(AudioSegment.from_file(str(tmp_p)))
                    tmp_p.unlink(missing_ok=True)
            
            if not parts: raise ValueError("فشل التوليد، النص غير صالح")
            
            JOBS[jid].update({"progress": 80, "message": "المعالجة النهائية..."})
            sum(parts, AudioSegment.empty()).export(str(out_path), format="wav")
            
            final_path = process_audio_segment(out_path, 0)
            
            r2_url = upload_to_r2(final_path, f"tts/{Path(final_path).name}")
            final_audio_url = r2_url if r2_url else f"/api/file/{Path(final_path).name}"

            if r2_url and Path(final_path).exists(): Path(final_path).unlink(missing_ok=True)
            if out_path.exists() and str(out_path) != str(final_path): out_path.unlink(missing_ok=True)

            JOBS[jid].update({"status": "done", "progress": 100, "audio_url": final_audio_url, "message": "اكتمل التوليد!"})
        except Exception as e:
            logger.error(f"[{jid}] TTS Error: {e}")
            JOBS[jid].update({"status": "error", "error": str(e)})

    threading.Thread(target=run_tts_job, args=(job_id, text, lang, speaker_id, speed), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route('/api/progress/<job_id>')
def progress(job_id):
    def stream():
        while job_id in JOBS:
            job = JOBS[job_id]
            yield f"data: {json.dumps(job)}\n\n"
            if job.get('status') in ('done', 'error'): break
            time.sleep(0.5)
    return Response(stream(), mimetype='text/event-stream')

@app.route('/api/status')
def api_status(): return jsonify({"xtts_ready": XTTS_READY})

@app.route('/api/job/<job_id>')
def get_job_status(job_id): 
    if job_id in JOBS: return jsonify(JOBS[job_id])
    return jsonify({"status": "waiting", "progress": 0, "message": "جاري تهيئة المهمة..."})

@app.route('/api/file/<path:name>')
def get_file(name): return send_from_directory(str(AUDIO_DIR), name)

@app.route('/')
def index(): return send_from_directory(str(BASE_DIR), 'dubbing.html')

@app.route('/<path:filename>')
def serve_static(filename): return send_from_directory(str(BASE_DIR), filename)

def _download_default_speakers():
    for spk in DEFAULT_SPEAKERS:
        sid, url, raw = spk['speaker_id'], spk['url'], SPEAKER_DIR / f"{spk['speaker_id']}_raw.wav"
        out = SPEAKER_DIR / f"{sid}.wav"
        if out.exists(): continue
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            import shutil
            with urllib.request.urlopen(req, timeout=60) as resp, open(raw, 'wb') as out_f: shutil.copyfileobj(resp, out_f)
            if not prepare_speaker_wav(str(raw)) == str(out): raw.rename(out)
            else: raw.unlink(missing_ok=True)
        except: pass
threading.Thread(target=_download_default_speakers, daemon=True).start()

if __name__ == '__main__':
    # ==========================================
    # تأكد من إنشاء الجداول في قاعدة البيانات عند التشغيل
    with app.app_context():
        db.create_all()
    # ==========================================
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
