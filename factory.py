# factory.py — النسخة V10.13
# إصلاح نهائي: ترتيب الديكوراتور صحيح (@app.cls ثم @modal.concurrent) مع تمرير الوسيطة المطلوبة max_inputs.
# استيرادات كسولة للـ heavy libs، حماية استيراد الوحدات المحلية، توحيد الحدود، signed URLs.

import modal
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
import os
import tempfile
import subprocess
import shutil
import re
import json
import requests
import uuid
import base64
import gc
import asyncio
import logging
import binascii
import io
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("sl-dubbing")

# ======= حماية استيراد الوحدات المحلية (إن وُجدت) =======
_HAS_MODELS = False
try:
    from models import db, User, DubbingJob  # type: ignore
    _HAS_MODELS = True
except Exception:
    logger.info("Local 'models' module not available at import time; continuing (expected during modal deploy).")

# ======= ثوابت (موحّدة عبر env) =======
MAX_FILE_BYTES   = int(os.environ.get("MAX_UPLOAD_BYTES", 100 * 1024 * 1024))   # 100 MB default
MAX_B64_CHARS    = int(os.environ.get("MAX_B64_CHARS", 280 * 1024 * 1024))
MAX_TEXT_CHARS   = 5000
MAX_TTS_CHARS    = MAX_TEXT_CHARS * 4
FFMPEG_TIMEOUT   = int(os.environ.get("FFMPEG_TIMEOUT", 120))
NETWORK_TIMEOUT  = (5, 20)
STREAM_CHUNK     = 8192
SPEAKER_FETCH_TOTAL_TIMEOUT = float(os.environ.get("SPEAKER_FETCH_TOTAL_TIMEOUT", 10.0))

ALLOWED_MIME_MAGIC = {
    b"\x1aE\xdf\xa3"       : "video/webm",
    b"\x00\x00\x00\x18ftyp": "video/mp4",
    b"\x00\x00\x00\x20ftyp": "video/mp4",
    b"RIFF"                 : "audio/wav",
    b"ID3"                  : "audio/mp3",
    b"\xff\xfb"             : "audio/mp3",
    b"\xff\xf3"             : "audio/mp3",
    b"\xff\xf2"             : "audio/mp3",
    b"OggS"                 : "audio/ogg",
    b"fLaC"                 : "audio/flac",
}

# HTTP session helper
def _build_http_session() -> requests.Session:
    session = requests.Session()
    retry   = Retry(total=3, backoff_factor=0.5, status_forcelist=[429,500,502,503,504], allowed_methods=["GET"], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# Image definition for modal container
image = (
    modal.Image.debian_slim()
    .apt_install("ffmpeg", "libasound2", "libsndfile1")
    .pip_install(
        "torch==2.4.1", "torchaudio==2.4.1",
        "transformers", "optimum", "accelerate",
        "insanely-fast-whisper",
        "fastapi", "uvicorn", "aiofiles",
        "TTS==0.22.0",
        "deep-translator",
        "pydub", "google-cloud-storage", "python-multipart",
        "urllib3"
    )
    .env({"COQUI_TOS_AGREED": "1", "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"})
)

app = modal.App("sl-dubbing-factory")
_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("FACTORY_EXECUTOR_WORKERS", "4")))

XTTS_LANG_MAP = {
    "ar":"ar","en":"en","es":"es","fr":"fr","de":"de","it":"it",
    "pt":"pt","tr":"tr","ru":"ru","nl":"nl","cs":"cs","pl":"pl",
    "hu":"hu","zh":"zh-cn","ja":"ja","ko":"ko","hi":"hi"
}

# ======= Helpers: base64 streaming, validation, subprocess helpers =======
def stream_b64_to_file_sync(b64_str: str, out_path: str) -> int:
    if not isinstance(b64_str, str):
        raise ValueError("base64 input must be a string")
    b64_clean = "".join(b64_str.split())
    if len(b64_clean) > MAX_B64_CHARS:
        raise ValueError("base64 input exceeds allowed length")
    total = 0
    buffer = ""
    with open(out_path, "wb") as out:
        for i in range(0, len(b64_clean), STREAM_CHUNK):
            buffer += b64_clean[i:i+STREAM_CHUNK]
            safe_len = len(buffer) - (len(buffer) % 4)
            if safe_len >= 4:
                to_decode = buffer[:safe_len]
                buffer = buffer[safe_len:]
                try:
                    decoded = binascii.a2b_base64(to_decode)
                except binascii.Error as e:
                    raise ValueError(f"Invalid base64 chunk at offset {i}: {e}")
                out.write(decoded)
                total += len(decoded)
        if buffer:
            pad_len = (-len(buffer)) % 4
            try:
                decoded = binascii.a2b_base64(buffer + ("=" * pad_len))
            except binascii.Error as e:
                raise ValueError(f"Invalid base64 tail: {e}")
            out.write(decoded)
            total += len(decoded)
    return total

async def stream_b64_to_file(b64_str: str, out_path: str) -> int:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, partial(stream_b64_to_file_sync, b64_str, out_path))

def validate_b64_header_length_only(b64_str_len: int) -> None:
    if b64_str_len > MAX_B64_CHARS:
        raise HTTPException(status_code=413, detail=f"Request too large. Maximum is {MAX_FILE_BYTES // (1024*1024)} MB.")

def validate_file_on_disk(path: str) -> None:
    try:
        size = os.path.getsize(path)
    except OSError:
        raise HTTPException(status_code=400, detail="Decoded file not found.")
    if size > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_FILE_BYTES // (1024*1024)} MB limit.")
    if size == 0:
        raise HTTPException(status_code=400, detail="File is empty after decoding.")
    with open(path, "rb") as f:
        header = f.read(16)
    recognized = any(header.startswith(magic) for magic in ALLOWED_MIME_MAGIC)
    if not recognized:
        raise HTTPException(status_code=415, detail="Unsupported file type. Accepted: mp4, webm, wav, mp3, ogg, flac.")

def validate_gcp_credentials() -> dict:
    creds_json = os.environ.get("GCP_CREDENTIALS")
    if not creds_json:
        logger.error("GCP_CREDENTIALS env var is missing.")
        raise HTTPException(status_code=502, detail="Storage not configured.")
    try:
        creds = json.loads(creds_json)
    except json.JSONDecodeError:
        logger.error("GCP_CREDENTIALS contains invalid JSON (content redacted).")
        raise HTTPException(status_code=502, detail="Storage credentials malformed.")
    required = {"type", "project_id", "private_key", "client_email"}
    missing  = required - set(creds.keys())
    if missing:
        logger.error("GCP_CREDENTIALS missing fields: %s", missing)
        raise HTTPException(status_code=502, detail="Storage credentials incomplete.")
    return creds

def get_dynamic_batch_size() -> int:
    try:
        import torch
        if not torch.cuda.is_available():
            logger.warning("CUDA not available, using batch_size=4")
            return 4
        total    = torch.cuda.get_device_properties(0).total_memory
        reserved = torch.cuda.memory_reserved(0)
        allocated = torch.cuda.memory_allocated(0)
        margin_bytes = int(os.environ.get("XTTS_MARGIN_BYTES", 8 * 1024 ** 3))
        free_bytes = max(0, total - max(reserved, allocated) - margin_bytes)
        free_gb = free_bytes / (1024 ** 3)
        if free_gb >= 6:
            return 12
        elif free_gb >= 3:
            return 8
        else:
            return 4
    except Exception as e:
        logger.warning("batch_size fallback due to: %s", e)
        return 4

async def run_cmd_async(cmd: list, timeout: int = FFMPEG_TIMEOUT):
    loop = asyncio.get_event_loop()
    def _run():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            proc.communicate()
            raise RuntimeError(f"Command timed out after {timeout}s: {cmd[0]}")
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors='replace')[:400]
            raise RuntimeError(f"Command failed ({cmd[0]}): {stderr_text}")
        return stdout
    return await loop.run_in_executor(_executor, _run)

async def probe_duration_async(path: str) -> float:
    loop = asyncio.get_event_loop()
    def _probe():
        proc = subprocess.Popen(
            ["ffprobe", "-i", path, "-show_entries", "format=duration",
             "-v", "quiet", "-of", "csv=p=0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            stdout, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            proc.communicate()
            return 0.0
        text = stdout.decode(errors="replace").strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except (ValueError, TypeError):
            return 0.0
    return await loop.run_in_executor(_executor, _probe)

def make_secure_tempdir() -> str:
    try:
        tmp = tempfile.mkdtemp()
        try:
            os.chmod(tmp, stat.S_IRWXU)
        except Exception:
            pass
        return tmp
    except Exception as e:
        logger.error("Failed to create tempdir: %s", e)
        raise HTTPException(status_code=500, detail="Server cannot create temp directory.")

def _check_ffmpeg_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        try:
            proc = subprocess.run([tool, "-version"], capture_output=True, text=True, timeout=5)
            if proc.returncode != 0:
                raise RuntimeError(f"{tool} not available")
        except Exception as e:
            logger.error("%s check failed: %s", tool, e)
            raise RuntimeError(f"{tool} is required but not available in PATH.")

# ======= Modal class decorator: use app.cls then modal.concurrent (order matters)
_min_containers = int(os.environ.get("MIN_CONTAINERS", "1"))
_max_containers = int(os.environ.get("MAX_CONTAINERS", "50"))
# required param for concurrent
_modal_max_inputs = int(os.environ.get("MODAL_MAX_INPUTS", "1"))

@app.cls(
    secrets=[modal.Secret.from_name("Key")],
    image=image,
    gpu=os.environ.get("MODAL_GPU", "A10G"),
    timeout=int(os.environ.get("MODAL_TIMEOUT", "1800")),
    min_containers=_min_containers,
    max_containers=_max_containers,
)
@modal.concurrent(max_inputs=_modal_max_inputs)
class DubbingService:

    @modal.enter()
    def load_models(self):
        # heavy imports kept inside modal.enter to avoid local import-time failures
        import torch
        from transformers import pipeline
        from TTS.api import TTS

        try:
            _check_ffmpeg_tools()
        except RuntimeError as e:
            logger.error("ffmpeg/ffprobe missing: %s", e)
            raise

        logger.info("Loading Whisper (SDPA)...")
        try:
            self.whisper_pipe = pipeline(
                "automatic-speech-recognition",
                model="openai/whisper-medium",
                device="cuda",
                torch_dtype=torch.float16,
                model_kwargs={"attn_implementation": "sdpa"}
            )
        except Exception as e:
            logger.exception("Failed to load whisper model: %s", e)
            raise

        logger.info("Loading XTTS v2...")
        try:
            self.xtts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=True)
        except Exception as e:
            logger.exception("Failed to load XTTS model: %s", e)
            raise

        self.http_session = _build_http_session()
        logger.info("✅ Models loaded | VRAM free: %.1f GB", self._free_vram_gb())

    @modal.exit()
    def cleanup_container(self):
        self._cleanup_vram()
        logger.info("🧹 Container VRAM cleaned.")

    def _free_vram_gb(self) -> float:
        try:
            import torch
            if not torch.cuda.is_available():
                return 0.0
            total    = torch.cuda.get_device_properties(0).total_memory
            reserved = torch.cuda.memory_reserved(0)
            return (total - reserved) / (1024 ** 3)
        except Exception:
            return 0.0

    def _cleanup_vram(self):
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass
        gc.collect()

    async def _format_to_wav_async(self, src: str, dst: str, max_seconds: int = 15):
        await run_cmd_async(
            ["ffmpeg", "-y", "-i", src, "-t", str(max_seconds),
             "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le", dst]
        )

    async def _fit_audio_to_duration_async(self, input_wav: str, output_wav: str, target_sec: float):
        try:
            actual_sec = await probe_duration_async(input_wav)
            if actual_sec <= 0 or target_sec <= 0:
                shutil.copy(input_wav, output_wav)
                return
            tempo = max(0.35, min(3.5, actual_sec / target_sec))
            if abs(tempo - 1.0) < 0.01:
                await run_cmd_async(
                    ["ffmpeg", "-y", "-i", input_wav, "-t", str(target_sec), output_wav]
                )
            else:
                factors, t = [], tempo
                while t > 2.0: factors.append(2.0); t /= 2.0
                while t < 0.5: factors.append(0.5); t /= 0.5
                factors.append(round(t, 6))
                chain = ",".join(f"atempo={f}" for f in factors)
                await run_cmd_async(
                    ["ffmpeg", "-y", "-i", input_wav,
                     "-filter:a", chain, "-t", str(target_sec), output_wav]
                )
        except Exception as e:
            logger.warning("fit_audio fallback (copy): %s", e)
            try:
                shutil.copy(input_wav, output_wav)
            except Exception:
                pass

    def _tts_with_fallback(self, text: str, out_path: str, speaker_wav=None, language: str = "ar"):
        from pydub import AudioSegment
        xtts_lang  = XTTS_LANG_MAP.get(language, language)
        clean_text = text.strip()
        if clean_text and clean_text[-1] not in ['.','!','?','؟','،',',']:
            clean_text += "."

        if speaker_wav and os.path.exists(speaker_wav):
            try:
                self.xtts_model.tts_to_file(
                    text=clean_text, file_path=out_path,
                    speaker_wav=speaker_wav, language=xtts_lang
                )
                return
            except Exception as e:
                logger.warning("XTTS+speaker failed: %s", e)
        try:
            self.xtts_model.tts_to_file(
                text=clean_text, file_path=out_path, language=xtts_lang
            )
            return
        except Exception as e:
            logger.warning("XTTS no-speaker failed: %s", e)

        AudioSegment.silent(duration=1500).export(out_path, format="wav")

    async def _prepare_speaker(self, data: dict, temp_dir: str, fallback_wav: str = None) -> str:
        async def _fetch_and_convert(url: str, raw_name: str, label: str, start_time: float):
            if time.time() - start_time > SPEAKER_FETCH_TOTAL_TIMEOUT:
                logger.warning("Speaker fetch aborted due to total timeout")
                return None
            try:
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(
                    _executor,
                    partial(self.http_session.get, url, timeout=NETWORK_TIMEOUT)
                )
                if resp is None:
                    return None
                if resp.status_code == 200:
                    raw = os.path.join(temp_dir, raw_name)
                    with open(raw, "wb") as f:
                        f.write(resp.content)
                    fmt = os.path.join(temp_dir, f"spk_{label}.wav")
                    await self._format_to_wav_async(raw, fmt, max_seconds=15)
                    return fmt
                logger.warning("Speaker fetch %s → HTTP %d", label, getattr(resp, "status_code", None))
            except Exception as e:
                logger.warning("Speaker fetch/convert failed (%s): %s", label, e)
            return None

        start_time = time.time()

        sample_b64 = data.get("sample_b64") or ""
        if sample_b64:
            try:
                raw = os.path.join(temp_dir, "raw_sample.tmp")
                await stream_b64_to_file(sample_b64, raw)
                fmt = os.path.join(temp_dir, "spk_sample.wav")
                await self._format_to_wav_async(raw, fmt, max_seconds=15)
                return fmt
            except ValueError as e:
                logger.warning("sample_b64 speaker failed: %s", e)
            except Exception as e:
                logger.warning("sample_b64 speaker unexpected error: %s", e)

        voice_id = (data.get("voice_id") or "").strip()
        if voice_id and voice_id not in ("source", ""):
            safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "", voice_id)[:64]
            url = (f"https://raw.githubusercontent.com/sl-Dubbing/sl-dubbing-frontend/main/speakers/{safe_id}.mp3")
            res = await _fetch_and_convert(url, f"raw_{safe_id}.tmp", safe_id, start_time)
            if res:
                return res

        voice_url = (data.get("voice_url") or "").strip()
        if voice_url:
            res = await _fetch_and_convert(voice_url, "raw_url.tmp", "url", start_time)
            if res:
                return res

        if fallback_wav and os.path.exists(fallback_wav):
            return fallback_wav

        res = await _fetch_and_convert(
            "https://res.cloudinary.com/dxbmvzsiz/video/upload/v1712611200/sl_voices/muhammad_ar.wav",
            "raw_default.tmp", "default", start_time
        )
        return res

    def _safe_translate(self, translator, text: str, fallback: str) -> str:
        if not text.strip():
            return fallback
        try:
            return translator.translate(text[:MAX_TEXT_CHARS]) or fallback
        except Exception as e:
            logger.warning("Translation failed: %s", e)
            return fallback

    def _upload_to_gcs(self, local_path: str, creds: dict, prefix: str = "out") -> str:
        from google.cloud import storage
        bucket_name = os.environ.get("GCS_BUCKET_NAME", "dubbing-bucket-sl")
        client    = storage.Client.from_service_account_info(creds)
        blob_name = f"results/{prefix}_{uuid.uuid4().hex[:10]}.wav"
        blob      = client.bucket(bucket_name).blob(blob_name)
        blob.upload_from_filename(local_path)
        try:
            url = blob.generate_signed_url(expiration=int(os.environ.get("SIGNED_URL_TTL", 3600)))
            logger.info("GCS upload complete (signed URL): %s", blob_name)
            return url
        except Exception:
            try:
                blob.make_public()
                logger.info("GCS upload complete (public): %s", blob_name)
                return blob.public_url
            except Exception:
                logger.exception("Failed to make blob public or generate signed URL")
                raise HTTPException(status_code=500, detail="Upload succeeded but cannot generate public URL.")

    async def _process_file_on_disk(self, input_path: str, data: dict, temp_dir: str):
        from pydub import AudioSegment
        from deep_translator import GoogleTranslator

        start_time = time.time()
        validate_file_on_disk(input_path)
        gcp_creds = validate_gcp_credentials()
        target_lang = data.get("lang", "ar")

        try:
            wav_path = os.path.join(temp_dir, "source.wav")
            try:
                await run_cmd_async(
                    ["ffmpeg", "-y", "-i", input_path, "-vn",
                     "-ar", "22050", "-ac", "1", wav_path],
                    timeout=FFMPEG_TIMEOUT
                )
            except RuntimeError as e:
                logger.error("ffmpeg conversion failed: %s", e)
                raise HTTPException(status_code=502, detail="Audio conversion failed.")

            original_duration = await probe_duration_async(wav_path)

            batch_size = get_dynamic_batch_size()
            logger.info("Transcription | batch=%d | lang=%s | vram_free=%.1fGB",
                        batch_size, target_lang, self._free_vram_gb())

            loop = asyncio.get_event_loop()
            try:
                whisper_res = await loop.run_in_executor(
                    _executor,
                    partial(self.whisper_pipe, wav_path,
                            chunk_length_s=30, batch_size=batch_size,
                            return_timestamps=True)
                )
            except Exception as e:
                msg = str(e).lower()
                try:
                    import torch
                    is_torch_oom = isinstance(e, getattr(__import__('torch'), 'cuda', object))
                except Exception:
                    is_torch_oom = False
                if "out of memory" in msg or "cuda" in msg or "oom" in msg or is_torch_oom:
                    logger.error("CUDA OOM during transcription: %s", e)
                    try:
                        whisper_res = await loop.run_in_executor(
                            _executor,
                            partial(self.whisper_pipe, wav_path,
                                    chunk_length_s=30, batch_size=max(1, batch_size//2),
                                    return_timestamps=True)
                        )
                    except Exception:
                        raise HTTPException(status_code=503, detail="Transcription OOM, try again later.")
                else:
                    logger.exception("Unexpected whisper error: %s", e)
                    raise HTTPException(status_code=503, detail="Transcription service unavailable.")

            segments  = whisper_res.get("chunks", [])
            full_text = whisper_res.get("text", "").strip()

            if not segments:
                url = self._upload_to_gcs(wav_path, gcp_creds, prefix="nodub")
                return JSONResponse({
                    "success": True, "audio_url": url,
                    "original_text": "", "translated_text": ""
                })

            translator      = GoogleTranslator(source="auto", target=target_lang)
            full_translated = (
                self._safe_translate(translator, full_text, "")
                if len(full_text) < MAX_TEXT_CHARS
                else "Text too long for full translation."
            )

            speaker_wav = await self._prepare_speaker(
                data, temp_dir, fallback_wav=wav_path
            )
            final_audio = AudioSegment.silent(
                duration=int(original_duration * 1000) + 500
            )

            for i, seg in enumerate(segments):
                ts         = seg.get("timestamp") or (None, None)
                start_time_seg = ts[0] if ts[0] is not None else 0.0
                end_time_seg   = ts[1] if ts[1] is not None else (start_time_seg + 2.0)
                start_ms   = int(start_time_seg * 1000)
                target_sec = max(end_time_seg - start_time_seg, 0.1)

                raw_text = seg.get("text", "").strip()
                if not raw_text or target_sec < 0.2:
                    continue

                chunk_translated = self._safe_translate(translator, raw_text, raw_text)
                chunk_raw    = os.path.join(temp_dir, f"raw_{i}.wav")
                chunk_fitted = os.path.join(temp_dir, f"fit_{i}.wav")

                await loop.run_in_executor(
                    _executor,
                    partial(self._tts_with_fallback, chunk_translated,
                            chunk_raw, speaker_wav, target_lang)
                )
                await self._fit_audio_to_duration_async(chunk_raw, chunk_fitted, target_sec)

                try:
                    final_audio = final_audio.overlay(
                        AudioSegment.from_wav(chunk_fitted), position=start_ms
                    )
                except Exception as e:
                    logger.warning("Overlay failed at seg %d: %s", i, e)

            out_path   = os.path.join(temp_dir, "final_dubbed.wav")
            final_audio.export(out_path, format="wav")
            public_url = self._upload_to_gcs(out_path, gcp_creds, "dub")

            elapsed = time.time() - start_time
            logger.info("Dubbing completed in %.2fs", elapsed)

            return JSONResponse({
                "success": True,
                "audio_url": public_url,
                "original_text": full_text,
                "translated_text": full_translated
            })

        except HTTPException:
            raise
        except RuntimeError as e:
            logger.error("Processing error: %s", e)
            raise HTTPException(status_code=502, detail=f"Processing failed: {e}")
        except Exception as e:
            logger.exception("Unexpected error in processing file on disk")
            raise HTTPException(status_code=500, detail="Internal server error.")
        finally:
            self._cleanup_vram()

    @modal.asgi_app()
    def fastapi_app(self):
        web_app = FastAPI()

        # JSON base64 endpoint (disabled in production)
        @web_app.post("/")
        async def process_dubbing(request: Request):
            from pydub import AudioSegment
            from deep_translator import GoogleTranslator

            if os.environ.get("ENV", "development") == "production":
                raise HTTPException(status_code=403, detail="Base64 JSON uploads disabled in production. Use multipart /upload.")

            start_time = time.time()
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_B64_CHARS:
                        raise HTTPException(status_code=413, detail="Request too large.")
                except ValueError:
                    pass

            data = await request.json()
            file_b64 = data.get("file_b64")
            if not file_b64:
                raise HTTPException(status_code=400, detail="file_b64 is required.")

            validate_b64_header_length_only(len(file_b64))
            temp_dir = make_secure_tempdir()
            try:
                input_path = os.path.join(temp_dir, "input.bin")
                try:
                    await stream_b64_to_file(file_b64, input_path)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Bad base64: {e}")
                except Exception:
                    logger.exception("Unexpected error during base64 streaming")
                    raise HTTPException(status_code=400, detail="Failed to decode base64 file.")
                return await self._process_file_on_disk(input_path, data, temp_dir)
            finally:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass

        # multipart upload endpoint
        @web_app.post("/upload")
        async def upload_file_endpoint(media_file: UploadFile = File(...), lang: str = "ar",
                                       voice_id: str = "", sample_b64: str = ""):
            import aiofiles
            temp_dir = make_secure_tempdir()
            input_path = os.path.join(temp_dir, "source_upload")
            try:
                try:
                    async with aiofiles.open(input_path, "wb") as out:
                        while True:
                            chunk = await media_file.read(8192)
                            if not chunk:
                                break
                            await out.write(chunk)
                except Exception as e:
                    logger.exception("Failed to write uploaded file: %s", e)
                    raise HTTPException(status_code=400, detail="Failed to receive uploaded file.")

                validate_file_on_disk(input_path)
                data = {"lang": lang, "voice_id": voice_id, "sample_b64": sample_b64}
                response = await self._process_file_on_disk(input_path, data, temp_dir)
                return response
            finally:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass

        # TTS endpoint
        @web_app.post("/tts")
        async def process_tts(request: Request):
            from pydub import AudioSegment
            from deep_translator import GoogleTranslator

            start_time = time.time()
            data     = await request.json()
            raw_text = (data.get("text") or "").strip()
            if not raw_text:
                raise HTTPException(status_code=400, detail="text is required.")
            if len(raw_text) > MAX_TTS_CHARS:
                raise HTTPException(status_code=413, detail=f"Text too long. Max {MAX_TTS_CHARS} characters.")

            gcp_creds   = validate_gcp_credentials()
            target_lang = data.get("lang", "en")
            temp_dir    = make_secure_tempdir()

            try:
                translator = GoogleTranslator(source="auto", target=target_lang)
                translated = self._safe_translate(translator, raw_text, raw_text)

                speaker_wav = await self._prepare_speaker(data, temp_dir, fallback_wav=None)

                sentences = (
                    [s.strip()
                     for s in re.split(r'(?<=[.!?؟\n])\s+|\n+', translated)
                     if s.strip()]
                    or [translated]
                )
                combined     = AudioSegment.empty()
                breath_pause = AudioSegment.silent(duration=380)
                loop         = asyncio.get_event_loop()

                for i, sentence in enumerate(sentences):
                    chunk_path = os.path.join(temp_dir, f"tts_{i}.wav")
                    await loop.run_in_executor(
                        _executor,
                        partial(self._tts_with_fallback, sentence,
                                chunk_path, speaker_wav, target_lang)
                    )
                    try:
                        combined += AudioSegment.from_wav(chunk_path) + breath_pause
                    except Exception as e:
                        logger.warning("TTS concat failed at sentence %d: %s", i, e)

                if len(combined) == 0:
                    raise RuntimeError("No audio was generated.")

                out_path   = os.path.join(temp_dir, "tts_output.wav")
                combined.export(out_path, format="wav")
                public_url = self._upload_to_gcs(out_path, gcp_creds, "tts")

                elapsed = time.time() - start_time
                logger.info("TTS completed in %.2fs", elapsed)

                return JSONResponse({
                    "success": True,
                    "audio_url": public_url,
                    "final_text": translated,
                    "original_text": raw_text
                })

            except HTTPException:
                raise
            except RuntimeError as e:
                logger.error("TTS error: %s", e)
                raise HTTPException(status_code=502, detail=f"TTS failed: {e}")
            except Exception as e:
                logger.exception("Unexpected error in /tts")
                raise HTTPException(status_code=500, detail="Internal server error.")
            finally:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass
                self._cleanup_vram()

        # Health
        @web_app.get("/health")
        async def health():
            try:
                import torch
                if not torch.cuda.is_available():
                    return {"status": "ok", "gpu": False}
                total    = torch.cuda.get_device_properties(0).total_memory
                reserved = torch.cuda.memory_reserved(0)
                free_gb = (total - reserved) / (1024 ** 3)
                return {"status": "ok", "gpu": True, "vram_free_gb": round(free_gb, 2),
                        "batch_size": get_dynamic_batch_size()}
            except Exception:
                return {"status": "ok", "gpu": False}

        return web_app
