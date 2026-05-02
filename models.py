# models.py — مع Supabase + Direct Upload
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255))
    supabase_id = db.Column(db.String(255), unique=True, index=True)
    credits = db.Column(db.Integer, default=10)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class DubbingJob(db.Model):
    __tablename__ = 'dubbing_jobs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    kind = db.Column(db.String(20), default='dub')
    lang = db.Column(db.String(10))
    voice_id = db.Column(db.String(100))
    engine = db.Column(db.String(50))
    status = db.Column(db.String(20), default='queued', index=True)
    input_key = db.Column(db.String(500))
    audio_url = db.Column(db.String(1000))
    error = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    completed_at = db.Column(db.DateTime)


class CreditTransaction(db.Model):
    __tablename__ = 'credit_transactions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    amount = db.Column(db.Integer)
    reason = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
