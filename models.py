# models.py — V2.0 (المستخدمون في Supabase، الجوبات هنا فقط)
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class DubbingJob(db.Model):
    __tablename__ = 'dubbing_jobs'
    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.String(255), nullable=False, index=True)  # Supabase UUID
    language = db.Column(db.String(20), index=True, nullable=True)
    status = db.Column(db.String(50), default='pending', index=True, nullable=False)
    output_url = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    credits_used = db.Column(db.Integer, default=0, nullable=False)
    file_key = db.Column(db.Text, nullable=True)
    engine = db.Column(db.String(50), nullable=True)
    custom_name = db.Column(db.Text, nullable=True)
    folder_name = db.Column(db.Text, default='الرئيسية', nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)


# للحفاظ على import compatibility مع tasks.py القديم
class CreditTransaction(db.Model):
    __tablename__ = 'credit_transactions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(255), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    transaction_type = db.Column(db.String(20), nullable=False)
    job_id = db.Column(db.String(36), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
