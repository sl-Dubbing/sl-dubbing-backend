from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(120))
    supabase_id = db.Column(db.String(120), unique=True)
    credits = db.Column(db.Integer, default=10)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class DubbingJob(db.Model):
    __tablename__ = 'dubbing_jobs'
    
    # ✅ تم الإصلاح هنا: تحويل id إلى String ليتوافق مع قاعدة البيانات
    id = db.Column(db.String(36), primary_key=True)
    
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    kind = db.Column(db.String(50), default='dub') 
    lang = db.Column(db.String(20))
    voice_id = db.Column(db.String(100))
    engine = db.Column(db.String(50))
    status = db.Column(db.String(50), default='queued')
    input_key = db.Column(db.Text)
    audio_url = db.Column(db.Text)
    error = db.Column(db.Text)
    
    # الأعمدة الجديدة الخاصة بمدير الملفات
    custom_name = db.Column(db.Text)
    folder_name = db.Column(db.Text, default='الرئيسية')
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
