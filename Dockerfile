# استخدام نسخة بايثون خفيفة ومستقرة
FROM python:3.10-slim

# تحديث النظام وتثبيت ffmpeg بطريقة آمنة
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# تحديد مجلد العمل داخل السيرفر
WORKDIR /app

# نسخ جميع ملفات المشروع إلى السيرفر
COPY . .

# تثبيت مكتبات البايثون
RUN pip install --no-cache-dir -r requirements.txt

# فتح البورت
EXPOSE 5000

# أمر تشغيل السيرفر
CMD ["python", "server.py"]
