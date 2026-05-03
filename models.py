from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255))
    avatar = db.Column(db.String(500))
    avatar_key = db.Column(db.String(500))
    auth_method = db.Column(db.String(50), default='supabase')
    supabase_id = db.Column(db.String(255), unique=True, index=True)
    credits = db.Column(db.Integer, default=1000)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'name': self.name,
            'avatar': self.avatar,
            'credits': self.credits or 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class DubbingJob(db.Model):
    __tablename__ = 'dubbing_jobs'

    # 🔑 String ID (UUID) — يطابق app.py
    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    # 🔑 الحقول المتوقعة من app.py:
    language = db.Column(db.String(20), index=True)        # ليس "lang"
    method = db.Column(db.String(50), default='dubbing')   # ليس "kind"
    voice_id = db.Column(db.String(100))
    engine = db.Column(db.String(50))
    status = db.Column(db.String(50), default='pending', index=True)
    file_key = db.Column(db.Text)                           # input_key
    output_url = db.Column(db.Text)                         # audio_url
    error_message = db.Column(db.Text)                      # error
    credits_used = db.Column(db.Integer, default=0)

    # ميزات إضافية:
    custom_name = db.Column(db.Text)
    folder_name = db.Column(db.Text, default='الرئيسية')

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    completed_at = db.Column(db.DateTime)


class CreditTransaction(db.Model):
    __tablename__ = 'credit_transactions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    amount = db.Column(db.Integer)
    transaction_type = db.Column(db.String(20))   # 'debit' or 'refund'
    job_id = db.Column(db.String(36))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
