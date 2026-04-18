# text_corrector.py
import os
import openai

# نقوم بإعداد مفتاح الـ API الخاص بـ OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

def smart_correct_srt(raw_srt_text):
    """
    هذه الدالة تأخذ نص الـ SRT الخام، وتصححه، وتعيده بنفس الهيكلة
    """
    
    system_prompt = """
    أنت مدقق لغوي ومترجم محترف في استوديو دبلجة. سأعطيك نصاً بصيغة SRT يحتوي على توقيتات وجمل.
    مهمتك هي إصلاح النص بناءً على القواعد التالية فقط:
    1. صحح أي أخطاء إملائية أو نحوية في اللغة العربية.
    2. استبدل الكلمات الإنجليزية المكتوبة بحروف عربية إلى أصلها الإنجليزي (مثال: "تايم لاين" تصبح "Timeline"، "لابتوب" تصبح "Laptop").
    3. **قاعدة صارمة:** إياك أن تغير أو تحذف أو تعدل الأرقام التسلسلية أو التوقيتات الزمنية (التي تحتوي على أسهم -->). حافظ على هيكل SRT كما هو بالضبط بنسبة 100%.
    """

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo", # أو gpt-4 لنتائج أدق
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"قم بتصحيح هذا النص:\n\n{raw_srt_text}"}
            ],
            temperature=0.1 # درجة حرارة منخفضة جداً لكي لا يضيف الذكاء الاصطناعي كلاماً من عنده
        )
        
        corrected_srt = response.choices[0].message.content.strip()
        return corrected_srt
        
    except Exception as e:
        print(f"Error in text correction: {e}")
        # في حال فشل الاتصال، نعيد النص الأصلي لكي لا تتوقف الدبلجة
        return raw_srt_text
