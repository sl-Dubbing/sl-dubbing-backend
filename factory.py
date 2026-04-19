import modal
from fastapi import FastAPI, Request
import os, tempfile, subprocess, base64, urllib.request, shutil

image = (
    modal.Image.debian_slim()
    .apt_install("ffmpeg", "libasound2", "libsndfile1")
    .run_commands("echo 'Version 11.0 - Ultimate Audio Purifier'")
    .pip_install(
        "fastapi", "uvicorn", "openai-whisper", "TTS", 
        "soundfile", "transformers==4.35.2", 
        "torch==2.5.1", "torchaudio==2.5.1", "deep-translator", "requests"
    )
    .env({"COQUI_TOS_AGREED": "1", "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"})
)

app = modal.App("sl-dubbing-factory")
web_app = FastAPI()
whisper_model = None
xtts_model = None

def load_models():
    global whisper_model, xtts_model
    import torch
    if whisper_model is None:
        import whisper
        whisper_model = whisper.load_model("base", device="cuda" if torch.cuda.is_available() else "cpu")
    if xtts_model is None:
        from TTS.api import TTS
        xtts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    return whisper_model, xtts_model

# --- مسار الدبلجة الأساسي ---
@web_app.post("/")
async def process_dubbing(request: Request):
    data = await request.json()
    file_b64 = data.get("file_b64")
    target_lang = data.get("lang", "ar")
    voice_url = data.get("voice_url", "")

    w_model, t_model = load_models()
    temp_dir = tempfile.mkdtemp()

    try:
        input_path = os.path.join(temp_dir, "in.mp4")
        with open(input_path, "wb") as f:
            f.write(base64.b64decode(file_b64))
        
        res = subprocess.run(["ffprobe", "-i", input_path, "-show_entries", "format=duration", "-v", "quiet", "-of", "csv=p=0"], capture_output=True, text=True)
        original_duration = float(res.stdout.strip())
        
        source_wav = os.path.join(temp_dir, "source.wav")
        subprocess.run(["ffmpeg", "-y", "-i", input_path, "-vn", "-ar", "22050", "-ac", "1", source_wav], check=True)

        from deep_translator import GoogleTranslator
        raw_text = w_model.transcribe(source_wav)["text"].strip()
        final_text = GoogleTranslator(source='auto', target=target_lang).translate(raw_text)

        speaker_wav = source_wav
        if voice_url:
            speaker_wav = os.path.join(temp_dir, "sample.wav")
            try:
                import requests
                r = requests.get(voice_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
                if r.status_code == 200:
                    with open(speaker_wav, 'wb') as f: f.write(r.content)
                else: speaker_wav = source_wav
            except Exception: speaker_wav = source_wav

        raw_ai_wav = os.path.join(temp_dir, "raw_ai.wav")
        t_model.tts_to_file(text=final_text, speaker_wav=speaker_wav, language=target_lang, file_path=raw_ai_wav)
        
        res_temp = subprocess.run(["ffprobe", "-i", raw_ai_wav, "-show_entries", "format=duration", "-v", "quiet", "-of", "csv=p=0"], capture_output=True, text=True)
        generated_duration = float(res_temp.stdout.strip())
        tempo = generated_duration / original_duration
        
        final_locked_wav = os.path.join(temp_dir, "final_locked.wav")
        subprocess.run(["ffmpeg", "-y", "-i", raw_ai_wav, "-filter:a", f"atempo={tempo}", "-t", str(original_duration), final_locked_wav], check=True)

        with open(final_locked_wav, "rb") as f: encoded = base64.b64encode(f.read()).decode('utf-8')
        return {"success": True, "audio_base64": encoded, "transcription": final_text}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

# --- مسار الـ TTS المطور (المفلتر) ---
@web_app.post("/tts")
async def process_tts(request: Request):
    data = await request.json()
    raw_text = data.get("text", "")
    target_lang = data.get("lang", "en")
    voice_id = data.get("voice_id", "source") 
    sample_b64 = data.get("sample_b64") 
    
    _, t_model = load_models()
    temp_dir = tempfile.mkdtemp()

    try:
        from deep_translator import GoogleTranslator
        translated_text = GoogleTranslator(source='auto', target=target_lang).translate(raw_text)
        
        output_wav = os.path.join(temp_dir, "tts_out.wav")
        speaker_wav = None
        
        # 🟢 الفلتر السحري: تجهيز وتطهير الصوت ليقبله الذكاء الاصطناعي
        if sample_b64:
            raw_sample = os.path.join(temp_dir, "raw_sample.tmp")
            speaker_wav = os.path.join(temp_dir, "pure_speaker.wav")
            with open(raw_sample, "wb") as f:
                f.write(base64.b64decode(sample_b64))
            
            # قص أول 10 ثوانٍ، وتحويل لـ 16-bit PCM, 22050Hz, Mono (وهي طلبات النموذج الصارمة)
            subprocess.run(["ffmpeg", "-y", "-i", raw_sample, "-t", "10", "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le", speaker_wav], check=True)
            
        elif voice_id != "source":
            import requests
            speaker_wav = os.path.join(temp_dir, "pure_speaker.wav")
            urls = [
                f"https://sl-dubbing.github.io/dubbing-studio/samples/{voice_id}.mp3",
                f"https://raw.githubusercontent.com/sl-Dubbing/dubbing-studio/main/samples/{voice_id}.mp3"
            ]
            downloaded = False
            for url in urls:
                try:
                    r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
                    if r.status_code == 200:
                        raw_sample = os.path.join(temp_dir, "raw_sample.tmp")
                        with open(raw_sample, 'wb') as f: f.write(r.content)
                        subprocess.run(["ffmpeg", "-y", "-i", raw_sample, "-t", "10", "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le", speaker_wav], check=True)
                        downloaded = True
                        break
                except Exception: continue
            
            if not downloaded:
                speaker_wav = None

        if speaker_wav and os.path.exists(speaker_wav):
            t_model.tts_to_file(text=translated_text, file_path=output_wav, speaker_wav=speaker_wav, language=target_lang)
        else:
            t_model.tts_to_file(text=translated_text, file_path=output_wav, speaker="Claribel Dervla", language=target_lang)

        with open(output_wav, "rb") as f:
            encoded = base64.b64encode(f.read()).decode('utf-8')

        return {"success": True, "audio_base64": encoded, "final_text": translated_text}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.function(secrets=[modal.Secret.from_name("MODAL_KEYS")], image=image, gpu="T4", timeout=600)
@modal.asgi_app()
def fastapi_app():
    return web_app
