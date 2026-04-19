# text_corrector.py
import requests
import os

def smart_correct_srt(raw_srt_text, api_key=None):
    """
    هذه الدالة تأخذ نص الـ SRT الخام، وتصححه عبر OpenAI، وتعيده بنفس الهيكلة
    """
    # نأخذ المفتاح الممرر من Railway، أو نبحث عنه في النظام كخيار بديل
    openai_key = api_key or os.getenv("OPENAI_API_KEY")
    
    if not openai_key:
        print("Warning: No OPENAI_API_KEY provided. Returning raw text.")
        return raw_srt_text

    system_prompt = """
    أنت مدقق لغوي ومترجم محترف في استوديو دبلجة. سأعطيك نصاً بصيغة SRT يحتوي على توقيتات وجمل.
    مهمتك هي إصلاح النص بناءً على القواعد التالية فقط:
    1. صحح أي أخطاء إملائية أو نحوية في اللغة العربية.
    2. استبدل الكلمات الإنجليزية المكتوبة بحروف عربية إلى أصلها الإنجليزي (مثال: "تايم لاين" تصبح "Timeline").
    3. **قاعدة صارمة:** إياك أن تغير أو تحذف أو تعدل الأرقام التسلسلية أو التوقيتات الزمنية (التي تحتوي على أسهم -->). حافظ على هيكل SRT كما هو بالضبط بنسبة 100%.
    """

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {openai_key}"
    }

    payload = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"قم بتصحيح هذا النص:\n\n{raw_srt_text}"}
        ],
        "temperature": 0.1 # درجة حرارة منخفضة لكي لا يضيف كلاماً من عنده
    }

    try:
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=120)
        response_data = response.json()
        
        # التأكد من نجاح الطلب
        if response.status_code == 200:
            corrected_srt = response_data['choices'][0]['message']['content'].strip()
            return corrected_srt
        else:
            print(f"OpenAI API Error: {response_data}")
            return raw_srt_text # إعادة النص الأصلي في حال وجود خطأ بالرصيد أو المفتاح
            
    except Exception as e:
        print(f"Error in text correction: {e}")
        # في حال فشل الاتصال، نعيد النص الأصلي لكي لا تتوقف الدبلجة
        return raw_srt_text
