# dubbing_factory.py — V14.2 (Fixed: torch compatibility + translated_text + error handling)
import modal
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
import os, tempfile, subprocess, shutil, json, uuid, logging, base64, traceback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sl-dubbing")

# ✅ إصلاح: torch 2.1.2 متوافق مع TTS 0.22.0 (الـ 2.4.x يكسر XTTS)
# ✅ إصلاح: numpy<2 ضروري لأن TTS 0.22.0 لا يدعم numpy 2.x
image = (
    modal.Image.debian_slim(python_version="3.10")
    .apt_install("ffmpeg", "libasound2", "libsndfile1")
    .pip_install(
        "torch==2.1.2",
        "torchaudio==2.1.2",
        "transformers==4.33.0",
        "faster-whisper==1.0.3",
        "fastapi",
        "uvicorn",
        "aiofiles",
        "TTS==0.22.0",
        "deep-translator",
        "pydub",
        "google-cloud-storage",
        "python-multipart",
        "requests",
        "pydantic>=2.0,<3.0",
        "numpy<2.0",
    )
    .env({"COQUI_TOS_AGREED": "1"})
)

app = modal.App("sl-dubbing-factory")


@app.cls(
    image=image,
    gpu="A10G",
    timeout=1800,
    secrets=[modal.Secret.from_name("Key")],
    scaledown_window=300,
)
class DubbingService:
    @modal.enter()
    def load_models(self):
        from faster_whisper import WhisperModel
        from TTS.api import TTS
        # ✅ "large-v3-turbo" يتطلب faster-whisper >= 1.0.3
        self.whisper = WhisperModel("large-v3", device="cuda", compute_type="float16")
        self.xtts = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=True)
        logger.info("✅ Dubbing Models Loaded")

    def _upload_to_gcs(self, local_path):
        from google.cloud import storage
        client = storage.Client.from_service_account_info(
            json.loads(os.environ.get("GCP_CREDENTIALS"))
        )
        blob = client.bucket("dubbing-bucket-sl").blob(
            f"results/dub_{uuid.uuid4().hex}.wav"
        )
        blob.upload_from_filename(local_path)
        blob.make_public()
        return blob.public_url

    def _prepare_speaker(self, voice_id: str, sample_b64: str, temp_dir: str, fallback_wav: str):
        """تجهيز الصوت المرجعي: بصمة مخصصة → Cloudinary → الصوت الأصلي."""
        import requests

        # 1) بصمة صوتية مرفوعة (Voice Cloning)
        if sample_b64:
            try:
                custom_raw = os.path.join(temp_dir, "custom_raw")
                with open(custom_raw, "wb") as f:
                    f.write(base64.b64decode(sample_b64))
                # تحويل إلى wav قياسي
                custom_path = os.path.join(temp_dir, "custom_speaker.wav")
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", custom_raw,
                     "-ar", "22050", "-ac", "1", custom_path],
                    capture_output=True, text=True,
                )
                if result.returncode == 0 and os.path.exists(custom_path):
                    return custom_path
                logger.error(f"Custom speaker ffmpeg failed: {result.stderr[:200]}")
            except Exception as e:
                logger.error(f"Custom speaker preparation failed: {e}")

        # 2) صوت من Cloudinary
        if voice_id and voice_id not in ("source", "original", ""):
            try:
                url = f"https://res.cloudinary.com/dxbmvzsiz/video/upload/sl_voices/{voice_id}.mp3"
                r = requests.get(url, timeout=15)
                if r.status_code == 200:
                    cloud_path = os.path.join(temp_dir, "cloud_speaker.mp3")
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
                    logger.error(f"Cloudinary ffmpeg failed: {result.stderr[:200]}")
                else:
                    logger.warning(f"Cloudinary returned HTTP {r.status_code} for {voice_id}")
            except Exception as e:
                logger.error(f"Cloudinary fetch failed: {e}")

        # 3) الصوت الأصلي للمقطع كـ fallback
        return fallback_wav

    @modal.asgi_app()
    def fastapi_app(self):
        web_app = FastAPI()

        @web_app.post("/upload")
        async def upload(
            media_file: UploadFile = File(...),
            lang: str = Form("en"),
            voice_id: str = Form("source"),
            sample_b64: str = Form(""),
        ):
            from pydub import AudioSegment
            from deep_translator import GoogleTranslator

            temp_dir = tempfile.mkdtemp()
            try:
                # [1] استخراج الصوت من الملف المُرسل
                in_path = os.path.join(temp_dir, "input")
                with open(in_path, "wb") as f:
                    f.write(await media_file.read())

                wav_path = os.path.join(temp_dir, "src.wav")
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", in_path, "-vn",
                     "-ar", "22050", "-ac", "1", wav_path],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"ffmpeg failed: {result.stderr[:300]}")

                # [2] تجهيز الصوت المرجعي
                speaker_wav = self._prepare_speaker(voice_id, sample_b64, temp_dir, wav_path)

                # [3] التفريغ النصي
                segments, info = self.whisper.transcribe(wav_path, beam_size=1)
                segments_list = list(segments)
                logger.info(f"✅ Transcribed {len(segments_list)} segments, lang={info.language}")

                if not segments_list:
                    return JSONResponse(
                        {"success": False, "error": "No speech detected"},
                        status_code=400,
                    )

                # [4] الترجمة + توليد المقاطع
                translator = GoogleTranslator(source="auto", target=lang)
                final_audio = AudioSegment.silent(duration=0)
                max_ms = 0
                generated_segments = []
                translated_parts = []

                for seg in segments_list:
                    src_txt = (seg.text or "").strip()
                    if not src_txt:
                        continue
                    try:
                        txt = translator.translate(src_txt) or src_txt
                    except Exception as e:
                        logger.warning(f"Translate failed for '{src_txt[:40]}': {e}")
                        txt = src_txt

                    translated_parts.append(txt)
                    out_seg = os.path.join(temp_dir, f"seg_{int(seg.start * 1000)}.wav")

                    try:
                        self.xtts.tts_to_file(
                            text=txt,
                            file_path=out_seg,
                            speaker_wav=speaker_wav,
                            language=lang,
                        )
                        s_aud = AudioSegment.from_wav(out_seg)
                        start_ms = int(seg.start * 1000)
                        generated_segments.append((start_ms, s_aud))
                        seg_end = start_ms + len(s_aud)
                        if seg_end > max_ms:
                            max_ms = seg_end
                    except Exception as e:
                        logger.warning(f"TTS failed for segment at {seg.start}: {e}")

                if not generated_segments:
                    return JSONResponse(
                        {"success": False, "error": "All segments failed to generate"},
                        status_code=500,
                    )

                # [5] بناء الصوت النهائي بطول صحيح
                final_audio = AudioSegment.silent(duration=max_ms + 500)
                for start_ms, seg_audio in generated_segments:
                    final_audio = final_audio.overlay(seg_audio, position=start_ms)

                # [6] التصدير والرفع
                res_path = os.path.join(temp_dir, "res.wav")
                final_audio.export(res_path, format="wav")
                url = self._upload_to_gcs(res_path)

                return {
                    "success": True,
                    "audio_url": url,
                    "translated_text": " ".join(translated_parts),
                    "detected_language": info.language,
                    "segments_count": len(generated_segments),
                }

            except Exception as e:
                err_trace = traceback.format_exc()
                logger.error(f"❌ Dubbing failed: {err_trace}")
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
