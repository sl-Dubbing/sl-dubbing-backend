# Dockerfile — Python 3.11 slim مع أدوات نظامية لبناء الحزم الصوتية
FROM python:3.11-slim

# تثبيت أدوات نظامية مطلوبة لبناء بعض الحزم وصيغ الصوت
RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    libsndfile1 \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# انسخ ملف الاعتمادات وثبّت الحزم
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# انسخ باقي المشروع
COPY . .

# أمر افتراضي لتشغيل الويب؛ عامل Celery يُشغّل كخدمة منفصلة عبر Procfile أو Start Command
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--workers", "4"]
