# مقتطف من server.py — استدعاء tts_backend داخل الخلفية
from tts_backend import synthesize_text

def process_tts_sync(payload):
    job_id = payload.get('job_id')
    user_id = payload.get('user_id')
    start_ts = time.time()
    with app.app_context():
        try:
            job = DubbingJob.query.get(job_id)
            user = User.query.get(user_id)
            if not job or not user:
                raise ValueError("Job or user not found")
            text = (payload.get('text') or '').strip()
            srt = (payload.get('srt') or '').strip()
            if not text and srt:
                text = srt
            # synthesize_text returns local mp3 or wav path
            mp_path = synthesize_text(text=text, lang=payload.get('lang','ar'),
                                      voice_mode=payload.get('voice_mode','xtts'),
                                      voice_id=payload.get('voice_id',''),
                                      voice_url=payload.get('voice_url',''))
            # upload to cloudinary or move to AUDIO_DIR
            if CLOUDINARY_AVAILABLE:
                resp = cloudinary.uploader.upload(mp_path, resource_type='auto', folder='sl-dubbing/audio', public_id=f"tts_{job_id}", overwrite=True)
                audio_url = resp.get('secure_url') or resp.get('url')
            else:
                dest = AUDIO_DIR / f"dub_{job_id}.mp3"
                Path(mp_path).rename(dest)
                audio_url = f"file://{dest}"
            job.output_url = audio_url
            job.status = 'completed'
            job.processing_time = time.time() - start_ts
            db.session.add(job)
            db.session.commit()
        except Exception as e:
            # refund and mark failed
            if job:
                job.status = 'failed'
                db.session.add(job)
            if job and job.credits_used:
                u = User.query.get(job.user_id)
                if u:
                    u.credits += job.credits_used
                    db.session.add(CreditTransaction(user_id=u.id, transaction_type='refund', amount=job.credits_used, reason='Dubbing failed'))
            db.session.commit()
