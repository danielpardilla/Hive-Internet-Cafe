from .database import db
from datetime import datetime, timezone, timedelta

_PH = timezone(timedelta(hours=8))

def _ph_now():
    return datetime.now(_PH).replace(tzinfo=None)

# Print rates
RATE_BW    = 3.00   # ₱ per page B&W
RATE_COLOR = 5.00   # ₱ per page Color

class PrintJob(db.Model):
    __tablename__ = "print_jobs"

    id             = db.Column(db.Integer, primary_key=True)
    session_id     = db.Column(db.Integer, db.ForeignKey("sessions.id"), nullable=True)
    pc_label       = db.Column(db.String(16),  nullable=False, default="PC-??")
    customer_name  = db.Column(db.String(64),  nullable=False, default="Unknown")
    filename       = db.Column(db.String(256), nullable=False)
    file_path      = db.Column(db.String(512), nullable=True)
    file_type      = db.Column(db.String(16),  default="unknown")

    # Page info
    pages          = db.Column(db.Integer, default=1)   # total pages in file
    page_range     = db.Column(db.String(32), default="All")  # e.g. 'All', '1-3', '5'
    targeted_pages = db.Column(db.Integer, default=1)   # pages to actually print

    # Print options
    copies_count   = db.Column(db.Integer, default=1)
    color_mode     = db.Column(db.String(8), default="BW")    # BW | Color
    paper_size     = db.Column(db.String(8), default="Short") # Short | A4 | Long

    # Billing
    charge         = db.Column(db.Float, default=0.0)
    note           = db.Column(db.Text, nullable=True)  # customer note/comment

    # Status
    status         = db.Column(db.String(16), default="pending")
    queue_position = db.Column(db.Integer, default=0)
    created_at     = db.Column(db.DateTime, default=_ph_now)
    processed_at   = db.Column(db.DateTime, nullable=True)

    def compute_charge(self):
        """targeted_pages × copies × rate (BW=₱3, Color=₱5)"""
        rate = RATE_COLOR if self.color_mode == "Color" else RATE_BW
        return round(self.targeted_pages * self.copies_count * rate, 2)

    def to_dict(self):
        return {
            "id":             self.id,
            "session_id":     self.session_id,
            "pc_label":       self.pc_label,
            "customer_name":  self.customer_name,
            "filename":       self.filename,
            "file_type":      self.file_type,
            "pages":          self.pages,
            "page_range":     self.page_range,
            "targeted_pages": self.targeted_pages,
            "copies_count":   self.copies_count,
            "color_mode":     self.color_mode,
            "paper_size":     self.paper_size,
            "charge":         self.charge,
            "note":           self.note or "",
            "status":         self.status,
            "queue_position": self.queue_position,
            "created_at":     self.created_at.strftime("%I:%M %p") if self.created_at else None,
            "processed_at":   self.processed_at.strftime("%I:%M %p") if self.processed_at else None,
        }
