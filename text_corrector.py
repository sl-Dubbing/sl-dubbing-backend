# text_corrector.py
import requests
import os
import time
import logging

logger = logging.getLogger(__name__)

def call_openai_with_retries(payload, headers, max_attempts=3):
    backoff = 1
    for attempt in range(1, max_attempts+1):
        try:
            resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=120)
            if resp.status_code == 429:
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            if attempt == max_attempts:
                raise
            time.sleep(backoff)
            backoff *= 2

def smart_correct_srt(raw_srt_text, api_key=None):
    openai_key = api_key or os.getenv("OPENAI_API_KEY")
    if not openai_key:
        logger.warning("No OPENAI_API_KEY provided. Returning raw text.")
        return raw_srt_text

    system_prompt = """
    أنت مدقق لغوي ومترجم محترف في استوديو دبلجة. سأعطيك نصاً بصيغة SRT يحتوي على توقيتات وجمل.
    مهمتك هي إصلاح النص بناءً على القواعد التالية فقط:
    1. صحح أي أخطاء إملائية أو نحوية في اللغة العربية.
    2. استبدل الكلمات الإنجليزية المكتوبة بحروف عربية إلى أصلها الإنجليزي.
    3. إياك أن تغير أو تحذف أو تعدل الأرقام التسلسلية أو التوقيتات الزمنية. حافظ على هيكل SRT كما هو.
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
        "temperature": 0.1
    }

    try:
        resp = call_openai_with_retries(payload, headers)
        response_data = resp.json()
        choices = response_data.get('choices') or []
        if choices:
            message = choices[0].get('message') or {}
            corrected_srt = message.get('content') or choices[0].get('text') or ""
            corrected_srt = corrected_srt.strip()
            # تحقق بسيط من عدم تغيير عدد التوقيتات
            if raw_srt_text.count("-->") != corrected_srt.count("-->"):
                logger.warning("Timestamp count changed after correction. Returning original SRT.")
                return raw_srt_text
            return corrected_srt if corrected_srt else raw_srt_text
        else:
            logger.warning(f"OpenAI returned no choices: {response_data}")
            return raw_srt_text
    except Exception as e:
        logger.error(f"Error in text correction: {e}")
        return raw_srt_text
