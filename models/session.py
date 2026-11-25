from .database import db
from datetime import datetime, timezone, timedelta

RATE_PER_HOUR = 20.00   # ₱ default rate

_PH = timezone(timedelta(hours=8))

def _ph_now():
    return datetime.now(_PH).replace(tzinfo=None)

def _fmt_ph(dt):
    """Format a stored datetime (assumed PH time already) for display."""
    if dt is None:
        return None
    return dt.strftime("%b %d %I:%M %p")

class Session(db.Model):
    __tablename__ = "sessions"

    id             = db.Column(db.Integer, primary_key=True)
    session_code   = db.Column(db.String(16), unique=True, nullable=False)  # e.g. SES-000001

    pc_id          = db.Column(db.Integer, db.ForeignKey("pcs.id"), nullable=False)
    customer_name  = db.Column(db.String(64), nullable=False)

    started_at     = db.Column(db.DateTime, nullable=True)
    ended_at       = db.Column(db.DateTime, nullable=True)

    rate_per_hour  = db.Column(db.Float, default=RATE_PER_HOUR)
    internet_charge= db.Column(db.Float, default=0.0)
    print_charge   = db.Column(db.Float, default=0.0)
    total_charge   = db.Column(db.Float, default=0.0)

    status         = db.Column(db.String(16), default="pending")
    # status: pending | active | ended | paid

    paid           = db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship to print jobs
    print_jobs     = db.relationship("PrintJob", backref="session", lazy=True)

    @property
    def elapsed_seconds(self):
        if not self.started_at:
            return 0
        end = self.ended_at or _ph_now()
        return int((end - self.started_at).total_seconds())

    @property
    def elapsed_hours(self):
        return self.elapsed_seconds / 3600

    @property
    def current_internet_charge(self):
        return round(self.elapsed_hours * self.rate_per_hour, 2)

    @property
    def current_total(self):
        return round(self.current_internet_charge + self.print_charge, 2)

    def to_dict(self):
        return {
            "id":             self.id,
            "session_code":   self.session_code,
            "pc_label":       self.pc.label if self.pc else "",
            "pc_id":          self.pc_id,
            "customer_name":  self.customer_name,
            "started_at":     _fmt_ph(self.started_at),
            "ended_at":       _fmt_ph(self.ended_at),
            "elapsed_seconds":self.elapsed_seconds,
            "rate_per_hour":  self.rate_per_hour,
            "internet_charge":self.current_internet_charge,
            "print_charge":   self.print_charge,
            "total":          self.current_total,
            "status":         self.status,
            "paid":           self.paid,
        }

    def __repr__(self):
        return f"<Session {self.session_code} [{self.status}]>"
