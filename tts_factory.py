# tts_factory.py — V1.0 (Dedicated TTS Service)
import modal
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import os, tempfile, json, uuid, logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sl-tts")

image = (
    modal.Image.debian_slim(python_version="3.10") 
    .pip_install(
        "torch==2.4.1", "transformers==4.33.0", "TTS==0.22.0", 
        "deep-translator", "pydub", "google-cloud-storage"
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

    @modal.asgi_app()
    def fastapi_app(self):
        web_app = FastAPI()
        @web_app.post("/tts")
        async def process_tts(request: Request):
            from deep_translator import GoogleTranslator
            data = await request.json()
            text, lang = data.get("text", ""), data.get("lang", "en")
            
            translator = GoogleTranslator(source="auto", target=lang)
            translated_text = translator.translate(text)
            
            temp_dir = tempfile.mkdtemp()
            out_path = os.path.join(temp_dir, "output.wav")
            
            self.xtts.tts_to_file(text=translated_text, file_path=out_path, language=lang)
            url = self._upload_to_gcs(out_path)
            
            import shutil
            shutil.rmtree(temp_dir)
            return {"success": True, "audio_url": url, "final_text": translated_text}
        return web_app
