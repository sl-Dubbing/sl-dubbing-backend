from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=True)
    avatar = db.Column(db.String(500), nullable=True)
    avatar_key = db.Column(db.String(500), nullable=True)
    auth_method = db.Column(db.String(50), default='supabase', nullable=False)
    supabase_id = db.Column(db.String(255), unique=True, index=True, nullable=True)
    credits = db.Column(db.Integer, default=1000, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # علاقات مفيدة
    jobs = db.relationship('DubbingJob', backref='user', lazy='dynamic')
    transactions = db.relationship('CreditTransaction', backref='user', lazy='dynamic')

    def to_dict(self):
        avatar_url = None
        if self.avatar_key:
            avatar_url = self.avatar_key  # تحويل لاحقًا إلى URL في app إذا لزم
        elif self.avatar:
            avatar_url = self.avatar
        return {
            'id': self.id,
            'email': self.email,
            'name': self.name,
            'avatar': avatar_url,
            'credits': self.credits or 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<User id={self.id} email={self.email}>"

class DubbingJob(db.Model):
    __tablename__ = 'dubbing_jobs'
    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    language = db.Column(db.String(20), index=True, nullable=True)
    method = db.Column(db.String(50), default='dubbing', nullable=False)
    voice_id = db.Column(db.String(100), nullable=True)
    engine = db.Column(db.String(50), nullable=True)
    status = db.Column(db.String(50), default='pending', index=True, nullable=False)
    file_key = db.Column(db.Text, nullable=True)
    output_url = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    credits_used = db.Column(db.Integer, default=0, nullable=False)
    custom_name = db.Column(db.Text, nullable=True)
    folder_name = db.Column(db.Text, default='الرئيسية', nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<DubbingJob id={self.id} user_id={self.user_id} status={self.status}>"

class CreditTransaction(db.Model):
    __tablename__ = 'credit_transactions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    transaction_type = db.Column(db.String(20), nullable=False)   # 'debit' or 'refund'
    job_id = db.Column(db.String(36), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<CreditTransaction id={self.id} user_id={self.user_id} amount={self.amount}>"
