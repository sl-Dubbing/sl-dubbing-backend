import os
import time
from celery import Celery
import cloudinary
import cloudinary.uploader
from TTS.api import TTS
import torch

# --- 🛠️ إعدادات السحاب ---
CLOUDINARY_NAME = "dxbmvzsiz"
CLOUDINARY_API_KEY = "432687952743126"
CLOUDINARY_API_SECRET = "BrFvzlPFXBJZ-B-cZyxCc-0wHRo"
REDIS_URL = "rediss://default:gQAAAAAAAXrOAAIncDIyYWIyMzA5NTE2NTU0M2YzYjk0MGM0ZTVjZjRiZjA5M3AyOTY5NzQ@primary-muskrat-96974.upstash.io:6379"

cloudinary.config(cloud_name=CLOUDINARY_NAME, api_key=CLOUDINARY_API_KEY, api_secret=CLOUDINARY_API_SECRET)

app = Celery('tasks', broker=REDIS_URL, backend=REDIS_URL)
app.conf.update(broker_use_ssl={'ssl_cert_reqs': 'none'}, redis_backend_use_ssl={'ssl_cert_reqs': 'none'}, worker_prefetch_multiplier=1)

# تحميل موديل XTTS
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"--- 🚀 Loading XTTS on {device} ---")
tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)

@app.task(name='tasks.process_tts', bind=True)
def process_tts(self, data):
    try:
        # ملاحظة: إذا كان النص عربي، تأكد من اختيار "العربية" في الموقع 
        # لأن السجل أظهر أنك اخترت 'en' (إنجليزي) لنص عربي!
        lang = data.get('lang', 'ar')
        
        # توافق أكواد اللغات مع XTTS
        if lang == 'en': lang = 'en'
        elif lang == 'ar': lang = 'ar'
            
        speaker_id = data.get('speaker_id', 'auto')
        ref_speaker = "speakers/muhammad.wav" 
        if speaker_id != 'auto' and os.path.exists(f"speakers/{speaker_id}.wav"):
            ref_speaker = f"speakers/{speaker_id}.wav"

        # --- 🧠 الحل السحري لتجنب خطأ 400 توكن ---
        if 'segments' in data:
            # نربط المقاطع بـ "نقطة" لكي يقسمها الموديل ويتنفس بين الجمل
            full_text = " . ".join([s['text'].strip() for s in data['segments']])
        else:
            full_text = data.get('text', '')

        output_path = f"final_output_{int(time.time())}.wav"
        
        print(f"--- 🎙️ جاري توليد الصوت للغة ({lang})... سيتم المعالجة جملة بجملة ---")

        # التوليد الفعلي
        tts.tts_to_file(
            text=full_text,
            speaker_wav=ref_speaker,
            language=lang,
            file_path=output_path,
            split_sentences=True # سيتعرف على النقاط ويقسم النص بشكل مثالي الآن
        )

        print("--- ☁️ جاري الرفع إلى Cloudinary ---")
        upload_res = cloudinary.uploader.upload(output_path, resource_type="video")
        
        if os.path.exists(output_path): os.remove(output_path)

        print("--- ✅ تمت الدبلجة بنجاح! ---")
        return {"status": "done", "audio_url": upload_res['secure_url']}

    except Exception as e:
        print(f"❌ خطأ: {str(e)}")
        return {"status": "error", "error": str(e)}