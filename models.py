from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    # 🔴 التعديل الجذري: تحويل ID إلى String لاستقبال UUID من Supabase
    id = db.Column(db.String(255), primary_key=True) 
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=True)
    avatar = db.Column(db.String(500), nullable=True)
    avatar_key = db.Column(db.String(500), nullable=True)
    auth_method = db.Column(db.String(50), default='supabase', nullable=False)
    supabase_id = db.Column(db.String(255), unique=True, index=True, nullable=True)
    credits = db.Column(db.Integer, default=1000, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # تحديث العلاقات لتناسب النوع النصي الجديد
    jobs = db.relationship('DubbingJob', backref='user', lazy='dynamic')
    transactions = db.relationship('CreditTransaction', backref='user', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'name': self.name,
            'avatar': self.avatar_key or self.avatar,
            'credits': self.credits or 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

class DubbingJob(db.Model):
    __tablename__ = 'dubbing_jobs'
    id = db.Column(db.String(36), primary_key=True)
    # 🔴 التعديل: يجب أن يكون نوع ForeignKey مطابقاً لـ User.id
    user_id = db.Column(db.String(255), db.ForeignKey('users.id'), nullable=False, index=True)
    language = db.Column(db.String(20), index=True, nullable=True)
    status = db.Column(db.String(50), default='pending', index=True, nullable=False)
    output_url = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    credits_used = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True, nullable=False)

class CreditTransaction(db.Model):
    __tablename__ = 'credit_transactions'
    id = db.Column(db.Integer, primary_key=True)
    # 🔴 التعديل: مطابقة نوع البيانات
    user_id = db.Column(db.String(255), db.ForeignKey('users.id'), index=True, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    transaction_type = db.Column(db.String(20), nullable=False)
    job_id = db.Column(db.String(36), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
