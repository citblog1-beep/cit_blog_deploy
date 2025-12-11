from flask import (
    Flask, render_template, request, redirect, session, url_for,
    render_template_string, flash, jsonify
)
import sqlite3
import os
import glob
from functools import wraps
from datetime import datetime, timedelta
import pytz
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import uuid
import re
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


# ==============================
# إعداد التطبيق والثوابت
# ==============================

# المجلد الأساسي للتطبيق (مهم جداً في بيئات الاستضافة)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# مجلدات المحتوى (مسارات مطلقة نسبية لمجلد المشروع)
BASE_POSTS_DIR = os.path.join(BASE_DIR, "posts")          # لم نعد نستخدمه في العرض ولكن نُبقيه لو أردت توليد HTML لاحقًا
BASE_MARKDOWN_DIR = os.path.join(BASE_DIR, "markdown")    # مصدر المحتوى (نخزّن HTML أيضًا من Quill داخل .md)

# مسارات قواعد البيانات كمسارات مطلقة
DB_PATH = os.path.join(BASE_DIR, "users.db")
COMMENTS_DB_PATH = os.path.join(BASE_DIR, "comments.db")
POSTS_STATS_DB_PATH = os.path.join(BASE_DIR, "posts_stats.db")

app = Flask(__name__)

# مفتاح الجلسة (يُفضّل ضبطه من متغيّر بيئة في الإنتاج)
app.config["SECRET_KEY"] = os.environ.get("CIT_SECRET_KEY", "change-me-in-production")

# رفع الصور للمحرر
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_UPLOAD_MB = 5
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# إعدادات SMTP (Gmail + App Password) – تُقرأ من متغيرات البيئة قدر الإمكان
app.config.update({
    "MAIL_SERVER": os.environ.get("MAIL_SERVER", "smtp.gmail.com"),
    "MAIL_PORT": int(os.environ.get("MAIL_PORT", "587")),
    "MAIL_USERNAME": os.environ.get("MAIL_USERNAME", ""),       # يجب ضبطها في متغيّر بيئة
    "MAIL_PASSWORD": os.environ.get("MAIL_PASSWORD", ""),       # يجب ضبطها في متغيّر بيئة
    "MAIL_USE_TLS": os.environ.get("MAIL_USE_TLS", "1") == "1",
    "MAIL_USE_SSL": os.environ.get("MAIL_USE_SSL", "0") == "1",
    "MAIL_FROM_NAME": os.environ.get("MAIL_FROM_NAME", "CIT Blog"),
    "MAIL_FROM_ADDR": os.environ.get("MAIL_FROM_ADDR", ""),     # إن تُركت فارغة سنستخدم MAIL_USERNAME
    # في الإنتاج عدّل هذا المتغيّر في بيئة الاستضافة إلى https://yourdomain.com
    "APP_BASE_URL": os.environ.get("APP_BASE_URL", "http://127.0.0.1:5000"),
})


# ==============================
# Utilities
# ==============================
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def slugify_ar(name: str) -> str:
    """تحويل نص (قد يكون عربي) إلى slug لاتيني آمن للرابط/المجلد."""
    text = name.strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^A-Za-z0-9\-\_ء-ي]+", "", text)
    if re.fullmatch(r"[ء-ي\-\_]+", text or ""):
        arabic_map = {
            "أ": "a", "إ": "i", "آ": "a", "ا": "a", "ب": "b", "ت": "t", "ث": "th", "ج": "j",
            "ح": "h", "خ": "kh", "د": "d", "ذ": "th", "ر": "r", "ز": "z", "س": "s", "ش": "sh",
            "ص": "s", "ض": "d", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "q",
            "ك": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h", "و": "w", "ي": "y",
            "ة": "h", "ى": "a", "ئ": "y", "ؤ": "w",
        }
        text = "".join(arabic_map.get(ch, ch) for ch in text)
    text = text.lower().strip("-_")
    text = re.sub(r"-{2,}", "-", text)
    return text or "section"


def list_posts_in_category(category_folder: str):
    """إرجاع قائمة (filename, title) لكل مقال في قسم معيّن (من ملفات .md)."""
    folder_path = os.path.join(BASE_MARKDOWN_DIR, category_folder)
    files = glob.glob(os.path.join(folder_path, "*.md"))
    posts = []
    for path in files:
        filename = os.path.splitext(os.path.basename(path))[0]
        title = filename
        try:
            with open(path, "r", encoding="utf-8") as f:
                first_line = f.readline()
                if first_line.startswith("#"):
                    title = first_line.replace("#", "").strip()
        except Exception:
            pass
        posts.append((filename, title))
    return posts


def send_email(to_email: str, subject: str, html_content: str):
    """إرسال بريد عبر SMTP بحسب الإعدادات العامة."""
    from_addr = app.config.get("MAIL_FROM_ADDR") or app.config.get("MAIL_USERNAME")

    # حماية بسيطة: إذا لم تُضبط بيانات البريد لا نحاول الإرسال
    if not app.config.get("MAIL_USERNAME") or not app.config.get("MAIL_PASSWORD"):
        print("EMAIL CONFIG ERROR: MAIL_USERNAME or MAIL_PASSWORD not set. Email not sent.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{app.config.get('MAIL_FROM_NAME', 'CIT Blog')} <{from_addr}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    server = app.config["MAIL_SERVER"]
    port = app.config["MAIL_PORT"]
    username = app.config["MAIL_USERNAME"]
    password = app.config["MAIL_PASSWORD"]
    use_tls = app.config.get("MAIL_USE_TLS", True)
    use_ssl = app.config.get("MAIL_USE_SSL", False)

    if use_ssl:
        with smtplib.SMTP_SSL(server, port) as s:
            s.login(username, password)
            s.sendmail(from_addr, to_email, msg.as_string())
    else:
        with smtplib.SMTP(server, port) as s:
            if use_tls:
                s.starttls()
            s.login(username, password)
            s.sendmail(from_addr, to_email, msg.as_string())


# ==============================
# قواعد البيانات (Users + Categories + Password Resets + Email Verifications)
# ==============================
def init_users_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'writer',
            status TEXT DEFAULT 'active',
            phone TEXT,
            created_at TEXT,
            email_verified INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS email_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.commit()
    conn.close()


def migrate_add_role_column():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA table_info(users)")
    cols = [r[1] for r in c.fetchall()]
    if "role" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'writer'")
    if "status" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'")
    if "phone" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    if "email_verified" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


def init_categories_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            folder TEXT UNIQUE NOT NULL,
            is_active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    c.execute("SELECT COUNT(*) FROM categories")
    (count,) = c.fetchone()
    if count == 0:
        now = datetime.now(pytz.timezone("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M:%S")
        seeds = [
            ("🛠️ برمجتي", "projects", "projects", 1, 10),
            ("📚 شروحاتي", "tutorials", "tutorials", 1, 20),
            ("🧠 مقالاتي", "articles", "articles", 1, 30),
        ]
        for name, slug, folder, active, order in seeds:
            c.execute("""
                INSERT OR IGNORE INTO categories (name, slug, folder, is_active, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (name, slug, folder, active, order, now))
    conn.commit()
    conn.close()


def _col_exists(conn, table, col):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return col in cols


def migrate_categories_schema():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT UNIQUE,
            folder TEXT UNIQUE,
            is_active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.commit()

    if not _col_exists(conn, "categories", "slug"):
        c.execute("ALTER TABLE categories ADD COLUMN slug TEXT")
    if not _col_exists(conn, "categories", "folder"):
        c.execute("ALTER TABLE categories ADD COLUMN folder TEXT")
    if not _col_exists(conn, "categories", "is_active"):
        c.execute("ALTER TABLE categories ADD COLUMN is_active INTEGER DEFAULT 1")
    if not _col_exists(conn, "categories", "sort_order"):
        c.execute("ALTER TABLE categories ADD COLUMN sort_order INTEGER DEFAULT 0")
    if not _col_exists(conn, "categories", "created_at"):
        c.execute("ALTER TABLE categories ADD COLUMN created_at TEXT")
    conn.commit()

    c.execute("SELECT id, name, slug, folder FROM categories")
    rows = c.fetchall()
    now = datetime.now(pytz.timezone("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M:%S")
    for (cid, name, slug, folder) in rows:
        if not slug or not slug.strip():
            new_slug = slugify_ar(name or "")
            c.execute(
                "UPDATE categories SET slug=?, created_at=COALESCE(created_at, ?) WHERE id=?",
                (new_slug, now, cid),
            )
        if not folder or not folder.strip():
            c.execute("UPDATE categories SET folder=slug WHERE id=?", (cid,))
    conn.commit()
    conn.close()


def ensure_category_dirs():
    for cat in get_categories():
        folder = cat["folder"]
        os.makedirs(os.path.join(BASE_MARKDOWN_DIR, folder), exist_ok=True)
        os.makedirs(os.path.join(BASE_POSTS_DIR, folder), exist_ok=True)


def get_categories():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT id, name, slug, folder, is_active, sort_order
        FROM categories
        WHERE is_active = 1
        ORDER BY sort_order ASC, id ASC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_category_by_folder(folder: str):
    """جلب بيانات قسم واحد اعتماداً على قيمة folder."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, name, slug, folder FROM categories WHERE folder = ?", (folder,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


# استدعاءات التهيئة
init_users_db()
migrate_add_role_column()
init_categories_db()
migrate_categories_schema()
ensure_category_dirs()


# ==============================
# قاعدة بيانات إحصائيات المقالات
# ==============================
def init_posts_stats_db():
    conn = sqlite3.connect(POSTS_STATS_DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            filename TEXT NOT NULL,
            views INTEGER DEFAULT 0,
            UNIQUE(category, filename)
        )
    """)
    conn.commit()
    conn.close()


init_posts_stats_db()


def increment_view(category, filename):
    conn = sqlite3.connect(POSTS_STATS_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT views FROM stats WHERE category=? AND filename=?", (category, filename))
    row = c.fetchone()
    if row:
        c.execute(
            "UPDATE stats SET views = views + 1 WHERE category=? AND filename=?",
            (category, filename),
        )
    else:
        c.execute(
            "INSERT INTO stats (category, filename, views) VALUES (?, ?, 1)",
            (category, filename),
        )
    conn.commit()
    conn.close()


def get_views(category, filename):
    conn = sqlite3.connect(POSTS_STATS_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT views FROM stats WHERE category=? AND filename=?", (category, filename))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


# ==============================
# شارة المدير + ضخ الأقسام للقوالب
# ==============================
def get_pending_count():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE status = 'pending'")
        (count,) = c.fetchone()
        conn.close()
        return count or 0
    except Exception:
        return 0


@app.context_processor
def inject_globals():
    is_admin = (session.get("logged_in") and session.get("role") == "admin")
    try:
        categories = get_categories()
    except Exception:
        categories = []
    return {
        "is_admin": is_admin,
        "pending_count": get_pending_count() if is_admin else 0,
        "categories": categories,
    }


# ==============================
# ديكوريتر حماية
# ==============================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("auth_page"))
        return f(*args, **kwargs)

    return decorated


# ==============================
# صفحات عامة
# ==============================
@app.route("/")
def index():
    return render_template("index.html")


# ==============================
# واجهة موحدة للمستخدمين (تسجيل/دخول/استعادة)
# ==============================
@app.route("/auth", methods=["GET"])
def auth_page():
    return render_template("auth.html")


@app.route("/register_user", methods=["POST"])
def register_user():
    name = (request.form.get("register-name") or "").strip()
    email = (request.form.get("register-email") or "").strip().lower()
    phone = (request.form.get("register-phone") or "").strip()
    password = request.form.get("register-password") or ""
    confirm = request.form.get("register-confirm-password") or ""

    if not name or not email or not password:
        flash("⚠️ الرجاء تعبئة الحقول المطلوبة", "error")
        return redirect(url_for("auth_page"))

    if password != confirm:
        flash("❌ كلمات المرور غير متطابقة", "error")
        return redirect(url_for("auth_page"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = ?", (email,))
    if c.fetchone():
        conn.close()
        flash("❌ هذا البريد مستخدم مسبقًا", "error")
        return redirect(url_for("auth_page"))

    hashed_pw = generate_password_hash(password)
    now = datetime.now(pytz.timezone("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
        INSERT INTO users (username, email, password, role, status, phone, created_at, email_verified)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (name, email, hashed_pw, "writer", "active", phone, now, 0))
    user_id = c.lastrowid
    conn.commit()
    conn.close()

    token = secrets.token_hex(32)
    expires_at = (datetime.utcnow() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    created_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO email_verifications (user_id, token, expires_at, created_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, token, expires_at, created_utc))
    conn.commit()
    conn.close()

    verify_link = f"{app.config['APP_BASE_URL']}/verify/{token}"

    html = f"""
    <html>
      <body style='font-family:Cairo,Arial; text-align:center;'>
        <h2>👋 مرحبًا {name}</h2>
        <p>شكرًا لتسجيلك في مدونة CIT.</p>
        <p>فضلاً اضغط على الزر التالي لتفعيل بريدك الإلكتروني وإكمال تفعيل حسابك:</p>
        <p>
          <a href="{verify_link}"
             style="background:#16a34a;color:#fff;padding:10px 18px;
                    border-radius:8px;text-decoration:none;">
             تفعيل حسابي
          </a>
        </p>
        <p style='color:#666;font-size:13px;'>
          هذا الرابط صالح لمدة 24 ساعة. إذا لم تقم بالتسجيل، يمكنك تجاهل هذه الرسالة.
        </p>
      </body>
    </html>
    """

    try:
        send_email(email, "✅ تفعيل حسابك في مدونة CIT", html)
        flash(
            "✅ تم إنشاء الحساب! تم إرسال رسالة تفعيل إلى بريدك الإلكتروني. "
            "فضلاً قم بفتح الرسالة والضغط على رابط التفعيل قبل تسجيل الدخول.",
            "success",
        )
    except Exception as e:
        print("SMTP error while sending verification email:", e)
        print("DEV ONLY – email verification link:", verify_link)
        flash(
            "✅ تم إنشاء الحساب، لكن تعذّر إرسال رسالة التفعيل حاليًا. "
            "الرجاء المحاولة لاحقًا أو التواصل مع مدير الموقع.",
            "warning",
        )

    return redirect(url_for("auth_page"))


@app.route("/verify/<token>")
def verify_email(token):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM email_verifications WHERE token = ?", (token,))
    rec = c.fetchone()

    if not rec:
        conn.close()
        return """
        <div style="text-align:center; font-family:Cairo,Arial; margin-top:80px;">
          <h2>⚠️ رابط التفعيل غير صالح</h2>
          <p>ربما استخدم سابقًا أو منتهي الصلاحية.</p>
          <p><a href="/auth">العودة لصفحة الدخول/التسجيل</a></p>
        </div>
        """, 400

    try:
        exp_dt = datetime.strptime(rec["expires_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        conn.close()
        return """
        <div style="text-align:center; font-family:Cairo,Arial; margin-top:80px;">
          <h2>⚠️ رابط التفعيل غير صالح</h2>
          <p>حدث خطأ في بيانات الرابط.</p>
          <p><a href="/auth">العودة لصفحة الدخول/التسجيل</a></p>
        </div>
        """, 400

    if datetime.utcnow() > exp_dt:
        c.execute("DELETE FROM email_verifications WHERE id = ?", (rec["id"],))
        conn.commit()
        conn.close()
        return """
        <div style="text-align:center; font-family:Cairo,Arial; margin-top:80px;">
          <h2>⚠️ انتهت صلاحية رابط التفعيل</h2>
          <p>الرجاء طلب تفعيل جديد من خلال صفحة التسجيل.</p>
          <p><a href="/auth">العودة لصفحة الدخول/التسجيل</a></p>
        </div>
        """, 400

    user_id = rec["user_id"]
    c.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))
    c.execute("DELETE FROM email_verifications WHERE id = ?", (rec["id"],))
    conn.commit()
    conn.close()

    return """
    <div style="text-align:center; font-family:Cairo,Arial; margin-top:80px;">
      <h2>✅ تم تفعيل بريدك الإلكتروني بنجاح</h2>
      <p>يمكنك الآن تسجيل الدخول إلى حسابك.</p>
      <p><a href="/auth">الانتقال إلى صفحة تسجيل الدخول</a></p>
    </div>
    """


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return redirect(url_for("auth_page"))

    login_value = (request.form.get("login-email") or
                   request.form.get("username") or
                   request.form.get("email") or "").strip()
    password = request.form.get("login-password") or request.form.get("password") or ""

    if not login_value or not password:
        flash("⚠️ الرجاء إدخال البريد/المستخدم وكلمة المرور", "error")
        return redirect(url_for("auth_page"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if "@" in login_value:
        c.execute("SELECT * FROM users WHERE email = ?", (login_value.lower(),))
    else:
        c.execute("SELECT * FROM users WHERE username = ?", (login_value,))

    user = c.fetchone()
    conn.close()

    if not user:
        flash("❌ بيانات الدخول غير صحيحة", "error")
        return redirect(url_for("auth_page"))

    if not check_password_hash(user["password"], password):
        flash("❌ كلمة المرور غير صحيحة", "error")
        return redirect(url_for("auth_page"))

    if user["status"] == "banned":
        flash("🚫 حسابك محظور.", "error")
        return redirect(url_for("auth_page"))

    if not user["email_verified"]:
        flash("📧 يجب تفعيل بريدك الإلكتروني قبل تسجيل الدخول. "
              "تحقق من الرسالة التي أُرسلت إلى بريدك عند التسجيل.", "error")
        return redirect(url_for("auth_page"))

    session["logged_in"] = True
    session["username"] = user["username"]
    session["role"] = user["role"]
    flash("✅ تم تسجيل الدخول بنجاح", "success")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    session.pop("username", None)
    session.pop("role", None)
    return redirect(url_for("index"))


# ==============================
# استعادة كلمة المرور عبر البريد
# ==============================
@app.route("/forgot", methods=["POST"])
def forgot_password():
    email = (request.form.get("forgot-email") or
             request.form.get("email") or "").strip().lower()

    if not email:
        flash("⚠️ أدخل بريدك الإلكتروني", "error")
        return redirect(url_for("auth_page"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, email, username, email_verified FROM users WHERE email = ?", (email,))
    user = c.fetchone()
    conn.close()

    if not user:
        flash("❌ لا يوجد حساب بهذا البريد", "error")
        return redirect(url_for("auth_page"))

    if not user["email_verified"]:
        flash("⚠️ بريدك الإلكتروني غير مفعّل بعد. "
              "فضلاً قم بتفعيل بريدك قبل طلب استعادة كلمة المرور.", "error")
        return redirect(url_for("auth_page"))

    token = secrets.token_hex(32)
    expires_at = (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO password_resets (user_id, token, expires_at, created_at)
        VALUES (?, ?, ?, ?)
    """, (user["id"], token, expires_at, now))
    conn.commit()
    conn.close()

    reset_link = f"{app.config['APP_BASE_URL']}/reset/{token}"

    html = f"""
    <html>
      <body style='font-family:Cairo,Arial; text-align:center;'>
        <h2>🔑 استعادة كلمة المرور</h2>
        <p>مرحبًا {user['username']},</p>
        <p>لقد طلبت استعادة كلمة المرور لحسابك في مدونة CIT.</p>
        <p>اضغط على الزر لتعيين كلمة مرور جديدة (الرابط صالح لمدة ساعة واحدة):</p>
        <p>
          <a href="{reset_link}"
             style="background:#0d6efd;color:#fff;padding:10px 18px;
                    border-radius:8px;text-decoration:none;">
             إعادة تعيين كلمة المرور
          </a>
        </p>
        <p style='color:#666;font-size:13px;'>
          إذا لم تطلب ذلك، يمكنك تجاهل هذه الرسالة ولن يتم تغيير أي شيء في حسابك.
        </p>
      </body>
    </html>
    """

    try:
        send_email(email, "🔐 استعادة كلمة المرور - مدونة CIT", html)
        flash(
            "📩 تم إرسال رسالة استعادة كلمة المرور إلى بريدك الإلكتروني "
            "إذا كان مسجَّلًا لدينا.",
            "success",
        )
    except Exception as e:
        print("SMTP error while sending reset email:", e)
        print("DEV ONLY – password reset link:", reset_link)
        flash(
            "⚠️ تعذّر إرسال البريد الآن. الرجاء المحاولة لاحقًا أو التواصل مع مدير الموقع.",
            "error",
        )

    return redirect(url_for("auth_page"))


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM password_resets WHERE token = ?", (token,))
    rec = c.fetchone()

    if not rec:
        conn.close()
        return "⚠️ الرابط غير صالح", 400

    try:
        exp_dt = datetime.strptime(rec["expires_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        conn.close()
        return "⚠️ الرابط غير صالح", 400

    if datetime.utcnow() > exp_dt:
        c.execute("DELETE FROM password_resets WHERE id = ?", (rec["id"],))
        conn.commit()
        conn.close()
        return "⚠️ انتهت صلاحية الرابط، اطلب رابطًا جديدًا.", 400

    if request.method == "POST":
        new_pw = request.form.get("password") or ""
        if len(new_pw) < 6:
            flash("⚠️ كلمة المرور يجب ألا تقل عن 6 حروف", "error")
            return redirect(request.url)

        hashed = generate_password_hash(new_pw)
        c.execute("UPDATE users SET password=? WHERE id=?", (hashed, rec["user_id"]))
        c.execute("DELETE FROM password_resets WHERE id = ?", (rec["id"],))
        conn.commit()
        conn.close()

        return """
        <div style="text-align:center; font-family:Cairo,Arial; margin-top:80px;">
          <h2>✅ تم تحديث كلمة المرور بنجاح</h2>
          <p><a href="/auth">العودة لتسجيل الدخول</a></p>
        </div>
        """

    conn.close()
    return """
    <form method="POST" style="text-align:center; margin-top:100px; font-family:Cairo,Arial;">
      <h2>🔑 تعيين كلمة مرور جديدة</h2>
      <input type="password" name="password" placeholder="كلمة المرور الجديدة"
             required style="padding:10px; width:260px;">
      <br><br>
      <button type="submit" style="padding:10px 20px;">تحديث</button>
    </form>
    """


# ==============================
# إدارة الأقسام (لوحة المدير)
# ==============================
@app.route("/admin/categories", methods=["GET", "POST"])
@login_required
def admin_categories():
    if session.get("role") != "admin":
        return "🚫 غير مصرح", 403

    message = None
    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        sort_order = request.form.get("sort_order", "0").strip()
        if not name:
            error = "❌ أدخل اسم القسم."
        else:
            try:
                sort_order = int(sort_order or 0)
                slug = slugify_ar(name)
                folder = slug
                os.makedirs(os.path.join(BASE_MARKDOWN_DIR, folder), exist_ok=True)
                os.makedirs(os.path.join(BASE_POSTS_DIR, folder), exist_ok=True)

                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT id FROM categories WHERE slug = ?", (slug,))
                exists = c.fetchone()
                if exists:
                    error = "⚠️ هذا القسم موجود بالفعل."
                else:
                    now = datetime.now(pytz.timezone("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M:%S")
                    c.execute("""
                        INSERT INTO categories (name, slug, folder, is_active, sort_order, created_at)
                        VALUES (?, ?, ?, 1, ?, ?)
                    """, (name, slug, folder, sort_order, now))
                    conn.commit()
                    conn.close()
                    message = "✅ تم إنشاء القسم."
            except Exception as e:
                error = f"❌ خطأ أثناء الإضافة: {e}"

    cats = get_categories()
    return render_template("admin_categories.html", categories=cats, message=message, error=error)


@app.post("/admin/categories/delete/<int:cat_id>")
@login_required
def delete_category(cat_id):
    if session.get("role") != "admin":
        return "🚫 غير مصرح", 403

    PROTECTED = {"projects", "tutorials", "articles"}

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, name, slug, folder FROM categories WHERE id = ?", (cat_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return redirect(url_for("admin_categories"))

        if row["slug"] in PROTECTED:
            conn.close()
            flash("⛔️ لا يمكن حذف قسم افتراضي.", "warning")
            return redirect(url_for("admin_categories"))

        folder = row["folder"]
        md_dir = os.path.join(BASE_MARKDOWN_DIR, folder)
        has_files = any(glob.glob(os.path.join(md_dir, "*.md")))
        if has_files:
            conn.close()
            flash("⚠️ احذف مقالات هذا القسم أولاً.", "warning")
            return redirect(url_for("admin_categories"))

        c.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        conn.commit()
        conn.close()

        try:
            os.rmdir(os.path.join(BASE_MARKDOWN_DIR, folder))
        except Exception:
            pass
        try:
            os.rmdir(os.path.join(BASE_POSTS_DIR, folder))
        except Exception:
            pass

        flash("🗑️ تم حذف القسم.", "success")
    except Exception as e:
        flash(f"❌ خطأ أثناء الحذف: {e}", "danger")

    return redirect(url_for("admin_categories"))


@app.route("/admin/pending-users")
@login_required
def pending_users():
    if session.get("role") != "admin":
        return "🚫 غير مصرح لك بدخول لوحة المدير", 403

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, username, email, role, status FROM users WHERE status = 'pending'")
    users = c.fetchall()
    conn.close()

    return render_template("pending_users.html", users=users)


@app.route("/admin/update-user/<int:user_id>/<string:action>")
@login_required
def update_user_status(user_id, action):
    if session.get("role") != "admin":
        return "🚫 غير مصرح لك بدخول لوحة المدير", 403

    status_map = {"approve": "active", "reject": "banned"}
    if action not in status_map:
        return "❌ أمر غير معروف", 400

    new_status = status_map[action]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET status=? WHERE id=?", (new_status, user_id))
    conn.commit()
    conn.close()

    return redirect(url_for("pending_users"))


@app.route("/admin/users")
@login_required
def admin_users():
    """عرض جميع المستخدمين (لوحة تحكم الأدمن)."""
    if session.get("role") != "admin":
        return "🚫 غير مصرح لك بدخول لوحة المدير", 403

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT id, username, email, role, status, created_at
        FROM users
        ORDER BY created_at DESC, id DESC
    """)
    users = c.fetchall()
    conn.close()

    return render_template("admin_users.html", users=users)


@app.route("/admin/users/set-role/<int:user_id>/<string:new_role>")
@login_required
def admin_set_user_role(user_id, new_role):
    """تحديث دور المستخدم: writer / admin."""
    if session.get("role") != "admin":
        return "🚫 غير مصرح لك بدخول لوحة المدير", 403

    if new_role not in ("admin", "writer"):
        flash("❌ دور غير صالح", "error")
        return redirect(url_for("admin_users"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()

    if not user:
        conn.close()
        flash("⚠️ المستخدم غير موجود", "warning")
        return redirect(url_for("admin_users"))

    current_username = session.get("username")

    # منع إنزال نفسك من admin إلى writer
    if user["username"] == current_username and new_role != "admin":
        conn.close()
        flash("🚫 لا يمكنك إزالة صلاحية المدير عن نفسك.", "error")
        return redirect(url_for("admin_users"))

    c.execute("UPDATE users SET role=? WHERE id=?", (new_role, user_id))
    conn.commit()
    conn.close()

    flash("✅ تم تحديث دور المستخدم.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/set-status/<int:user_id>/<string:new_status>")
@login_required
def admin_set_user_status(user_id, new_status):
    """تحديث حالة المستخدم: active / banned / pending."""
    if session.get("role") != "admin":
        return "🚫 غير مصرح لك بدخول لوحة المدير", 403

    if new_status not in ("active", "banned", "pending"):
        flash("❌ حالة غير صالحة", "error")
        return redirect(url_for("admin_users"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, username, status FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()

    if not user:
        conn.close()
        flash("⚠️ المستخدم غير موجود", "warning")
        return redirect(url_for("admin_users"))

    current_username = session.get("username")

    # منع حظر نفسك
    if user["username"] == current_username and new_status == "banned":
        conn.close()
        flash("🚫 لا يمكنك حظر حسابك.", "error")
        return redirect(url_for("admin_users"))

    c.execute("UPDATE users SET status=? WHERE id=?", (new_status, user_id))
    conn.commit()
    conn.close()

    flash("✅ تم تحديث حالة المستخدم.", "success")
    return redirect(url_for("admin_users"))


# ==============================
# “أضف مقالًا” + رفع الصور
# ==============================
@app.route("/form")
@login_required
def form():
    if session.get("role") != "admin":
        return "🚫 غير مسموح لك بإنشاء المقالات", 403
    categories = get_categories()
    return render_template("form.html", categories=categories)


@app.route("/submit", methods=["POST"])
@login_required
def submit():
    if session.get("role") != "admin":
        return "🚫 صلاحيات غير كافية", 403

    title = request.form["title"].strip()
    filename = request.form["filename"].strip()
    content = request.form["content"]       # HTML الناتج من Quill
    category = request.form["category"].strip()

    # مجلدات القسم
    md_dir = os.path.join(BASE_MARKDOWN_DIR, category)
    os.makedirs(md_dir, exist_ok=True)

    # نحفظ في ملف markdown: أول سطر عنوان بـ # ثم المحتوى
    md_path = os.path.join(md_dir, f"{filename}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n{content}")

    # لا نولّد HTML منفصل الآن، العرض يتم من view_post + post_template.html
    return redirect(url_for("form", success=1))


@app.post("/upload_image")
def upload_image():
    if "file" not in request.files:
        return jsonify({"error": "لم يتم إرسال ملف"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "لم يتم اختيار ملف"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "صيغة الصورة غير مدعومة"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    base = secure_filename(file.filename.rsplit(".", 1)[0])[:40]
    fname = f"{base or 'img'}_{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(UPLOAD_FOLDER, fname)
    file.save(save_path)

    url = url_for("static", filename=f"uploads/{fname}", _external=False)
    return jsonify({"url": url}), 200


# ==============================
# التعليقات (مربوطة بالقسم + اسم الملف)
# ==============================
def _ensure_comments_table():
    """تهيئة جدول التعليقات + إضافة عمود category إن لم يكن موجوداً."""
    conn = sqlite3.connect(COMMENTS_DB_PATH)
    c = conn.cursor()

    # إنشاء الجدول إن لم يكن موجوداً
    c.execute("""
        CREATE TABLE IF NOT EXISTS comments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            post_filename TEXT,
            name TEXT,
            comment TEXT,
            timestamp TEXT
        )
    """)

    # التأكد من وجود العمود category في الجداول القديمة
    c.execute("PRAGMA table_info(comments)")
    cols = [row[1] for row in c.fetchall()]
    if "category" not in cols:
        c.execute("ALTER TABLE comments ADD COLUMN category TEXT")

    conn.commit()
    conn.close()


def get_comments(category, filename):
    """إرجاع التعليقات الخاصة بمقال معيّن داخل قسم محدّد."""
    _ensure_comments_table()
    conn = sqlite3.connect(COMMENTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT name, comment, timestamp
        FROM comments
        WHERE category = ? AND post_filename = ?
        ORDER BY timestamp DESC
    """, (category, filename))
    comments = c.fetchall()
    conn.close()
    return comments


def add_comment_to_db(category, filename, name, comment):
    """إضافة تعليق لمقال معيّن داخل قسم معيّن."""
    _ensure_comments_table()
    tz = pytz.timezone('Asia/Riyadh')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')

    conn = sqlite3.connect(COMMENTS_DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO comments (category, post_filename, name, comment, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (category, filename, name, comment, timestamp))
    conn.commit()
    conn.close()


# ==============================
# عرض مقال واحد + مقالات مشابهة
# ==============================
@app.route("/post/<category>/<filename>")
def view_post(category, filename):
    # زيادة عدد المشاهدات
    increment_view(category, filename)
    views = get_views(category, filename)

    # قراءة ملف markdown
    md_path = os.path.join(BASE_MARKDOWN_DIR, category, f"{filename}.md")
    if not os.path.exists(md_path):
        return "❌ المقال غير موجود", 404

    with open(md_path, "r", encoding="utf-8") as f:
        raw = f.read()

    # استخراج العنوان من أول سطر يبدأ بـ #
    lines = raw.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        page_title = lines[0].lstrip("#").strip()
        body_html = "\n".join(lines[1:]).strip()
    else:
        page_title = filename
        body_html = raw

    # محاولة استخراج تاريخ من النص (اختياري)
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    # إذا لم نجد تاريخ، نخليها None بدلاً من "غير محدد"
    date_value = date_match.group(1) if date_match else None

    # جلب التعليقات لهذا المقال (مربوطة بالقسم + اسم الملف)
    comments = get_comments(category, filename)

    # معلومات القسم (للبريدكرمب + زر العودة)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT name, slug, folder FROM categories WHERE folder = ? AND is_active = 1",
        (category,),
    )
    cat_row = c.fetchone()
    conn.close()

    if cat_row:
        category_name = cat_row["name"]
        category_slug = cat_row["slug"]
    else:
        category_name = category
        category_slug = category

    # مقالات مشابهة من نفس القسم (قائمة قواميس فيها رابط جاهز)
    related_posts = []
    for fn, t in list_posts_in_category(category):
        if fn == filename:
            continue

        related_posts.append({
            "filename": fn,
            "title": t,
            "url": url_for("view_post", category=category, filename=fn),
        })

    return render_template(
        "post_template.html",
        title=page_title,
        content=body_html,
        filename=filename,
        comments=comments,
        date=date_value,
        views=views,
        category_name=category_name,
        category_slug=category_slug,
        related_posts=related_posts,
    )


# إضافة تعليق من النموذج
@app.route("/add_comment/<category>/<filename>", methods=["POST"])
def add_comment(category, filename):
    if not session.get("logged_in"):
        return "🚫 يجب تسجيل الدخول للتعليق", 403

    name = session.get("username")
    comment = request.form.get("comment", "").strip()
    if not comment:
        # لا نسمح بتعليق فارغ
        return redirect(request.referrer or "/")

    add_comment_to_db(category, filename, name, comment)
    return redirect(request.referrer or "/")


# ==============================
# البحث
# ==============================
@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    results = []
    if not query:
        return render_template("search_results.html", query=query, results=[])

    try:
        cats = get_categories()
    except Exception:
        cats = [
            {"folder": "projects"},
            {"folder": "tutorials"},
            {"folder": "articles"},
        ]

    for cat in cats:
        folder = cat["folder"]
        folder_path = os.path.join(BASE_MARKDOWN_DIR, folder)
        if not os.path.exists(folder_path):
            continue

        for path in glob.glob(os.path.join(folder_path, "*.md")):
            filename = os.path.splitext(os.path.basename(path))[0]
            title = filename
            snippet = ""
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                    lines = content.splitlines()
                    if lines and lines[0].startswith("#"):
                        title = lines[0].replace("#", "").strip()
                    idx = content.lower().find(query.lower())
                    if idx != -1:
                        start = max(idx - 50, 0)
                        snippet = content[start:start + 150].replace("\n", " ")
            except Exception:
                continue

            if (query.lower() in title.lower()) or snippet:
                results.append({
                    "category": folder,
                    "filename": filename,
                    "title": title,
                    "snippet": snippet,
                })

    return render_template("search_results.html", query=query, results=results)


# ==============================
# إدارة المقالات (تعديل / حذف) - لوحة الأدمن
# ==============================
def list_all_posts_with_category():
    """
    ترجع قائمة بكل المقالات في كل الأقسام:
    كل عنصر: {category_folder, category_name, category_slug, filename, title}
    """
    posts = []
    try:
        cats = get_categories()
    except Exception:
        cats = []

    for cat in cats:
        folder = cat["folder"]
        cat_name = cat["name"]
        cat_slug = cat["slug"]
        md_dir = os.path.join(BASE_MARKDOWN_DIR, folder)

        if not os.path.exists(md_dir):
            continue

        for path in glob.glob(os.path.join(md_dir, "*.md")):
            filename = os.path.splitext(os.path.basename(path))[0]
            title = filename
            try:
                with open(path, "r", encoding="utf-8") as f:
                    first_line = f.readline()
                    if first_line.lstrip().startswith("#"):
                        title = first_line.replace("#", "").strip()
            except Exception:
                pass

            posts.append({
                "category_folder": folder,
                "category_name": cat_name,
                "category_slug": cat_slug,
                "filename": filename,
                "title": title,
            })

    # ترتيب بسيط: حسب اسم القسم ثم العنوان
    posts.sort(key=lambda p: (p["category_name"], p["title"]))
    return posts


@app.route("/admin/posts")
@login_required
def admin_posts():
    """قائمة جميع المقالات للأدمن فقط."""
    if session.get("role") != "admin":
        return "🚫 غير مصرح", 403

    posts = list_all_posts_with_category()
    return render_template("admin_posts.html", posts=posts)


@app.route("/admin/posts/edit/<category>/<filename>", methods=["GET", "POST"])
@login_required
def edit_post(category, filename):
    """تعديل مقال موجود (العنوان + المحتوى) داخل نفس القسم / نفس الملف."""
    if session.get("role") != "admin":
        return "🚫 صلاحيات غير كافية", 403

    md_path = os.path.join(BASE_MARKDOWN_DIR, category, f"{filename}.md")
    if not os.path.exists(md_path):
        return "❌ المقال غير موجود", 404

    if request.method == "POST":
        new_title = (request.form.get("title") or "").strip()
        new_content = request.form.get("content") or ""

        if not new_title:
            flash("⚠️ يجب إدخال عنوان للمقال", "error")
            return redirect(request.url)

        # نكتب أول سطر كـ H1 ثم المحتوى (HTML من Quill)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# {new_title}\n\n{new_content}")

        flash("✅ تم حفظ تعديلات المقال بنجاح", "success")
        return redirect(url_for("view_post", category=category, filename=filename))

    # GET: تحميل المقال الحالي لملئ النموذج
    with open(md_path, "r", encoding="utf-8") as f:
        raw = f.read()

    lines = raw.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip("#").strip()
        body_html = "\n".join(lines[1:]).strip()
    else:
        title = filename
        body_html = raw

    cat_obj = get_category_by_folder(category)
    category_name = cat_obj["name"] if cat_obj else category

    return render_template(
        "edit_post.html",
        category=category,
        category_name=category_name,
        filename=filename,
        title=title,
        content=body_html,
    )


@app.post("/admin/posts/delete/<category>/<filename>")
@login_required
def delete_post(category, filename):
    """حذف مقال + تنظيف الإحصائيات + التعليقات."""
    if session.get("role") != "admin":
        return "🚫 صلاحيات غير كافية", 403

    md_path = os.path.join(BASE_MARKDOWN_DIR, category, f"{filename}.md")

    try:
        # حذف ملف الـ markdown إن وجد
        if os.path.exists(md_path):
            os.remove(md_path)

        # حذف الإحصائيات من posts_stats.db
        conn_stats = sqlite3.connect(POSTS_STATS_DB_PATH)
        c_stats = conn_stats.cursor()
        c_stats.execute("DELETE FROM stats WHERE category=? AND filename=?", (category, filename))
        conn_stats.commit()
        conn_stats.close()

        # حذف التعليقات من comments.db
        _ensure_comments_table()
        conn_comm = sqlite3.connect(COMMENTS_DB_PATH)
        c_comm = conn_comm.cursor()
        c_comm.execute(
            "DELETE FROM comments WHERE category=? AND post_filename=?",
            (category, filename),
        )
        conn_comm.commit()
        conn_comm.close()

        flash("🗑️ تم حذف المقال بنجاح", "success")
    except Exception as e:
        flash(f"❌ حدث خطأ أثناء حذف المقال: {e}", "error")

    return redirect(url_for("admin_posts"))


# ==============================
# مسارات الأقسام (ثابت + ديناميكي)
# ==============================
@app.route("/projects")
def projects():
    posts = list_posts_in_category("projects")
    return render_template("category.html",
                           title="🛠️ برمجتي",
                           posts=posts,
                           category="projects")


@app.route("/tutorials")
def tutorials():
    posts = list_posts_in_category("tutorials")
    return render_template("category.html",
                           title="📚 شروحاتي",
                           posts=posts,
                           category="tutorials")


@app.route("/articles")
def articles():
    posts = list_posts_in_category("articles")
    return render_template("category.html",
                           title="🧠 مقالاتي",
                           posts=posts,
                           category="articles")


# ==============================
# فحص هل اسم الملف موجود مسبقًا
# ==============================
@app.route("/check_filename")
def check_filename():
    category = request.args.get("category", "").strip()
    filename = request.args.get("filename", "").strip()

    if not category or not filename:
        return jsonify({"exists": False})

    md_path = os.path.join(BASE_MARKDOWN_DIR, category, f"{filename}.md")
    exists = os.path.exists(md_path)
    return jsonify({"exists": exists})


@app.route("/<slug>")
def dynamic_category(slug):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # جملة SQL في سترنق واحد بدون كسر غير صحيح
    c.execute(
        "SELECT name, folder FROM categories WHERE slug = ? AND is_active = 1",
        (slug,),
    )
    row = c.fetchone()
    conn.close()

    if not row:
        return "❌ القسم غير موجود", 404

    folder = row["folder"]
    title = f"📂 {row['name']}"
    posts = list_posts_in_category(folder)

    return render_template(
        "category.html",
        title=title,
        posts=posts,
        category=folder
    )


# ==============================
# صفحات ثابتة: عن المدونة / تواصل / سياسة الخصوصية
# ==============================
@app.route("/about")
def about_page():
    return render_template("about.html")


@app.route("/privacy")
def privacy_page():
    """
    صفحة سياسة الخصوصية (تُستخدم عادة في طلبات Google AdSense).
    """
    return render_template("privacy.html")


@app.route("/contact", methods=["GET", "POST"])
def contact_page():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip()
        message = (request.form.get("message") or "").strip()

        if not name or not email or not message:
            flash("⚠️ الرجاء تعبئة جميع الحقول.", "error")
            return redirect(url_for("contact_page"))

        # معالجة الرسالة قبل استخدامها في الـ f-string
        safe_message = message.replace("\n", "<br>")

        # نص الرسالة التي ستصل لبريدك
        html = f"""
        <html>
          <body style='font-family:Cairo,Arial;'>
            <h3>📩 رسالة جديدة من نموذج التواصل في مدونة CIT</h3>
            <p><strong>الاسم:</strong> {name}</p>
            <p><strong>البريد:</strong> {email}</p>
            <p><strong>الرسالة:</strong></p>
            <p>{safe_message}</p>
          </body>
        </html>
        """

        try:
            admin_email = app.config.get("MAIL_FROM_ADDR") or app.config.get("MAIL_USERNAME")
            send_email(
                to_email=admin_email,
                subject="📩 تواصل جديد من مدونة CIT",
                html_content=html,
            )
            flash("✅ تم إرسال رسالتك بنجاح، شكرًا لتواصلك.", "success")
        except Exception as e:
            print("Contact form send_email error:", e)
            flash("⚠️ تعذّر إرسال الرسالة حاليًا، الرجاء المحاولة لاحقًا.", "error")

        return redirect(url_for("contact_page"))

    return render_template("contact.html")


# ==============================
# Run (للاستخدام المحلي فقط)
# ==============================
if __name__ == "__main__":
    # في التطوير: FLASK_DEBUG=1 (افتراضي)
    # في الإنتاج (لو شغلت بـ python app.py): اضبط FLASK_DEBUG=0
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(debug=debug)
