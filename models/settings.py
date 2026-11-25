from .database import db

class Settings(db.Model):
    """Key-value store for runtime configuration like billing rates."""
    __tablename__ = "settings"
    key   = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(256), nullable=False)

    @staticmethod
    def get(key, default=None):
        row = Settings.query.filter_by(key=key).first()
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = Settings.query.filter_by(key=key).first()
        if row:
            row.value = str(value)
        else:
            db.session.add(Settings(key=key, value=str(value)))
        db.session.commit()
