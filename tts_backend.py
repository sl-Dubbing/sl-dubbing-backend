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

def load_xtts():
    global xtts_model
    if xtts_model is None:
        try:
            import torch
            from TTS.api import TTS
            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info(f"Loading XTTS v2 on {device}")
            xtts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False, gpu=(device=="cuda"))
            logger.info("XTTS loaded")
        except Exception as e:
            logger.exception("Failed to load XTTS")
            xtts_model = None
    return xtts_model

def load_cosy():
    global cosy_model
    if cosy_model is None:
        try:
            # مثال افتراضي: استبدل بتحميل CosyVoice حسب التوثيق الحقيقي
            from cosyvoice import CosyVoice
            device = "cuda" if CosyVoice.is_cuda_available() else "cpu"
            logger.info(f"Loading CosyVoice 3.0 on {device}")
            cosy_model = CosyVoice(model="cosyvoice-3.0", device=device)
            logger.info("CosyVoice loaded")
        except Exception:
            logger.exception("Failed to load CosyVoice")
            cosy_model = None
    return cosy_model

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

def synthesize_text(text: str, lang: str='ar', voice_mode: str='xtts', voice_id: str='', voice_url: str='') -> Optional[str]:
    """
    Returns path to MP3 file or raises Exception.
    """
    # prepare temp files
    tmp_wav = _temp_path(suffix=".wav")
    tmp_mp3 = tmp_wav[:-4] + ".mp3"

    # Try XTTS
    if voice_mode == 'xtts':
        model = load_xtts()
        if model:
            try:
                model.tts_to_file(text=text, speaker_wav=voice_url or None, language=lang, file_path=tmp_wav, split_sentences=True, verbose=False)
                if convert_wav_to_mp3(tmp_wav, tmp_mp3):
                    return tmp_mp3
                else:
                    return tmp_wav  # fallback
            except Exception:
                logger.exception("XTTS generation failed, falling back")
    # Try CosyVoice
    if voice_mode == 'cosy':
        model = load_cosy()
        if model:
            try:
                # استبدل السطر التالي بدعوة CosyVoice الحقيقية
                model.synthesize_to_file(text=text, speaker=voice_id, out_path=tmp_wav, lang=lang)
                if convert_wav_to_mp3(tmp_wav, tmp_mp3):
                    return tmp_mp3
                else:
                    return tmp_wav
            except Exception:
                logger.exception("CosyVoice generation failed, falling back")
    # Fallback to gTTS
    try:
        from gtts import gTTS
        gTTS(text=text, lang=lang[:2]).save(tmp_mp3)
        return tmp_mp3
    except Exception:
        logger.exception("gTTS fallback failed")
        raise RuntimeError("All TTS methods failed")
