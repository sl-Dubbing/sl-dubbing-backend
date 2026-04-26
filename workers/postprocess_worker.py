# postprocess_worker.py
import os, json, time, redis, shutil
from pydub import AudioSegment

r = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

def process_post(msg):
    data = json.loads(msg)
    job_id = data["job_id"]
    clone_wav = data["clone_wav"]
    final_path = clone_wav.replace(".clone.wav", ".final.wav")
    shutil.copy(clone_wav, final_path)
    r.hset(f"job:{job_id}", mapping={"status":"completed", "audio_url": final_path})

def run():
    print("Postprocess worker started")
    while True:
        item = r.brpop("queue:postprocess", timeout=5)
        if item:
            msg = item[1].decode()
            try:
                process_post(msg)
            except Exception as e:
                print("Postprocess error", e)
        else:
            time.sleep(0.5)

if __name__ == "__main__":
    run()
