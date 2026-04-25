# tts_factory.py — V1.2 (Fixed: image binding + torch compatibility + error handling)
import modal
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import os, tempfile, json, uuid, logging, subprocess, shutil

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sl-tts")

image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("ffmpeg", "libsndfile1")
    .pip_install(
        "torch==2.1.2",
        "torchaudio==2.1.2",
        "transformers==4.33.0",
        "TTS==0.22.0",
        "deep-translator",
        "pydub",
        "google-cloud-storage",
        "requests",
        "fastapi",
        "pydantic>=2.0,<3.0",
        "numpy<2.0",
    )
    .env({"COQUI_TOS_AGREED": "1"})
)

app = modal.App("sl-tts-factory")


@app.cls(
    image=image,
    gpu="T4",
    timeout=600,
    secrets=[modal.Secret.from_name("Key")],
    scaledown_window=120,
)
class TTSService:
    @modal.enter()
    def load_models(self):
        from TTS.api import TTS
        self.xtts = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=True)
        logger.info("✅ TTS Model Loaded")

    def _upload_to_gcs(self, local_path):
        from google.cloud import storage
        client = storage.Client.from_service_account_info(
            json.loads(os.environ.get("GCP_CREDENTIALS"))
        )
        blob = client.bucket("dubbing-bucket-sl").blob(
            f"results/tts_{uuid.uuid4().hex}.wav"
        )
        blob.upload_from_filename(local_path)
        blob.make_public()
        return blob.public_url

    def _prepare_speaker(self, voice_id: str, temp_dir: str):
        import requests

        # محاولة جلب الصوت المحدد
        if voice_id and voice_id not in ("source", "original", ""):
            for ext in ("wav", "mp3", "m4a"):
                try:
                    url = f"https://res.cloudinary.com/dxbmvzsiz/video/upload/sl_voice/{voice_id}.{ext}"
                    r = requests.get(url, timeout=15)
                    if r.status_code == 200:
                        cloud_path = os.path.join(temp_dir, f"cloud_speaker.{ext}")
                        with open(cloud_path, "wb") as f:
                            f.write(r.content)
                        wav_cloud_path = os.path.join(temp_dir, "cloud_speaker_fmt.wav")
                        result = subprocess.run(
                            ["ffmpeg", "-y", "-i", cloud_path,
                             "-ar", "22050", "-ac", "1", wav_cloud_path],
                            capture_output=True, text=True,
                        )
                        if result.returncode == 0 and os.path.exists(wav_cloud_path):
                            return wav_cloud_path
                        logger.error(f"ffmpeg failed: {result.stderr[:200]}")
                except Exception as e:
                    logger.error(f"Cloudinary fetch failed for .{ext}: {e}")
                    continue

        # Fallback
        for ext in ("wav", "mp3"):
            try:
                fallback_url = f"https://res.cloudinary.com/dxbmvzsiz/video/upload/sl_voice/muhammad_ar.{ext}"
                r = requests.get(fallback_url, timeout=15)
                if r.status_code != 200:
                    continue
                fallback_path = os.path.join(temp_dir, f"fallback.{ext}")
                fallback_wav = os.path.join(temp_dir, "fallback.wav")
                with open(fallback_path, "wb") as f:
                    f.write(r.content)
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", fallback_path,
                     "-ar", "22050", "-ac", "1", fallback_wav],
                    capture_output=True, text=True,
                )
                if result.returncode == 0 and os.path.exists(fallback_wav):
                    return fallback_wav
            except Exception as e:
                logger.error(f"Fallback {ext} failed: {e}")
                continue

        return None

    @modal.asgi_app()
    def fastapi_app(self):
        web_app = FastAPI()

        @web_app.post("/tts")
        async def process_tts(request: Request):
            from deep_translator import GoogleTranslator

            try:
                data = await request.json()
            except Exception as e:
                return JSONResponse(
                    {"success": False, "error": f"Invalid JSON: {e}"},
                    status_code=400,
                )

            text = (data.get("text") or "").strip()
            lang = data.get("lang", "en")
            voice_id = data.get("voice_id", "")

            if not text:
                return JSONResponse(
                    {"success": False, "error": "No text provided"},
                    status_code=400,
                )

            try:
                translator = GoogleTranslator(source="auto", target=lang)
                translated_text = translator.translate(text) or text
            except Exception as e:
                logger.error(f"Translation failed: {e}")
                translated_text = text

            temp_dir = tempfile.mkdtemp()
            try:
                out_path = os.path.join(temp_dir, "output.wav")
                speaker_wav = self._prepare_speaker(voice_id, temp_dir)

                if not speaker_wav or not os.path.exists(speaker_wav):
                    return JSONResponse(
                        {"success": False,
                         "error": "Could not prepare speaker reference audio"},
                        status_code=500,
                    )

                self.xtts.tts_to_file(
                    text=translated_text,
                    file_path=out_path,
                    speaker_wav=speaker_wav,
                    language=lang,
                )

                if not os.path.exists(out_path):
                    return JSONResponse(
                        {"success": False, "error": "TTS produced no output"},
                        status_code=500,
                    )

                url = self._upload_to_gcs(out_path)
                return {
                    "success": True,
                    "audio_url": url,
                    "final_text": translated_text,
                }

            except Exception as e:
                logger.error(f"TTS error: {e}")
                return JSONResponse(
                    {"success": False, "error": str(e)},
                    status_code=500,
                )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        @web_app.get("/health")
        async def health():
            return {"status": "ok"}

        return web_app
