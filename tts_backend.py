# tts_backend.py
import os
import logging
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level model holders
xtts_model = None
cosy_model = None

def _temp_path(suffix=".mp3"):
    fd, p = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return p

def convert_wav_to_mp3(wav_path, mp3_path, bitrate="192k"):
    import subprocess
    try:
        subprocess.run(['ffmpeg','-y','-i', str(wav_path), '-b:a', bitrate, str(mp3_path)],
                       check=True, capture_output=True, timeout=120)
        return True
    except Exception as e:
        logger.warning("ffmpeg conversion failed: %s", e)
        return False

def load_xtts():
    global xtts_model
    if xtts_model is None:
        try:
            import torch
            from TTS.api import TTS
            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info("Loading XTTS v2 on %s", device)
            xtts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False, gpu=(device=="cuda"))
            logger.info("XTTS loaded")
        except Exception:
            logger.exception("Failed to load XTTS")
            xtts_model = None
    return xtts_model

def load_cosy():
    global cosy_model
    if cosy_model is None:
        try:
            # حاول استيراد مكتبة CosyVoice — استبدل هذا إذا كان اسم الحزمة مختلفًا
            import cosyvoice
            # افتراض: cosyvoice.CosyVoice(...) — عدّل حسب التوثيق الحقيقي
            device = "cuda" if getattr(cosyvoice, "is_cuda_available", lambda: False)() else "cpu"
            logger.info("Loading CosyVoice 3.0 on %s", device)
            cosy_model = cosyvoice.CosyVoice(model="cosyvoice-3.0", device=device)
            logger.info("CosyVoice loaded")
        except Exception:
            logger.exception("Failed to load CosyVoice (package may be missing or API differs)")
            cosy_model = None
    return cosy_model

def synthesize_text(text: str, lang: str='ar', voice_mode: str='xtts', voice_id: str='', voice_url: str='') -> str:
    """
    Returns path to MP3 file (local path). Raises Exception on total failure.
    voice_mode: 'xtts', 'cosy', 'gtts', 'source'
    voice_url: optional sample WAV URL for cloning modes
    """
    tmp_wav = None
    tmp_mp3 = None
    try:
        tmp_wav = _temp_path(suffix=".wav")
        tmp_mp3 = tmp_wav[:-4] + ".mp3"

        # XTTS path
        if voice_mode == 'xtts':
            model = load_xtts()
            if model:
                try:
                    speaker = voice_url if voice_url else None
                    model.tts_to_file(text=text, speaker_wav=speaker, language=lang, file_path=tmp_wav, split_sentences=True, verbose=False)
                    if convert_wav_to_mp3(tmp_wav, tmp_mp3):
                        return tmp_mp3
                    return tmp_wav
                except Exception:
                    logger.exception("XTTS generation failed, falling back")

        # CosyVoice path
        if voice_mode == 'cosy':
            model = load_cosy()
            if model:
                try:
                    # استبدل السطر التالي بدعوة CosyVoice الحقيقية حسب التوثيق
                    # مثال افتراضي:
                    # model.synthesize_to_file(text=text, speaker=voice_id or None, out_path=tmp_wav, lang=lang)
                    model.synthesize_to_file(text=text, speaker=voice_id or None, out_path=tmp_wav, lang=lang)
                    if convert_wav_to_mp3(tmp_wav, tmp_mp3):
                        return tmp_mp3
                    return tmp_wav
                except Exception:
                    logger.exception("CosyVoice generation failed, falling back")

        # source cloning: try xtts then cosy
        if voice_mode == 'source' and voice_url:
            model = load_xtts()
            if model:
                try:
                    model.tts_to_file(text=text, speaker_wav=voice_url, language=lang, file_path=tmp_wav, split_sentences=True, verbose=False)
                    if convert_wav_to_mp3(tmp_wav, tmp_mp3):
                        return tmp_mp3
                    return tmp_wav
                except Exception:
                    logger.exception("Source cloning via XTTS failed")
            model = load_cosy()
            if model:
                try:
                    # مثال افتراضي: model.synthesize_to_file(..., sample_wav=voice_url, ...)
                    model.synthesize_to_file(text=text, speaker=voice_id or None, sample_wav=voice_url, out_path=tmp_wav, lang=lang)
                    if convert_wav_to_mp3(tmp_wav, tmp_mp3):
                        return tmp_mp3
                    return tmp_wav
                except Exception:
                    logger.exception("Source cloning via CosyVoice failed")

        # Fallback to gTTS
        try:
            from gtts import gTTS
            gTTS(text=text, lang=lang[:2]).save(tmp_mp3)
            return tmp_mp3
        except Exception:
            logger.exception("gTTS fallback failed")
            raise RuntimeError("All TTS methods failed")
    finally:
        # cleanup WAV if mp3 produced
        try:
            if tmp_wav and Path(tmp_wav).exists() and tmp_mp3 and Path(tmp_mp3).exists():
                Path(tmp_wav).unlink(missing_ok=True)
        except Exception:
            pass
