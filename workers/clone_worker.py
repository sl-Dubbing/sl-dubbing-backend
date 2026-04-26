# clone_worker.py
import os, json, time, redis, shutil
r = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))

def process_clone(msg):
    data = json.loads(msg)
    job_id = data["job_id"]
    seg = data["segment"]
    tts_wav = data["tts_wav"]
    clone_path = tts_wav.replace(".wav", ".clone.wav")
    shutil.copy(tts_wav, clone_path)
    r.lpush("queue:postprocess", json.dumps({"job_id": job_id, "segment": seg, "clone_wav": clone_path}))

def run():
    print("Clone worker started (simulated)")
    while True:
        item = r.brpop("queue:clone", timeout=5)
        if item:
            msg = item[1].decode()
            try:
                process_clone(msg)
            except Exception as e:
                print("Clone error", e)
        else:
            time.sleep(0.5)

if __name__ == "__main__":
    run()
