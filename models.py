# models.py — مُعدّل (توحيد الأسماء، avatar_key، job_id في المعاملات)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from uuid import uuid4

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    # معرف Supabase (قد يكون null للحسابات غير المرتبطة)
    supabase_id = db.Column(db.String(128), unique=True, nullable=True, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    # مفتاح الصورة في R2 (يفضل تخزين المفتاح وليس URL كامل)
    avatar_key = db.Column(db.String(255), nullable=True, index=True)
    # للحفاظ على التوافق مع أي استخدام سابق لحقل avatar كـ URL أو رمز افتراضي
    avatar = db.Column(db.String(1000), nullable=True, default=None)
    # قيمة افتراضية معقولة؛ عدّلها حسب سياسة المنتج
    credits = db.Column(db.Integer, default=500)
    password_hash = db.Column(db.String(255), nullable=True)
    auth_method = db.Column(db.String(50), default='oauth')
    last_login = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    jobs = db.relationship('DubbingJob', backref='user', lazy=True, cascade='all, delete-orphan')
    transactions = db.relationship('CreditTransaction', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'supabase_id': self.supabase_id,
            'email': self.email,
            'name': self.name,
            'avatar_key': self.avatar_key,
            'avatar': self.avatar,
            'credits': self.credits,
            'auth_method': self.auth_method,
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<User {self.email}>"


class DubbingJob(db.Model):
    __tablename__ = 'dubbing_jobs'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    status = db.Column(db.String(20), default='pending', index=True)
    language = db.Column(db.String(10), nullable=False, default='en')
    # voice_mode قد لا يُمرّر دائمًا من الواجهات؛ نجعله قابلًا لأن يكون null مع قيمة افتراضية عند الحاجة
    voice_mode = db.Column(db.String(50), nullable=True, default='source')
    text_length = db.Column(db.Integer, default=0)
    credits_used = db.Column(db.Integer, default=0, nullable=False)
    output_url = db.Column(db.String(2000), nullable=True)
    processing_time = db.Column(db.Float, nullable=True)
    method = db.Column(db.String(50), nullable=True)
    extra_data = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'status': self.status,
            'language': self.language,
            'voice_mode': self.voice_mode,
            'text_length': self.text_length,
            'credits_used': self.credits_used,
            'output_url': self.output_url,
            'processing_time': self.processing_time,
            'method': self.method,
            'extra_data': self.extra_data,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<DubbingJob {self.id} status={self.status}>"


class CreditTransaction(db.Model):
    __tablename__ = 'credit_transactions'
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    # ربط المعاملة بمهمة اختيارية لتتبع المدفوعات والاستردادات
    job_id = db.Column(db.String(36), db.ForeignKey('dubbing_jobs.id'), nullable=True, index=True)
    transaction_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    # اجعل السبب اختياريًا لكن لا تتركه فارغًا في الاستخدام الفعلي
    reason = db.Column(db.String(200), nullable=True, default=None)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<CreditTransaction {self.id} {self.transaction_type} {self.amount}>"
