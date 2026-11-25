"""
ICMS-ASI — Phase 4 Final
All fixes:
  1. Rates stored in DB (Settings table) — persist across restarts, always live
  2. rates_updated socket event pushes to ALL pages (sessions, billing, print, client)
  3. Multi-file upload queue on client side
  4. File download endpoint for admin
  5. Client can request session end with confirmation
  6. Admin notified when client requests session end
"""

import os, threading, time, uuid
from datetime import datetime, date, timedelta, timezone, time as dtime
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

from models.database import db
from models.user      import User
from models.pc        import PC
from models.session   import Session
from models.print_job import PrintJob, RATE_BW, RATE_COLOR
from models.settings  import Settings
from models.client_account import ClientAccount

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "icms-asi-secret-2026")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR,'instance','icms.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"]      = {
    "connect_args": {"check_same_thread": False},
    "pool_pre_ping": True,
}
app.config["UPLOAD_FOLDER"]        = os.path.join(BASE_DIR, "uploads")
app.config["MAX_CONTENT_LENGTH"]   = 20 * 1024 * 1024
# LAN multi-client session stability
app.config["SESSION_COOKIE_SAMESITE"]    = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"]    = True
app.config["PERMANENT_SESSION_LIFETIME"] = 86400   # 24 h
ALLOWED_EXTENSIONS = {"pdf","docx","jpg","jpeg","png"}

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                   ping_interval=20, ping_timeout=40,   # LAN: keep connections alive
                    logger=False, engineio_logger=False)

connected_clients = {}
pc_sid_map        = {}

# ── Philippine Time ───────────────────────────────────────────────────────────
_PH = timezone(timedelta(hours=8))
def ph_now():
    return datetime.now(_PH).replace(tzinfo=None)

# ── Rate helpers (always read from DB) ───────────────────────────────────────
def get_rate_internet():
    return float(Settings.get("internet_rate", "20.0"))
def get_rate_bw():
    return float(Settings.get("print_rate_bw", str(RATE_BW)))
def get_rate_color():
    return float(Settings.get("print_rate_color", str(RATE_COLOR)))
def get_rates():
    return {"internet_rate": get_rate_internet(),
            "print_rate_bw": get_rate_bw(),
            "print_rate_color": get_rate_color()}

# ── Login ─────────────────────────────────────────────────────────────────────
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ── DB Init ───────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.join(BASE_DIR,"instance"), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    db.create_all()
    # Enable WAL mode for SQLite — allows concurrent reads + writes from multiple clients
    try:
        db.session.execute(db.text("PRAGMA journal_mode=WAL"))
        db.session.execute(db.text("PRAGMA synchronous=NORMAL"))
        db.session.execute(db.text("PRAGMA busy_timeout=5000"))  # 5s wait on lock
        db.session.commit()
    except Exception as e:
        print(f"[DB] WAL mode warning: {e}")
    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", role="admin")
        admin.set_password("admin123")
        db.session.add(admin)
    if PC.query.count() == 0:
        for i in range(1,16):
            db.session.add(PC(pc_number=i, label=f"PC-{i:02d}", status="available"))
    # Seed default rates if not set
    for key, default in [("internet_rate","20.0"),("print_rate_bw","3.0"),("print_rate_color","5.0")]:
        if not Settings.query.filter_by(key=key).first():
            db.session.add(Settings(key=key, value=default))
    db.session.commit()
    print("[DB] Database initialized.")

# ── Helpers ───────────────────────────────────────────────────────────────────
def _pc_grid_data():
    pcs = PC.query.order_by(PC.pc_number).all()
    return [{"id":pc.id,"label":pc.label,"status":pc.status,
             "connected":pc.label in pc_sid_map,
             "session":pc.active_session.to_dict() if pc.active_session else None}
            for pc in pcs]

def _broadcast_pc_update():
    with app.app_context():
        data = _pc_grid_data()
    socketio.emit("pc_update", data)

def _broadcast_rates():
    """Push current rates to ALL connected pages."""
    with app.app_context():
        rates = get_rates()
    socketio.emit("rates_updated", rates)

def _daily_revenue(target_date=None):
    if target_date is None: target_date = ph_now().date()
    day_start = datetime.combine(target_date, dtime.min)
    day_end   = datetime.combine(target_date, dtime.max)
    rows = Session.query.filter(Session.status=="paid",
                                Session.ended_at.between(day_start,day_end)).all()
    internet = sum(r.internet_charge for r in rows)
    printing = sum(r.print_charge    for r in rows)
    return round(internet,2), round(printing,2), round(internet+printing,2)

def _allowed(filename):
    return "." in filename and filename.rsplit(".",1)[1].lower() in ALLOWED_EXTENSIONS

def _count_pages(filepath, ext):
    try:
        if ext == "pdf":
            import PyPDF2
            with open(filepath,"rb") as f:
                return max(1, len(PyPDF2.PdfReader(f).pages))
        elif ext == "docx":
            import docx as docxlib
            doc   = docxlib.Document(filepath)
            lines = sum(1 for p in doc.paragraphs if p.text.strip())
            return max(1, round(lines/40) or 1)
        else:
            return 1
    except Exception:
        return 1

def _resolve_targeted_pages(total_pages, page_range):
    pr = (page_range or "All").strip()
    if pr.lower() == "all": return total_pages
    count = 0
    for part in pr.split(","):
        part = part.strip()
        if "-" in part:
            a,b = part.split("-",1)
            try: count += max(0, min(total_pages,int(b.strip())) - max(1,int(a.strip())) + 1)
            except ValueError: pass
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages: count += 1
            except ValueError: pass
    return max(1, count) if count > 0 else total_pages

def _print_queue_count():
    return PrintJob.query.filter(PrintJob.status.in_(["pending","processing"])).count()

# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index(): return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    from flask import session as fs
    error = None
    if request.method == "POST":
        # Single form — name field doubles as admin username
        name     = request.form.get("name","").strip()       # used for both admin+client
        password = request.form.get("password","").strip()
        pc_id    = request.form.get("pc_id","").strip()

        if not name or not password:
            error = "Please enter your name and password."
        else:
            # 1. Try admin login first (name = admin username)
            admin_user = User.query.filter_by(username=name).first()
            if admin_user and admin_user.check_password(password):
                login_user(admin_user)
                print(f"[AUTH] Admin '{name}' logged in from {request.remote_addr}")
                return redirect(url_for("admin_dashboard"))

            # 2. Try client login — match by pc_id + name (case-insensitive, first name OK)
            # 2. Try client login — require a PC to be selected
            if not pc_id:
                error = "Please select the PC you are sitting at."
            else:
                try:
                    pc_id_int = int(pc_id)
                except ValueError:
                    error = "Invalid PC selection."
                else:
                    pc = db.session.get(PC, pc_id_int)
                    if not pc:
                        error = "Selected PC not found."
                    else:
                        # Match PC-specific accounts first, then Any-PC (unassigned) accounts
                        pc_accounts  = ClientAccount.query.filter_by(pc_id=pc_id_int, active=True).all()
                        any_accounts = ClientAccount.query.filter(
                            ClientAccount.pc_id == None,  # noqa: E711 — IS NULL
                            ClientAccount.active == True
                        ).all()
                        accounts = pc_accounts + any_accounts
                        matched  = None
                        for acc in accounts:
                            acc_first = acc.name.strip().split()[0].lower()
                            inp_first = name.strip().split()[0].lower()
                            if acc_first == inp_first or acc.name.lower() == name.lower():
                                if acc.check_password(password):
                                    matched = acc
                                    break
                        if matched:
                            # Always store the actual PC label selected — works for both
                            # PC-specific accounts and Any-PC (shared) accounts
                            fs["client_name"]       = matched.name
                            fs["client_account_id"] = matched.id
                            fs["pc_label"]          = pc.label
                            fs["role"]              = "client"
                            acct_type = "shared" if matched.pc_id is None else "PC-specific"
                            print(f"[AUTH] Client '{matched.name}' ({acct_type}) logged in on {pc.label}")
                            return redirect(url_for("client_app"))
                        else:
                            error = "Incorrect name or password. Ask the admin if you don't have an account."

    pcs = PC.query.order_by(PC.pc_number).all()
    return render_template("login.html", error=error, pcs=pcs)

@app.route("/logout")
def logout():
    from flask import session as fs
    logout_user(); fs.clear()
    return redirect(url_for("login"))

# ── Admin Pages ───────────────────────────────────────────────────────────────
@app.route("/admin")
@login_required
def admin_dashboard():
    return render_template("admin/dashboard.html", user=current_user.username)

@app.route("/admin/sessions")
@login_required
def admin_sessions():
    return render_template("admin/sessions.html", user=current_user.username)

@app.route("/admin/client-accounts")
@login_required
def admin_client_accounts():
    return render_template("admin/client_accounts.html", user=current_user.username)

@app.route("/admin/billing")
@login_required
def admin_billing():
    return render_template("admin/billing.html", user=current_user.username)

@app.route("/admin/print-queue")
@login_required
def admin_print_queue():
    return render_template("admin/print_queue.html", user=current_user.username)

@app.route("/admin/reports")
@login_required
def admin_reports():
    return render_template("admin/reports.html", user=current_user.username)

@app.route("/admin/ai-insights")
@login_required
def admin_ai_insights():
    return render_template("admin/ai_insights.html", user=current_user.username)

@app.route("/client")
def client_app():
    from flask import session as fs
    if fs.get("role") != "client": return redirect(url_for("login"))
    return render_template("client/app.html",
                           user=fs.get("client_name","Guest"),
                           pc_label=fs.get("pc_label","PC-01"))

# ── Client session restore ────────────────────────────────────────────────────
@app.route("/api/client/session")
def api_client_session():
    from flask import session as fs
    if fs.get("role") != "client": return jsonify({"active":False})
    # Use the stored pc_label from login (accurate, not derived from name)
    pc_label = fs.get("pc_label","")
    if not pc_label:
        # fallback: derive from name
        client_name = fs.get("client_name","")
        pc_label = client_name.upper() if client_name.upper().startswith("PC-") \
                   else "PC-" + client_name[:3].upper()
    pc = PC.query.filter_by(label=pc_label).first()
    if not pc: return jsonify({"active":False,"pc_label":pc_label,**get_rates()})
    sess = pc.active_session
    rates = get_rates()
    if not sess: return jsonify({"active":False,"pc_label":pc_label,**rates})
    return jsonify({"active":True,"pc_label":pc_label,"session":sess.to_dict(),**rates})

# ── General API ───────────────────────────────────────────────────────────────
@app.route("/api/ping")
def ping(): return jsonify({"status":"ok","version":"4.2"})

@app.route("/api/status")
def server_status():
    return jsonify({"connected_clients":len(connected_clients),
                    "clients":list(connected_clients.values())})

@app.route("/api/pcs")
@login_required
def api_pcs(): return jsonify(_pc_grid_data())

@app.route("/api/rates")
def api_rates():
    """Public endpoint — any page can fetch current rates."""
    return jsonify(get_rates())

# ── Session API ───────────────────────────────────────────────────────────────
@app.route("/api/session/start", methods=["POST"])
@login_required
def api_session_start():
    data = request.get_json()
    pc_id = data.get("pc_id"); customer_name = data.get("customer_name","").strip()
    if not pc_id or not customer_name:
        return jsonify({"error":"pc_id and customer_name required"}), 400
    pc = db.session.get(PC, pc_id)
    if not pc: return jsonify({"error":"PC not found"}), 404
    if pc.status == "occupied": return jsonify({"error":f"{pc.label} is already occupied"}), 409
    rate  = float(data.get("rate_per_hour", get_rate_internet()))
    count = Session.query.count() + 1
    sess  = Session(session_code=f"SES-{count:06d}", pc_id=pc.id,
                    customer_name=customer_name, started_at=ph_now(),
                    status="active", rate_per_hour=rate)
    pc.status = "occupied"
    db.session.add(sess); db.session.commit()
    payload = sess.to_dict(); target_sid = pc_sid_map.get(pc.label)
    print(f"[SESSION] {sess.session_code} STARTED — {customer_name} | {pc.label} | ₱{rate}/hr")
    def _e():
        if target_sid: socketio.emit("session_started", payload, to=target_sid)
        _broadcast_pc_update()
    socketio.start_background_task(_e)
    return jsonify({"success":True,"session":payload})

@app.route("/api/session/end", methods=["POST"])
@login_required
def api_session_end():
    data = request.get_json()
    sess = db.session.get(Session, data.get("session_id"))
    if not sess: return jsonify({"error":"Session not found"}), 404
    if sess.status != "active": return jsonify({"error":"Session is not active"}), 409
    sess.ended_at = ph_now()
    sess.internet_charge = sess.current_internet_charge
    sess.total_charge    = sess.current_total
    sess.status = "ended"; sess.pc.status = "available"
    db.session.commit()
    payload = sess.to_dict(); target_sid = pc_sid_map.get(sess.pc.label)
    dur = f"{sess.elapsed_seconds//3600:02d}:{(sess.elapsed_seconds%3600)//60:02d}:{sess.elapsed_seconds%60:02d}"
    print(f"[SESSION] {sess.session_code} ENDED — {sess.customer_name} | {sess.pc.label} | {dur} | ₱{sess.total_charge:.2f}")
    def _e():
        if target_sid: socketio.emit("session_ended", payload, to=target_sid)
        _broadcast_pc_update()
    socketio.start_background_task(_e)
    return jsonify({"success":True,"session":payload})

# Client-initiated session end request
@app.route("/api/session/client-end-request", methods=["POST"])
def api_client_end_request():
    """Client requests to end their own session — notifies admin."""
    data = request.get_json()
    session_id = data.get("session_id")
    pc_label   = data.get("pc_label","")
    sess = db.session.get(Session, session_id)
    if not sess or sess.status != "active":
        return jsonify({"error":"No active session"}), 404
    payload = {**sess.to_dict(), "requested_by": "client"}
    print(f"[SESSION] {sess.session_code} — Client requested end | {pc_label} — notifying {len(connected_clients)} connected client(s)")
    # Emit directly (we're already in a request context thread — no background task needed)
    socketio.emit("client_end_request", payload)
    return jsonify({"success":True})

@app.route("/api/session/extend", methods=["POST"])
@login_required
def api_session_extend():
    data = request.get_json()
    sess = db.session.get(Session, data.get("session_id"))
    if not sess or sess.status != "active":
        return jsonify({"error":"Active session not found"}), 404
    minutes = int(data.get("minutes",30))
    payload = {**sess.to_dict(),"extended_minutes":minutes}
    target_sid = pc_sid_map.get(sess.pc.label)
    def _e():
        if target_sid: socketio.emit("session_extended", payload, to=target_sid)
    socketio.start_background_task(_e)
    return jsonify({"success":True,"session":payload})

@app.route("/api/sessions/active")
@login_required
def api_active_sessions():
    return jsonify([s.to_dict() for s in Session.query.filter_by(status="active").all()])

@app.route("/api/sessions/history")
@login_required
def api_sessions_history():
    return jsonify([s.to_dict() for s in
                    Session.query.filter(Session.status!="active")
                                 .order_by(Session.ended_at.desc()).limit(100).all()])

# ── Billing API ───────────────────────────────────────────────────────────────
@app.route("/api/billing/summary")
@login_required
def api_billing_summary():
    internet,printing,total = _daily_revenue()
    active   = Session.query.filter_by(status="active").all()
    unpaid   = Session.query.filter_by(status="ended",paid=False).count()
    paid_cnt = Session.query.filter(Session.status=="paid",
                Session.ended_at>=datetime.combine(ph_now().date(),dtime.min)).count()
    running  = sum(s.current_total for s in active)
    pq       = _print_queue_count()
    rates    = get_rates()
    return jsonify({"today_internet":internet,"today_printing":printing,"today_total":total,
                    "running_total":round(running,2),"active_count":len(active),
                    "unpaid_count":unpaid,"paid_count":paid_cnt,"print_queue_pending":pq,
                    **rates})

@app.route("/api/billing/session/<int:sid>")
@login_required
def api_billing_session(sid):
    sess = db.session.get(Session, sid)
    if not sess: return jsonify({"error":"Not found"}), 404
    data = sess.to_dict(); data["print_jobs"] = [j.to_dict() for j in sess.print_jobs]
    return jsonify(data)

@app.route("/api/billing/mark-paid", methods=["POST"])
@login_required
def api_billing_mark_paid():
    data = request.get_json()
    sess = db.session.get(Session, data.get("session_id"))
    if not sess: return jsonify({"error":"Not found"}), 404
    if sess.status not in ("ended","paid"):
        return jsonify({"error":"Session must be ended first"}), 409
    sess.paid=True; sess.status="paid"; db.session.commit()
    print(f"[BILLING] {sess.session_code} PAID — ₱{sess.total_charge:.2f}")
    return jsonify({"success":True,"session":sess.to_dict()})

@app.route("/api/billing/unpaid")
@login_required
def api_billing_unpaid():
    return jsonify([s.to_dict() for s in
                    Session.query.filter_by(status="ended",paid=False)
                                 .order_by(Session.ended_at.desc()).all()])

@app.route("/api/billing/history")
@login_required
def api_billing_history():
    return jsonify([s.to_dict() for s in
                    Session.query.filter(Session.status.in_(["ended","paid"]))
                                 .order_by(Session.ended_at.desc()).limit(200).all()])

@app.route("/api/billing/daily")
@login_required
def api_billing_daily():
    result=[]
    for i in range(6,-1,-1):
        d=ph_now().date()-timedelta(days=i)
        inet,prt,total=_daily_revenue(d)
        result.append({"date":d.strftime("%b %d"),"internet":inet,"printing":prt,"total":total})
    return jsonify(result)

@app.route("/api/billing/config", methods=["GET","POST"])
@login_required
def api_billing_config():
    if request.method == "POST":
        data = request.get_json()
        if "internet_rate"    in data: Settings.set("internet_rate",    float(data["internet_rate"]))
        if "print_rate_bw"    in data: Settings.set("print_rate_bw",    float(data["print_rate_bw"]))
        if "print_rate_color" in data: Settings.set("print_rate_color", float(data["print_rate_color"]))
        rates = get_rates()
        # Push new rates to ALL connected clients and admin pages
        def _push():
            with app.app_context():
                # Update all active sessions to new internet rate
                active = Session.query.filter_by(status="active").all()
                for s in active:
                    s.rate_per_hour = get_rate_internet()
                db.session.commit()
            socketio.emit("rates_updated", rates)
            _broadcast_pc_update()
        socketio.start_background_task(_push)
        print(f"[BILLING] Rates saved — Internet:₱{rates['internet_rate']}/hr B&W:₱{rates['print_rate_bw']}/p Color:₱{rates['print_rate_color']}/p")
    return jsonify(get_rates())

# ── Print API ─────────────────────────────────────────────────────────────────
@app.route("/api/print/upload", methods=["POST"])
def api_print_upload():
    from flask import session as fs
    pc_label      = request.form.get("pc_label","PC-??")
    customer_name = request.form.get("customer_name", fs.get("client_name","Unknown"))
    session_id    = request.form.get("session_id") or None
    note          = request.form.get("note","").strip()
    color_mode    = request.form.get("color_mode","BW")
    paper_size    = request.form.get("paper_size","Short")
    page_range    = request.form.get("page_range","All")
    copies_count  = max(1, int(request.form.get("copies_count",1) or 1))

    if "file" not in request.files: return jsonify({"error":"No file"}), 400
    file = request.files["file"]
    if not file or file.filename == "": return jsonify({"error":"Empty filename"}), 400
    if not _allowed(file.filename): return jsonify({"error":"File type not allowed. Use PDF, DOCX, JPG, PNG"}), 400

    ext      = file.filename.rsplit(".",1)[1].lower()
    safe     = secure_filename(file.filename)
    unique   = f"{uuid.uuid4().hex[:8]}_{safe}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], unique)
    file.save(filepath)

    total_pages    = _count_pages(filepath, ext)
    targeted_pages = _resolve_targeted_pages(total_pages, page_range)
    rate           = get_rate_color() if color_mode == "Color" else get_rate_bw()
    charge         = round(targeted_pages * copies_count * rate, 2)

    last = PrintJob.query.order_by(PrintJob.queue_position.desc()).first()
    pos  = (last.queue_position + 1) if last else 1

    job = PrintJob(note=note if note else None,
                   session_id=int(session_id) if session_id else None,
                   pc_label=pc_label, customer_name=customer_name,
                   filename=file.filename, file_path=unique, file_type=ext,
                   pages=total_pages, page_range=page_range,
                   targeted_pages=targeted_pages, copies_count=copies_count,
                   color_mode=color_mode, paper_size=paper_size,
                   charge=charge, status="pending", queue_position=pos)
    db.session.add(job)

    if session_id:
        sess = db.session.get(Session, int(session_id))
        if sess and sess.status == "active":
            sess.print_charge = round(sess.print_charge + charge, 2)

    db.session.commit()
    payload = job.to_dict()
    print(f"[PRINT] Job #{job.id} — {file.filename} | {targeted_pages}p×{copies_count} | {color_mode} | ₱{charge:.2f}")

    def _notify():
        socketio.emit("print_job_added", payload)
        sid = pc_sid_map.get(pc_label)
        if sid: socketio.emit("print_confirmed", payload, to=sid)
        _broadcast_pc_update()
    socketio.start_background_task(_notify)
    return jsonify({"success":True,"job":payload})

@app.route("/api/print/queue")
@login_required
def api_print_queue():
    jobs = PrintJob.query.filter(PrintJob.status.in_(["pending","processing"]))\
                         .order_by(PrintJob.queue_position).all()
    return jsonify([j.to_dict() for j in jobs])

@app.route("/api/print/history")
@login_required
def api_print_history():
    jobs = PrintJob.query.filter(PrintJob.status.in_(["done","cancelled"]))\
                         .order_by(PrintJob.created_at.desc()).limit(100).all()
    return jsonify([j.to_dict() for j in jobs])

# Admin download uploaded file
@app.route("/api/print/download/<int:job_id>")
@login_required
def api_print_download(job_id):
    job = db.session.get(PrintJob, job_id)
    if not job or not job.file_path:
        return jsonify({"error":"File not found"}), 404
    return send_from_directory(app.config["UPLOAD_FOLDER"], job.file_path,
                               as_attachment=True, download_name=job.filename)

@app.route("/api/print/process/<int:job_id>", methods=["POST"])
@login_required
def api_print_process(job_id):
    job = db.session.get(PrintJob, job_id)
    if not job: return jsonify({"error":"Not found"}), 404
    if job.status != "pending": return jsonify({"error":"Job is not pending"}), 409
    job.status = "processing"; db.session.commit()
    payload = job.to_dict()
    def _n():
        socketio.emit("print_job_updated", payload)
        sid = pc_sid_map.get(job.pc_label)
        if sid: socketio.emit("print_status", payload, to=sid)
    socketio.start_background_task(_n)
    return jsonify({"success":True,"job":payload})

@app.route("/api/print/done/<int:job_id>", methods=["POST"])
@login_required
def api_print_done(job_id):
    job = db.session.get(PrintJob, job_id)
    if not job: return jsonify({"error":"Not found"}), 404
    job.status = "done"; job.processed_at = ph_now(); db.session.commit()
    payload = job.to_dict()
    def _n():
        socketio.emit("print_job_updated", payload)
        sid = pc_sid_map.get(job.pc_label)
        if sid: socketio.emit("print_status", payload, to=sid)
    socketio.start_background_task(_n)
    return jsonify({"success":True,"job":payload})

@app.route("/api/print/cancel/<int:job_id>", methods=["POST"])
@login_required
def api_print_cancel(job_id):
    job = db.session.get(PrintJob, job_id)
    if not job: return jsonify({"error":"Not found"}), 404
    if job.status == "done": return jsonify({"error":"Cannot cancel completed job"}), 409
    if job.session_id:
        sess = db.session.get(Session, job.session_id)
        if sess and sess.status == "active":
            sess.print_charge = round(max(0.0, sess.print_charge - job.charge), 2)
            print(f"[PRINT] Refunded ₱{job.charge:.2f} to {sess.session_code}")
    job.status = "cancelled"; db.session.commit()
    payload = job.to_dict()
    def _n():
        socketio.emit("print_job_updated", payload)
        sid = pc_sid_map.get(job.pc_label)
        if sid: socketio.emit("print_status", payload, to=sid)
        _broadcast_pc_update()
    socketio.start_background_task(_n)
    return jsonify({"success":True,"job":payload})

@app.route("/api/print/stats")
@login_required
def api_print_stats():
    pending    = PrintJob.query.filter_by(status="pending").count()
    processing = PrintJob.query.filter_by(status="processing").count()
    done_today = PrintJob.query.filter(
        PrintJob.status=="done",
        PrintJob.processed_at>=datetime.combine(ph_now().date(),dtime.min)).count()
    return jsonify({"pending":pending,"processing":processing,
                    "done_today":done_today,"total_queue":pending+processing})



# ── Client Account Management API ────────────────────────────────────────────

@app.route("/api/client-accounts", methods=["GET"])
@login_required
def api_client_accounts():
    accounts = ClientAccount.query.order_by(ClientAccount.pc_id).all()
    return jsonify([a.to_dict() for a in accounts])

@app.route("/api/client-accounts", methods=["POST"])
@login_required
def api_create_client_account():
    data     = request.get_json()
    pc_id    = data.get("pc_id")
    name     = data.get("name","").strip()
    password = data.get("password","").strip()

    if not name or not password:
        return jsonify({"error":"name and password are required"}), 400

    # Normalize pc_id: convert empty strings to None, otherwise to int
    if pc_id == "" or pc_id is None:
        pc_id = None
    else:
        try:
            pc_id = int(pc_id)
        except (ValueError, TypeError):
            return jsonify({"error":"Invalid pc_id"}), 400
        pc = db.session.get(PC, pc_id)
        if not pc:
            return jsonify({"error":"PC not found"}), 404

    # Check for duplicates — use IS NULL for pc_id=None, = for actual ids
    if pc_id is None:
        existing = ClientAccount.query.filter(
            ClientAccount.pc_id == None,  # noqa: E711 — IS NULL
            ClientAccount.name == name
        ).first()
    else:
        existing = ClientAccount.query.filter_by(pc_id=pc_id, name=name).first()
    if existing:
        pc_label = f"PC {existing.pc.label}" if existing.pc_id else "any PC (unassigned)"
        return jsonify({"error":f"Account '{name}' already exists for {pc_label}"}), 409

    account = ClientAccount(name=name, pc_id=pc_id)
    account.set_password(password)
    db.session.add(account)
    db.session.commit()

    pc_label = db.session.get(PC, pc_id).label if pc_id else "any PC"
    print(f"[AUTH] Client account created: {name} on {pc_label}")
    return jsonify({"success":True,"account":account.to_dict()})

@app.route("/api/client-accounts/<int:account_id>", methods=["DELETE"])
@login_required
def api_delete_client_account(account_id):
    account = db.session.get(ClientAccount, account_id)
    if not account: return jsonify({"error":"Not found"}), 404
    account.active = False
    db.session.commit()
    return jsonify({"success":True})

@app.route("/api/client-accounts/<int:account_id>/reactivate", methods=["POST"])
@login_required
def api_reactivate_client_account(account_id):
    account = db.session.get(ClientAccount, account_id)
    if not account: return jsonify({"error":"Not found"}), 404
    account.active = True
    db.session.commit()
    return jsonify({"success":True})

@app.route("/api/client-accounts/<int:account_id>/delete", methods=["POST"])
@login_required
def api_permanently_delete_client_account(account_id):
    account = db.session.get(ClientAccount, account_id)
    if not account: return jsonify({"error":"Not found"}), 404
    account_name = account.name
    db.session.delete(account)
    db.session.commit()
    print(f"[AUTH] Client account permanently deleted: {account_name}")
    return jsonify({"success":True})

@app.route("/api/client-accounts/<int:account_id>/reset-password", methods=["POST"])
@login_required
def api_reset_client_password(account_id):
    account  = db.session.get(ClientAccount, account_id)
    if not account: return jsonify({"error":"Not found"}), 404
    data     = request.get_json()
    password = data.get("password","").strip()
    if not password: return jsonify({"error":"Password required"}), 400
    account.set_password(password)
    db.session.commit()
    return jsonify({"success":True})


# ── Reports & Analytics API ───────────────────────────────────────────────

@app.route("/api/reports/overview")
@login_required
def api_reports_overview():
    from sqlalchemy import func as sqlfunc
    today       = ph_now().date()
    week_start  = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    def rev_for_range(start, end):
        rows = Session.query.filter(Session.status=="paid",
            Session.ended_at>=datetime.combine(start,dtime.min),
            Session.ended_at<=datetime.combine(end,dtime.max)).all()
        return round(sum(r.internet_charge+r.print_charge for r in rows),2), len(rows)
    today_rev,today_sess  = rev_for_range(today, today)
    week_rev, week_sess   = rev_for_range(week_start, today)
    month_rev,month_sess  = rev_for_range(month_start, today)
    ended = Session.query.filter(Session.status.in_(["ended","paid"]),Session.ended_at.isnot(None)).all()
    avg_dur = round(sum(s.elapsed_seconds for s in ended)/len(ended)/60,1) if ended else 0
    pc_counts = {}
    for s in ended: pc_counts[s.pc.label]=pc_counts.get(s.pc.label,0)+1
    top_pc = max(pc_counts,key=pc_counts.get) if pc_counts else "—"
    total_print_pages = db.session.query(sqlfunc.sum(PrintJob.targeted_pages*PrintJob.copies_count)).scalar() or 0
    return jsonify({"today_revenue":today_rev,"today_sessions":today_sess,
        "week_revenue":week_rev,"week_sessions":week_sess,
        "month_revenue":month_rev,"month_sessions":month_sess,
        "avg_duration_min":avg_dur,"top_pc":top_pc,
        "total_sessions":Session.query.count(),
        "total_print_jobs":PrintJob.query.count(),
        "total_print_pages":int(total_print_pages),**get_rates()})

@app.route("/api/reports/revenue-trend")
@login_required
def api_reports_revenue_trend():
    result=[]
    for i in range(29,-1,-1):
        d=ph_now().date()-timedelta(days=i)
        inet,prt,total=_daily_revenue(d)
        sessions=Session.query.filter(Session.status=="paid",
            Session.ended_at>=datetime.combine(d,dtime.min),
            Session.ended_at<=datetime.combine(d,dtime.max)).count()
        result.append({"date":d.strftime("%b %d"),"internet":inet,"printing":prt,"total":total,"sessions":sessions})
    return jsonify(result)

@app.route("/api/reports/peak-hours")
@login_required
def api_reports_peak_hours():
    hours=[0]*24
    for s in Session.query.filter(Session.started_at.isnot(None)).all():
        hours[s.started_at.hour]+=1
    return jsonify([{"hour":h,"label":f"{h:02d}:00","count":hours[h]} for h in range(24)])

@app.route("/api/reports/pc-utilization")
@login_required
def api_reports_pc_utilization():
    result=[]
    for pc in PC.query.order_by(PC.pc_number).all():
        sessions=Session.query.filter(Session.pc_id==pc.id,Session.status.in_(["ended","paid","active"])).all()
        result.append({"label":pc.label,"sessions":len(sessions),
            "revenue":round(sum(s.internet_charge+s.print_charge for s in sessions),2),
            "minutes":int(round(sum(s.elapsed_seconds for s in sessions)/60,0)),
            "connected":pc.label in pc_sid_map})
    return jsonify(result)

@app.route("/api/reports/top-customers")
@login_required
def api_reports_top_customers():
    from sqlalchemy import func as sqlfunc
    rows=db.session.query(Session.customer_name,
        sqlfunc.count(Session.id).label("sessions"),
        sqlfunc.sum(Session.internet_charge+Session.print_charge).label("total_spend"),
        sqlfunc.sum(Session.elapsed_seconds).label("total_seconds")
    ).filter(Session.status.in_(["ended","paid","active"]))     .group_by(Session.customer_name)     .order_by(sqlfunc.sum(Session.internet_charge+Session.print_charge).desc()).limit(10).all()
    return jsonify([{"name":r.customer_name,"sessions":r.sessions,
        "total_spend":round(float(r.total_spend or 0),2),
        "total_hours":round(float(r.total_seconds or 0)/3600,2)} for r in rows])

@app.route("/api/reports/session-log")
@login_required
def api_reports_session_log():
    status=request.args.get("status","all")
    limit=int(request.args.get("limit",100)); offset=int(request.args.get("offset",0))
    q=Session.query
    if status!="all": q=q.filter(Session.status==status)
    sessions=q.order_by(Session.id.desc()).limit(limit).offset(offset).all()
    total = q.order_by(None).count() if status != "all" else Session.query.count()
    return jsonify({"total":total,"sessions":[s.to_dict() for s in sessions]})



# ── AI Insights API (Phase 6 — Gemini) ───────────────────────────────────

def _build_cafe_context():
    """Build a compact café snapshot for Gemini (minimizes token usage)."""
    from sqlalchemy import func as sqlfunc
    today = ph_now().date()

    # Revenue last 7 days (compact)
    daily_lines = []
    for i in range(6,-1,-1):
        d = today - timedelta(days=i)
        inet,prt,total = _daily_revenue(d)
        daily_lines.append(f"{d.strftime('%b %d')}: P{total:.0f} (I:P{inet:.0f} P:P{prt:.0f})")

    active    = Session.query.filter_by(status="active").count()
    pcs_occ   = PC.query.filter_by(status="occupied").count()
    pcs_total = PC.query.count()
    rates     = get_rates()

    top_custs = db.session.query(
        Session.customer_name,
        sqlfunc.count(Session.id).label("n"),
        sqlfunc.sum(Session.internet_charge+Session.print_charge).label("spend")
    ).filter(Session.status.in_(["ended","paid","active"]))     .group_by(Session.customer_name)     .order_by(sqlfunc.sum(Session.internet_charge+Session.print_charge).desc()).limit(3).all()

    hours = [0]*24
    for s in Session.query.filter(Session.started_at.isnot(None)).all():
        hours[s.started_at.hour] += 1
    peak_hour = hours.index(max(hours)) if any(hours) else 0

    total_sess  = Session.query.count()
    total_print = PrintJob.query.count()

    custs_str = ", ".join(f"{r.customer_name}({r.n}x P{float(r.spend or 0):.0f})" for r in top_custs) or "none yet"

    return (
        f"Philippine internet cafe data [{ph_now().strftime('%b %d %Y %I:%M%p')}]. "
        f"Now: {active} active/{pcs_occ} occupied of {pcs_total} PCs. "
        f"Rates: P{rates['internet_rate']}/hr internet, P{rates['print_rate_bw']}/p BW, P{rates['print_rate_color']}/p color. "
        f"7-day revenue: {' | '.join(daily_lines)}. "
        f"Peak hour: {peak_hour:02d}:00. "
        f"Top customers: {custs_str}. "
        f"All-time: {total_sess} sessions, {total_print} print jobs."
    )


@app.route("/api/ai/insights", methods=["POST"])
@login_required
def api_ai_insights():
    """Generate AI insights using Ollama (local, free, unlimited)."""
    import urllib.request, json as _json, urllib.error

    data        = request.get_json()
    user_prompt = data.get("prompt", "Give me a general business overview and recommendations.")
    mode        = data.get("mode", "insights")

    # Ollama settings — configurable via DB, defaults to localhost
    ollama_url   = Settings.get("ollama_url",   "http://localhost:11434")
    ollama_model = Settings.get("ollama_model", "gemma3:1b")

    cafe_context = _build_cafe_context()

    system_prompts = {
        "insights": "You are a business analyst for a Philippine internet café. Analyze the provided café data and give 3-5 specific, actionable business insights. Be concise, use bullet points, and focus on Philippine peso amounts. Always mention specific numbers from the data.",
        "predict":  "You are a revenue forecasting expert for a Philippine internet café. Based on the historical data provided, predict tomorrow's expected revenue and sessions. Give a specific number range, explain your reasoning, and suggest 2 actions to maximize revenue.",
        "question": "You are a helpful assistant for a Philippine internet café manager. Answer the question using only the data provided. Be specific, cite actual numbers, and keep the answer concise.",
    }

    system_text = system_prompts.get(mode, system_prompts["insights"])
    full_prompt = f"{system_text}\n\nCAFÉ DATA:\n{cafe_context}\n\nUSER REQUEST: {user_prompt}"

    payload = _json.dumps({
        "model":  ollama_model,
        "prompt": full_prompt,
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 1024}
    }).encode()

    req = urllib.request.Request(
        f"{ollama_url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = _json.loads(resp.read())
        text = result.get("response", "").strip()
        if not text:
            return jsonify({"error": "empty_response"}), 500
        print(f"[AI] Ollama ({ollama_model}) responded OK")
        return jsonify({"success": True, "response": text, "mode": mode, "model": ollama_model})

    except urllib.error.URLError as e:
        # Ollama not running or wrong URL
        print(f"[AI] Ollama not reachable: {e}")
        return jsonify({"error": "ollama_offline"}), 503
    except Exception as e:
        print(f"[AI] Error: {e}")
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/ai/config", methods=["GET", "POST"])
@login_required
def api_ai_config():
    """Get or save Ollama settings."""
    if request.method == "POST":
        data = request.get_json()
        if "ollama_url"   in data: Settings.set("ollama_url",   data["ollama_url"].strip())
        if "ollama_model" in data: Settings.set("ollama_model", data["ollama_model"].strip())
        return jsonify({"success": True})

    url   = Settings.get("ollama_url",   "http://localhost:11434")
    model = Settings.get("ollama_model", "gemma3:1b")

    # Quick connectivity check
    import urllib.request, urllib.error
    online = False
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=2) as r:
            online = r.status == 200
    except Exception:
        pass

    return jsonify({"url": url, "model": model, "online": online})


@app.route("/api/ai/quick-insights")
@login_required
def api_ai_quick_insights():
    """Pre-built quick insight buttons."""
    return jsonify([
        {"label":"Business Overview",   "prompt":"Give me a general business overview and key performance highlights.", "mode":"insights"},
        {"label":"Revenue Tips",         "prompt":"What specific actions should I take to increase revenue this week?",  "mode":"insights"},
        {"label":"Predict Tomorrow",     "prompt":"Predict tomorrow's revenue and number of sessions.",                  "mode":"predict"},
        {"label":"Best Hours",            "prompt":"What are the best and worst hours for business? When should I open and close?", "mode":"insights"},
        {"label":"Print Analysis",       "prompt":"Analyze my printing service. Is it profitable? Should I adjust prices?", "mode":"insights"},
        {"label":"Customer Insights",    "prompt":"Tell me about my customer patterns. Who are my most loyal customers?",  "mode":"insights"},
    ])


# ── Billing Tick ──────────────────────────────────────────────────────────────
_billing_started = False

def billing_tick_worker():
    while True:
        time.sleep(10)
        with app.app_context():
            active = Session.query.filter_by(status="active").all()
            if active: print(f"[BILLING] Tick — {len(active)} active")
            ticks = []
            for s in active:
                sid = pc_sid_map.get(s.pc.label)
                if sid:
                    ticks.append((sid,{"session_id":s.id,"pc_label":s.pc.label,
                        "seconds":s.elapsed_seconds,"bill":s.current_total,
                        "internet":s.current_internet_charge,"print":s.print_charge}))
        for sid,payload in ticks:
            socketio.emit("billing_tick", payload, to=sid)

def start_billing_thread():
    global _billing_started
    if not _billing_started:
        t = threading.Thread(target=billing_tick_worker, daemon=True)
        t.start(); _billing_started = True
        print("[BILLING] Billing tick thread started")

# ── SocketIO ──────────────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    ip=request.environ.get("REMOTE_ADDR","?"); sid=request.sid
    connected_clients[sid]={"sid":sid,"ip":ip,"role":"unknown"}
    print(f"[SOCKET] Connect    — ip={ip:<18} sid={sid} | total={len(connected_clients)}")
    emit("server_ack",{"message":"Connected.","sid":sid})
    emit("client_connected",{"sid":sid,"ip":ip,"total":len(connected_clients)},broadcast=True)
    # Send current rates on connect
    with app.app_context():
        rates = get_rates()
    emit("rates_updated", rates)

@socketio.on("disconnect")
def on_disconnect():
    sid=request.sid; info=connected_clients.pop(sid,{})
    pc_label=info.get("pc_label")
    if pc_label and pc_sid_map.get(pc_label)==sid: del pc_sid_map[pc_label]
    print(f"[SOCKET] Disconnect — ip={info.get('ip','?'):<18} sid={sid} | role={info.get('role','?')} pc={info.get('pc_label','?')} | total={len(connected_clients)}")
    emit("client_disconnected",{"sid":sid,"ip":info.get("ip","?"),"total":len(connected_clients)},broadcast=True)
    socketio.start_background_task(_broadcast_pc_update)

@socketio.on("identify")
def on_identify(data):
    sid=request.sid; role=data.get("role","client"); pc_label=data.get("pc_label",f"PC-{sid[:4].upper()}")
    connected_clients[sid]={**connected_clients.get(sid,{}),"role":role,"pc_label":pc_label}
    if role=="client":
        pc_sid_map[pc_label]=sid
        def _u():
            with app.app_context():
                pc=PC.query.filter_by(label=pc_label).first()
                if pc: pc.socket_sid=sid; pc.last_seen=ph_now(); db.session.commit()
            _broadcast_pc_update()
        socketio.start_background_task(_u)
    emit("identified",{"message":f"Identified as {role} / {pc_label}"})
    print(f"[SOCKET] Identified — sid={sid} -> role={role:<8} pc={pc_label}")

@socketio.on("request_pc_grid")
def on_request_pc_grid(*args):
    emit("pc_update",_pc_grid_data())

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__=="__main__":
    host=os.getenv("HOST","0.0.0.0"); port=int(os.getenv("PORT",5000))
    with app.app_context(): init_db()
    start_billing_thread()
    print(f"\n{'='*45}\n  ICMS-ASI Phase 4  ->  http://localhost:{port}\n  LAN              ->  http://<THIS_IP>:{port}\n{'='*45}\n")
    socketio.run(app,host=host,port=port,debug=True,use_reloader=False,allow_unsafe_werkzeug=True)
