# tts_worker.py
import os, json, time, redis, hashlib, tempfile, shutil
from pydub import AudioSegment
import asyncio
from edge_tts import Communicate

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
r = redis.from_url(REDIS_URL)
CACHE_DIR = "/tmp/sl_dubbing_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

def sha1_key(voice, text, ref_hash=""):
    return hashlib.sha1((voice + "|" + text + "|" + ref_hash).encode()).hexdigest()

async def generate_tts_async(text, voice, out_mp3):
    await Communicate(text, voice).save(out_mp3)

def generate_tts(text, voice, out_mp3):
    asyncio.run(generate_tts_async(text, voice, out_mp3))

def process_tts(msg):
    data = json.loads(msg)
    job_id = data["job_id"]
    seg = data["segment"]
    voice = data.get("voice", "en-US-AriaNeural")
    key = sha1_key(voice, seg["text"])
    cache_mp3 = os.path.join(CACHE_DIR, f"tts_{key}.mp3")
    if os.path.exists(cache_mp3):
        tts_path = cache_mp3
    else:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.close()
        try:
            generate_tts(seg["text"], voice, tmp.name)
            shutil.copy(tmp.name, cache_mp3)
            tts_path = cache_mp3
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
    wav_out = tts_path.replace(".mp3", ".wav")
    AudioSegment.from_file(tts_path).set_frame_rate(24000).set_channels(1).export(wav_out, format="wav")
    r.lpush("queue:clone", json.dumps({"job_id": job_id, "segment": seg, "tts_wav": wav_out}))

def run():
    print("TTS worker started")
    while True:
        item = r.brpop("queue:tts", timeout=5)
        if item:
            msg = item[1].decode()
            try:
                process_tts(msg)
            except Exception as e:
                print("TTS error", e)
        else:
            time.sleep(0.5)

if __name__ == "__main__":
    run()
