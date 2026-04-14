FROM python:3.10-slim

RUN apt-get update && apt-get install -y ffmpeg git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# استنساخ محرك CosyVoice من المستودع الرسمي وتثبيت متطلباته الأساسية
RUN git clone https://github.com/FunAudioLLM/CosyVoice.git
ENV PYTHONPATH="/app/CosyVoice:$PYTHONPATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000
CMD ["python", "server.py"]
