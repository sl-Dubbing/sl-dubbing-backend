# modal_app.py
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import os, tempfile, uuid, base64, time, json
import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
r = redis.from_url(REDIS_URL)

app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok", "engine": "modal-skeleton"}

@app.post("/upload")
async def upload(media_file: UploadFile = File(...), lang: str = Form("en"), voice_id: str = Form("source")):
    temp_dir = tempfile.mkdtemp()
    job_id = uuid.uuid4().hex
    in_path = os.path.join(temp_dir, "input")
    content = await media_file.read()
    with open(in_path, "wb") as f:
        f.write(content)

    # minimal preview placeholder: store job state and push to Redis queue
    r.hset(f"job:{job_id}", mapping={"status":"queued", "created_at": time.time()})
    # push message to queue list for ASR workers
    msg = json.dumps({"job_id": job_id, "file_path": in_path, "lang": lang, "voice_id": voice_id})
    r.lpush("queue:asr", msg)

    # preview_url placeholder (will be updated by workers)
    return JSONResponse({"success": True, "job_id": job_id, "preview_url": None})

@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    data = r.hgetall(f"job:{job_id}")
    if not data:
        return JSONResponse({"status":"not_found"}, status_code=404)
    # decode bytes to strings
    return {k.decode(): v.decode() for k, v in data.items()}
