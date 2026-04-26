# asr_worker.py
import os, json, time, redis
from faster_whisper import WhisperModel

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
r = redis.from_url(REDIS_URL)

# load a small model for local testing
model = WhisperModel("small", device="cpu", compute_type="int8")

def process_job(msg):
    data = json.loads(msg)
    job_id = data["job_id"]
    file_path = data["file_path"]
    r.hset(f"job:{job_id}", mapping={"status":"asr_running"})
    segments, info = model.transcribe(file_path, beam_size=1)
    segs = []
    for i, s in enumerate(segments):
        segs.append({"idx": i, "start": s.start, "end": s.end, "text": s.text})
    for seg in segs:
        tmsg = json.dumps({"job_id": job_id, "segment": seg, "file_path": file_path})
        r.lpush("queue:tts", tmsg)
    r.hset(f"job:{job_id}", mapping={"status":"asr_done", "language": getattr(info, "language", "")})

def run():
    print("ASR worker started")
    while True:
        item = r.brpop("queue:asr", timeout=5)
        if item:
            msg = item[1].decode()
            try:
                process_job(msg)
            except Exception as e:
                print("ASR error", e)
        else:
            time.sleep(0.5)

if __name__ == "__main__":
    run()
