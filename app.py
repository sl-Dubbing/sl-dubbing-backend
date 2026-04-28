import os
import time
import base64
import jwt
import modal
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from functools import wraps

# ==========================================
# 1. إعدادات السيرفر الأساسية (Flask)
# ==========================================
app = Flask(__name__)
# السماح للواجهة الأمامية بالاتصال بالسيرفر
CORS(app, resources={r"/api/*": {"origins": "*"}})

# إعدادات الحماية ومسار حفظ الملفات
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-super-secret-key-here')
RESULTS_DIR = os.path.join(os.getcwd(), 'static', 'results')

# إنشاء مجلد حفظ الصوتيات إذا لم يكن موجوداً
if not os.path.exists(RESULTS_DIR):
    os.makedirs(RESULTS_DIR)

# ==========================================
# 2. نظام المصادقة (Middleware)
# ==========================================
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        # استخراج التوكن من الـ Headers
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            token = auth_header.split(" ")[1] if "Bearer" in auth_header else auth_header

        if not token:
            return jsonify({'success': False, 'message': 'التوكن مفقود!'}), 401

        try:
            # ⚠️ [تعديل مطلوب]: قم بفك تشفير التوكن حسب نظامك.
            # إذا كنت لا تستخدم التشفير حالياً للـ testing، يمكنك استخدام التوكن كمعرف مباشر
            # data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            # current_user_id = data['user_id']
            
            # (للتجربة حالياً سنفترض أن التوكن صحيح وننشئ مستخدم وهمي)
            current_user = {
                "id": 1,
                "name": "مستخدم تجريبي",
                "credits": 45420  # رصيد النقاط
            }
            
        except Exception as e:
            return jsonify({'success': False, 'message': 'توكن غير صالح!'}), 401

        return f(current_user, *args, **kwargs)
    return decorated

# ==========================================
# 3. مسارات الـ API (الواجهة الخلفية)
# ==========================================

# أ. مسار جلب بيانات المستخدم والنقاط (للشريط الجانبي)
@app.route('/api/user', methods=['GET'])
@token_required
def get_user(current_user):
    # ⚠️ [تعديل مطلوب]: هنا تجلب بيانات المستخدم الحقيقية من قاعدة بياناتك
    return jsonify({'success': True, 'user': current_user})

# ب. مسار بدء عملية التوليد (يرسل الطلب لـ Modal ويعيد Job ID)
@app.route('/api/tts', methods=['POST'])
@token_required
def start_tts(current_user):
    data = request.json
    if not data or 'text' not in data:
        return jsonify({"success": False, "error": "النص غير موجود"})

    # ⚠️ [تعديل مطلوب]: هنا يمكنك التحقق من رصيد المستخدم (credits) قبل التوليد
    if current_user['credits'] <= 0:
        return jsonify({"success": False, "error": "رصيدك غير كافٍ"})

    try:
        print("🚀 إرسال المهمة إلى Modal...")
        # الاتصال بدالة Modal
        tts_function = modal.Function.lookup("sl-tts-factory", "process_tts")
        
        # استخدام spawn() بدلاً من remote() لكي يعمل في الخلفية ويعطينا رقم المهمة
        call = tts_function.spawn(data)
        
        # إرجاع رقم المهمة للواجهة لتبدأ بالانتظار (Polling)
        return jsonify({"success": True, "job_id": call.object_id})
        
    except Exception as e:
        print(f"❌ فشل الاتصال بـ Modal: {e}")
        return jsonify({"success": False, "error": "فشل الاتصال بسيرفر الذكاء الاصطناعي"})

# ج. مسار فحص حالة المهمة (Polling Route)
@app.route('/api/job/<job_id>', methods=['GET'])
@token_required
def check_job(current_user, job_id):
    try:
        # جلب المهمة من Modal باستخدام الـ ID
        call = modal.FunctionCall.from_id(job_id)
        
        # محاولة جلب النتيجة (ننتظر 0.1 ثانية فقط، إذا لم تنتهِ ستعطي TimeoutError)
        result = call.get(timeout=0.1)

        # إذا وصلنا هنا، يعني أن المهمة انتهت بنجاح! 🎉
        if result.get("success"):
            audio_base64 = result.get("audio_base64")
            lang = result.get("text_used", "audio")[:5] # جزء من النص كاسم
            
            # حفظ ملف الصوت في Railway
            file_name = f"tts_{int(time.time())}.mp3"
            file_path = os.path.join(RESULTS_DIR, file_name)

            with open(file_path, "wb") as fh:
                fh.write(base64.b64decode(audio_base64))

            # إنشاء رابط الملف
            audio_url = f"{request.host_url.rstrip('/')}/static/results/{file_name}"

            # ⚠️ [تعديل مطلوب]: هنا تقوم بخصم النقاط من قاعدة بيانات المستخدم بعد نجاح التوليد
            # update_user_credits(current_user['id'], cost)

            return jsonify({"status": "completed", "audio_url": audio_url})
        else:
            # انتهت المهمة ولكن حدث خطأ داخل Modal
            return jsonify({"status": "failed", "error": result.get("error", "خطأ غير معروف")})

    except TimeoutError:
        # المهمة ما زالت تعمل (لم تنتهِ بعد)
        return jsonify({"status": "processing"})
        
    except Exception as e:
        # خطأ في السيرفر أو المهمة غير موجودة
        print(f"Job Check Error: {e}")
        return jsonify({"status": "failed", "error": "المهمة غير صالحة أو منتهية"})

# د. مسار قراءة الملفات الصوتية لتشغيلها في المتصفح
@app.route('/static/results/<filename>')
def serve_audio(filename):
    return send_from_directory(RESULTS_DIR, filename)

# ==========================================
# 4. تشغيل السيرفر
# ==========================================
if __name__ == '__main__':
    # سيقوم Railway بتمرير رقم البورت تلقائياً
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
