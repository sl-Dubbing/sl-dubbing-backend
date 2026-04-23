# tts_factory.py — V1.1 (Dedicated TTS Service + Cloudinary Integration)
import modal
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import os, tempfile, json, uuid, logging, subprocess, requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sl-tts")

image = (
    modal.Image.debian_slim(python_version="3.10") 
    .apt_install("ffmpeg", "libsndfile1") # ⚡ إضافة ffmpeg ضرورية جداً هنا أيضاً
    .pip_install(
        "torch==2.4.1", "transformers==4.33.0", "TTS==0.22.0", 
        "deep-translator", "pydub", "google-cloud-storage", "requests"
    )
    .env({"COQUI_TOS_AGREED": "1"})
)

app = modal.App("sl-tts-factory")

@app.cls(gpu="T4", timeout=600, secrets=[modal.Secret.from_name("Key")], scaledown_window=120)
class TTSService:
    @modal.enter()
    def load_models(self):
        from TTS.api import TTS
        self.xtts = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=True)
        logger.info("✅ TTS Model Loaded")

    def _upload_to_gcs(self, local_path):
        from google.cloud import storage
        client = storage.Client.from_service_account_info(json.loads(os.environ.get("GCP_CREDENTIALS")))
        blob = client.bucket("dubbing-bucket-sl").blob(f"results/tts_{uuid.uuid4().hex}.wav")
        blob.upload_from_filename(local_path)
        blob.make_public()
        return blob.public_url

    # 🟢 دالة جلب الصوت من Cloudinary
    def _prepare_speaker(self, voice_id: str, temp_dir: str):
        if voice_id and voice_id != "source":
            try:
                # ⚡ جلب الصوت من Cloudinary
                url = f"https://res.cloudinary.com/dxbmvzsiz/video/upload/sl_voices/{voice_id}.mp3"
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    cloud_path = os.path.join(temp_dir, "cloud_speaker.mp3")
                    with open(cloud_path, "wb") as f:
                        f.write(r.content)
                    
                    wav_cloud_path = os.path.join(temp_dir, "cloud_speaker_fmt.wav")
                    subprocess.run(["ffmpeg", "-y", "-i", cloud_path, "-ar", "22050", "-ac", "1", wav_cloud_path], capture_output=True)
                    return wav_cloud_path
            except Exception as e:
                logger.error(f"Cloudinary fetch failed: {e}")
        
        # ⚡ XTTS يشترط وجود صوت، لذلك وضعنا صوت 'محمد' كافتراضي في حال لم يختر المستخدم
        fallback_url = "https://res.cloudinary.com/dxbmvzsiz/video/upload/sl_voices/muhammad.mp3"
        fallback_path = os.path.join(temp_dir, "fallback.mp3")
        fallback_wav = os.path.join(temp_dir, "fallback.wav")
        try:
            r = requests.get(fallback_url)
            with open(fallback_path, "wb") as f: f.write(r.content)
            subprocess.run(["ffmpeg", "-y", "-i", fallback_path, "-ar", "22050", "-ac", "1", fallback_wav], capture_output=True)
            return fallback_wav
        except: 
            return None

    @modal.asgi_app()
    def fastapi_app(self):
        web_app = FastAPI()
        
        @web_app.post("/tts")
        async def process_tts(request: Request):
            from deep_translator import GoogleTranslator
            data = await request.json()
            text = data.get("text", "")
            lang = data.get("lang", "en")
            voice_id = data.get("voice_id", "") # ⚡ استقبال الـ ID من الواجهة
            
            if not text:
                return JSONResponse({"success": False, "error": "No text provided"}, status_code=400)

            translator = GoogleTranslator(source="auto", target=lang)
            translated_text = translator.translate(text)
            
            temp_dir = tempfile.mkdtemp()
            out_path = os.path.join(temp_dir, "output.wav")
            
            # ⚡ جلب الصوت (المعلق) وتطبيقه
            speaker_wav = self._prepare_speaker(voice_id, temp_dir)
            
            try:
                if speaker_wav and os.path.exists(speaker_wav):
                    self.xtts.tts_to_file(text=translated_text, file_path=out_path, speaker_wav=speaker_wav, language=lang)
                else:
                    self.xtts.tts_to_file(text=translated_text, file_path=out_path, language=lang)
                
                url = self._upload_to_gcs(out_path)
            except Exception as e:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
                return JSONResponse({"success": False, "error": str(e)}, status_code=500)
            
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {"success": True, "audio_url": url, "final_text": translated_text}
            
        return web_app
