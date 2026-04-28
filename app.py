# تأكد من استيراد دالة المهام وقاعدة البيانات في أعلى الملف
from tasks import process_smart_tts
from models import db, DubbingJob

# ==========================================
# 🚀 1. مسار إرسال الطلب (الذي يرسل المهمة لـ Celery)
# ==========================================
@app.route('/api/tts', methods=['POST'])
@token_required  # تأكد من استخدام اسم الـ decorator الخاص بك للمصادقة
def start_tts(current_user):
    data = request.json
    if not data or 'text' not in data:
        return jsonify({"success": False, "error": "النص غير موجود"})

    try:
        # 1. إنشاء مهمة في قاعدة البيانات
        new_job = DubbingJob(
            user_id=current_user.id,
            status='processing'
            # أضف أي حقول أخرى تتطلبها قاعدة بياناتك
        )
        db.session.add(new_job)
        db.session.commit()

        # 2. تجهيز البيانات للعامل (Worker)
        payload = {
            'job_id': new_job.id,
            'text': data['text'],
            'lang': data.get('lang', 'en'),
            'voice_id': data.get('voice_id', ''),
            'sample_b64': data.get('sample_b64', ''),
            'edge_voice': data.get('edge_voice', ''),
            'translate': data.get('translate', True),
            'rate': data.get('rate', '+0%'),
            'pitch': data.get('pitch', '+0Hz')
        }

        # 3. إرسال المهمة إلى طابور Celery ليعالجها ملف tasks.py
        process_smart_tts.delay(payload)

        # 4. إرجاع رقم المهمة للواجهة لتبدأ بالانتظار
        return jsonify({"success": True, "job_id": new_job.id})

    except Exception as e:
        print(f"❌ Error starting TTS job: {e}")
        return jsonify({"success": False, "error": "حدث خطأ أثناء بدء العملية."})

# ==========================================
# 🔄 2. مسار فحص حالة المهمة (Polling Route)
# ==========================================
@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def check_job(current_user, job_id):
    try:
        # البحث عن المهمة في قاعدة البيانات
        job = DubbingJob.query.get(job_id)
        
        if not job:
            return jsonify({"status": "failed", "error": "المهمة غير موجودة"})

        # إذا اكتملت المهمة (العامل قام بتحديث الحالة ووضع رابط الملف)
        if job.status == 'completed':
            return jsonify({
                "status": "completed", 
                "audio_url": job.output_url
            })
            
        # إذا فشلت المهمة
        elif job.status == 'failed':
            return jsonify({"status": "failed", "error": "فشلت المعالجة"})
            
        # إذا كانت لا تزال قيد المعالجة
        else:
            return jsonify({"status": "processing"})

    except Exception as e:
        print(f"❌ Job Check Error: {e}")
        return jsonify({"status": "failed", "error": "خطأ في السيرفر"})
