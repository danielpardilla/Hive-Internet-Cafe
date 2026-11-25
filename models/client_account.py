from .database import db
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone, timedelta

_PH = timezone(timedelta(hours=8))
def _ph_now():
    return datetime.now(_PH).replace(tzinfo=None)

class ClientAccount(db.Model):
    """Admin-created client accounts, optionally linked to a PC."""
    __tablename__ = "client_accounts"

    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(64), nullable=False)           # customer full name
    pc_id        = db.Column(db.Integer, db.ForeignKey("pcs.id"), nullable=True)  # NULL = login from any PC
    password_hash= db.Column(db.String(256), nullable=False)
    active       = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=_ph_now)

    pc = db.relationship("PC", backref="client_accounts")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id":         self.id,
            "name":       self.name,
            "pc_label":   self.pc.label if self.pc else "—",
            "pc_id":      self.pc_id,
            "active":     self.active,
            "created_at": self.created_at.strftime("%b %d %Y %I:%M %p") if self.created_at else None,
        }

    def __repr__(self):
        return f"<ClientAccount {self.name} @ {self.pc.label if self.pc else '?'}>"
