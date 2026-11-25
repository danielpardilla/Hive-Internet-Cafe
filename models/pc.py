from .database import db
from datetime import datetime

class PC(db.Model):
    __tablename__ = "pcs"

    id         = db.Column(db.Integer, primary_key=True)
    pc_number  = db.Column(db.Integer, unique=True, nullable=False)  # 1-based
    label      = db.Column(db.String(16), unique=True, nullable=False)  # e.g. "PC-01"
    status     = db.Column(db.String(16), default="available")
    # status: available | occupied | reserved | maintenance

    socket_sid = db.Column(db.String(64), nullable=True)   # current SocketIO SID
    last_seen  = db.Column(db.DateTime, nullable=True)

    # Relationship: all sessions on this PC
    sessions   = db.relationship("Session", backref="pc", lazy=True)

    @property
    def active_session(self):
        from .session import Session
        return Session.query.filter_by(pc_id=self.id, status="active").first()

    def __repr__(self):
        return f"<PC {self.label} [{self.status}]>"
