# dubbing_factory.py — V14.1 (Dedicated Dubbing + Cloudinary Integration)
import modal
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import os, tempfile, subprocess, shutil, json, requests, uuid, logging
import base64

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sl-dubbing")

image = (
    modal.Image.debian_slim(python_version="3.10") 
    .apt_install("ffmpeg", "libasound2", "libsndfile1")
    .pip_install(
        "torch==2.4.1", "transformers==4.33.0", "faster-whisper", 
        "fastapi", "uvicorn", "aiofiles", "TTS==0.22.0", 
        "deep-translator", "pydub", "google-cloud-storage", "python-multipart", "requests"
    )
    .env({"COQUI_TOS_AGREED": "1"})
)

app = modal.App("sl-dubbing-factory")

@app.cls(gpu="A10G", timeout=1800, secrets=[modal.Secret.from_name("Key")], scaledown_window=300)
class DubbingService:
    @modal.enter()
    def load_models(self):
        from faster_whisper import WhisperModel
        from TTS.api import TTS
        self.whisper = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
        self.xtts = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=True)
        logger.info("✅ Dubbing Models Loaded")

    def _upload_to_gcs(self, local_path):
        from google.cloud import storage
        client = storage.Client.from_service_account_info(json.loads(os.environ.get("GCP_CREDENTIALS")))
        blob = client.bucket("dubbing-bucket-sl").blob(f"results/dub_{uuid.uuid4().hex}.wav")
        blob.upload_from_filename(local_path)
        blob.make_public()
        return blob.public_url

    # 🟢 دالة جديدة مخصصة لجلب الصوت من Cloudinary أو البصمة المخصصة
    def _prepare_speaker(self, voice_id: str, sample_b64: str, temp_dir: str, fallback_wav: str):
        # 1. إذا رفع المستخدم بصمة صوتية (Voice Cloning)
        if sample_b64:
            custom_path = os.path.join(temp_dir, "custom_speaker.wav")
            with open(custom_path, "wb") as f:
                f.write(base64.b64decode(sample_b64))
            return custom_path
            
        # 2. إذا اختار المستخدم صوتاً من Cloudinary
        if voice_id and voice_id != "source":
            try:
                # ⚡ الرابط المباشر لمجلد sl_voices في Cloudinary
                url = f"https://res.cloudinary.com/dxbmvzsiz/video/upload/sl_voices/{voice_id}.mp3"
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    cloud_path = os.path.join(temp_dir, "cloud_speaker.mp3")
                    with open(cloud_path, "wb") as f:
                        f.write(r.content)
                    
                    # تحويله إلى wav لكي يقبله XTTS بسهولة
                    wav_cloud_path = os.path.join(temp_dir, "cloud_speaker_fmt.wav")
                    subprocess.run(["ffmpeg", "-y", "-i", cloud_path, "-ar", "22050", "-ac", "1", wav_cloud_path], capture_output=True)
                    return wav_cloud_path
            except Exception as e:
                logger.error(f"Cloudinary fetch failed: {e}")
                
        # 3. إذا لم يختر شيئاً، يتم استخدام الصوت الأصلي للمقطع
        return fallback_wav

    @modal.asgi_app()
    def fastapi_app(self):
        web_app = FastAPI()
        
        @web_app.post("/upload")
        async def upload(
            media_file: UploadFile = File(...), 
            lang: str = Form("en"), 
            voice_id: str = Form("source"), 
            sample_b64: str = Form("") # ⚡ تمت إضافة هذا المتغير لاستقبال البصمة المخصصة
        ):
            from pydub import AudioSegment
            from deep_translator import GoogleTranslator
            
            temp_dir = tempfile.mkdtemp()
            in_path = os.path.join(temp_dir, "input")
            with open(in_path, "wb") as f: f.write(await media_file.read())
            
            wav_path = os.path.join(temp_dir, "src.wav")
            subprocess.run(["ffmpeg", "-y", "-i", in_path, "-vn", "-ar", "22050", "-ac", "1", wav_path])
            
            # ⚡ تجهيز الصوت قبل بدء الدبلجة
            speaker_wav = self._prepare_speaker(voice_id, sample_b64, temp_dir, wav_path)
            
            segments, _ = self.whisper.transcribe(wav_path, beam_size=1)
            translator = GoogleTranslator(source="auto", target=lang)
            
            final_audio = AudioSegment.silent(duration=5000)
            max_ms = 0
            
            for seg in segments:
                txt = translator.translate(seg.text)
                out_seg = os.path.join(temp_dir, f"seg_{int(seg.start)}.wav")
                
                try:
                    # ⚡ تمرير speaker_wav للمحرك لكي يقلد الصوت
                    self.xtts.tts_to_file(text=txt, file_path=out_seg, speaker_wav=speaker_wav, language=lang)
                    s_aud = AudioSegment.from_wav(out_seg)
                    final_audio = final_audio.overlay(s_aud, position=int(seg.start * 1000))
                    max_ms = max(max_ms, int(seg.start * 1000) + len(s_aud))
                except Exception as e:
                    logger.warning(f"Failed to generate segment: {e}")
            
            res_path = os.path.join(temp_dir, "res.wav")
            final_audio[:max_ms+500].export(res_path, format="wav")
            url = self._upload_to_gcs(res_path)
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {"success": True, "audio_url": url}
            
        return web_app
