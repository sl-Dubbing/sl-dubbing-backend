FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg build-essential libsndfile1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg build-essential libsndfile1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# لا حاجة لـ ENV PORT=5000 هنا لأن Railway سيعطيك بورت تلقائياً
# EXPOSE 5000 أيضاً غير ضرورية تماماً في Railway، يمكنك تركها أو حذفها

CMD gunicorn server:app -b 0.0.0.0:$PORT --workers 1 --threads 4
