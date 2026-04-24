# models.py
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from uuid import uuid4

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    avatar = db.Column(db.String(500), default='👤')
    credits = db.Column(db.Integer, default=50000)
    password_hash = db.Column(db.String(255), nullable=True)
    auth_method = db.Column(db.String(50), default='oauth')
    last_login = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    jobs = db.relationship('DubbingJob', backref='user', lazy=True, cascade='all, delete-orphan')
    transactions = db.relationship('CreditTransaction', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'name': self.name,
            'avatar': self.avatar,
            'credits': self.credits,
            'auth_method': self.auth_method,
        }

    def __repr__(self):
        return f"<User {self.email}>"


class DubbingJob(db.Model):
    __tablename__ = 'dubbing_jobs'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    status = db.Column(db.String(20), default='pending', index=True)
    language = db.Column(db.String(10), nullable=False)
    voice_mode = db.Column(db.String(50), nullable=False)
    text_length = db.Column(db.Integer, default=0)
    credits_used = db.Column(db.Integer, default=0)
    output_url = db.Column(db.String(1000), nullable=True)
    processing_time = db.Column(db.Float, nullable=True)
    method = db.Column(db.String(50), nullable=True)

    # ✅ حفظ النصوص المترجمة والـ metadata
    extra_data = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'status': self.status,
            'language': self.language,
            'voice_mode': self.voice_mode,
            'credits_used': self.credits_used,
            'output_url': self.output_url,
            'processing_time': self.processing_time,
            'method': self.method,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<DubbingJob {self.id} status={self.status}>"


class CreditTransaction(db.Model):
    __tablename__ = 'credit_transactions'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    transaction_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<CreditTransaction {self.id} {self.transaction_type} {self.amount}>"
