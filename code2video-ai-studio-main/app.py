import os
import threading
import uuid
import json
import ast
from complexity import analyze as complexity_analyze
import re
import requests
import asyncio
import sqlite3
import zipfile
from io import BytesIO
import time
import webbrowser
from dotenv import load_dotenv
load_dotenv()

from groq import Groq  # ✅ NEW: Groq import
from flask import Flask, request, jsonify, render_template, redirect, send_file, session
from flask import send_from_directory
from authlib.integrations.flask_client import OAuth
import edge_tts
from moviepy import (
    ImageClip,
    AudioFileClip,
    concatenate_videoclips,
    CompositeVideoClip
)
import moviepy.video.fx as vfx
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pygments import highlight
from pygments.lexers import PythonLexer
from pygments.formatters import ImageFormatter
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# Render/any proxy serves over HTTPS -> trust X-Forwarded-* headers so url_for/redirects use https
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.secret_key = os.getenv("FLASK_SECRET_KEY", "supersecretkey123")

app.config.update(
    SESSION_COOKIE_NAME="google-login-session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
)

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# ===============================
# GROQ CLIENT INITIALIZATION (REPLACED GEMINI)
# ===============================

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
client = Groq(api_key=GROQ_API_KEY)

# llama-3.3-70b-versatile was retired from Groq free/dev tier (Aug 2026); model is configurable via env
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# ===============================
# GOOGLE OAUTH CONFIG
# ===============================

oauth = OAuth(app)

google = oauth.register(
    name="google",
    client_id=os.getenv(
        "GOOGLE_CLIENT_ID",
        
    ),
    client_secret=os.getenv(
        "GOOGLE_CLIENT_SECRET"
        
    ),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"}
)

tasks = {}
VIDEO_DIR = "generated_videos"
TEMP_DIR = os.path.join(VIDEO_DIR, "temp_assets")
SUBTITLE_DIR = os.path.join(VIDEO_DIR, "subtitles")

os.makedirs(SUBTITLE_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

DEFAULT_VOICE = "en-US-AriaNeural"

SUPPORTED_LANGUAGES = {
    "english": {
        "label": "English",
        "voice": "en-US-AriaNeural",
        "groq_name": "English"
    },
    "hindi": {
        "label": "Hindi",
        "voice": "hi-IN-SwaraNeural",
        "groq_name": "Hindi"
    },
    "marathi": {
        "label": "Marathi",
        "voice": "mr-IN-AarohiNeural",
        "groq_name": "Marathi"
    },
    "telugu": {
        "label": "Telugu",
        "voice": "te-IN-ShrutiNeural",
        "groq_name": "Telugu"
    }
}

SUPPORTED_EXPLAIN_MODES = {
    "beginner": "Beginner",
    "advanced": "Advanced"
}

SUPPORTED_VOICE_SPEEDS = {
    "slow": "-20%",
    "normal": "-5%",
    "fast": "+15%"
}

SUPPORTED_THEMES = {
    "ocean": "Ocean",
    "midnight": "Midnight",
    "neon": "Neon"
}

DB = "users.db"

VIDEO_W = 1280
VIDEO_H = 720


# ===============================
# DATABASE HELPERS
# ===============================

def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    # users table
    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        password TEXT
    )
    """)

    # videos table
    c.execute("""
    CREATE TABLE IF NOT EXISTS videos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT,
        task_id TEXT UNIQUE,
        title TEXT,
        filename TEXT,
        original_code TEXT,
        language TEXT,
        explain_mode TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # favorites table
    c.execute("""
    CREATE TABLE IF NOT EXISTS favorites(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT,
        task_id TEXT,
        UNIQUE(user_email, task_id)
    )
    """)

    # doubts table
    c.execute("""
    CREATE TABLE IF NOT EXISTS doubts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT,
        task_id TEXT,
        question TEXT,
        answer TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # feedback table
    c.execute("""
    CREATE TABLE IF NOT EXISTS feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT,
        task_id TEXT,
        rating INTEGER,
        comment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 🔁 migration for old DB (VERY IMPORTANT)
    try:
        c.execute("ALTER TABLE videos ADD COLUMN title TEXT")
    except:
        pass

    try:
        c.execute("ALTER TABLE videos ADD COLUMN language TEXT")
    except:
        pass

    try:
        c.execute("ALTER TABLE videos ADD COLUMN explain_mode TEXT")
    except:
        pass

    conn.commit()
    conn.close()

def get_current_user():
    if "user" not in session:
        return None

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT name, email FROM users WHERE email=?", (session["user"],))
    user = c.fetchone()
    conn.close()
    return user


init_db()


# ===============================
# NORMALIZERS
# ===============================

def _normalize_language(language):
    lang = str(language or "english").strip().lower()
    return lang if lang in SUPPORTED_LANGUAGES else "english"


def _normalize_explain_mode(mode):
    mode = str(mode or "beginner").strip().lower()
    return mode if mode in SUPPORTED_EXPLAIN_MODES else "beginner"


def _normalize_voice_speed(speed):
    speed = str(speed or "normal").strip().lower()
    return speed if speed in SUPPORTED_VOICE_SPEEDS else "normal"


def _normalize_theme(theme):
    theme = str(theme or "ocean").strip().lower()
    return theme if theme in SUPPORTED_THEMES else "ocean"


def _get_voice_for_language(language):
    lang = _normalize_language(language)
    return SUPPORTED_LANGUAGES.get(lang, SUPPORTED_LANGUAGES["english"])["voice"]


def _get_groq_language_name(language):
    lang = _normalize_language(language)
    return SUPPORTED_LANGUAGES.get(lang, SUPPORTED_LANGUAGES["english"])["groq_name"]


# ===============================
# AUTH ROUTES
# ===============================

@app.route("/api/register", methods=["POST"])
def register():
    data = request.json or {}

    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    raw_password = data.get("password", "")

    if not name or not email or not raw_password:
        return jsonify({"status": "error", "message": "All fields are required"}), 400

    password = generate_password_hash(raw_password)

    conn = get_conn()
    c = conn.cursor()

    try:
        c.execute(
            "INSERT INTO users(name,email,password) VALUES(?,?,?)",
            (name, email, password)
        )
        conn.commit()
        conn.close()

        session["user"] = email
        return jsonify({"status": "success"})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"status": "error", "message": "User already exists"}), 400


@app.route("/api/login", methods=["POST"])
def login_api():
    data = request.json or {}
    email = data.get("email", "").strip()
    password = data.get("password", "")

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email=?", (email,))
    user = c.fetchone()
    conn.close()

    if user and check_password_hash(user["password"], password):
        session["user"] = email
        return jsonify({"status": "success"})

    return jsonify({"status": "error", "message": "Incorrect email or password."}), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ===============================
# GOOGLE LOGIN
# ===============================

@app.route("/auth/google")
def google_login():
    redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:5000/auth/google/callback")
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    try:
        token = google.authorize_access_token()

        resp = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token['access_token']}"},
            timeout=20
        )

        user_info = resp.json()
        email = user_info["email"]
        name = user_info.get("name", "Google User")

        conn = get_conn()
        c = conn.cursor()

        c.execute("SELECT * FROM users WHERE email=?", (email,))
        user = c.fetchone()

        if not user:
            c.execute(
                "INSERT INTO users(name,email,password) VALUES(?,?,?)",
                (name, email, "GOOGLE_AUTH")
            )
            conn.commit()

        conn.close()

        session["user"] = email
        return redirect("/dashboard")

    except Exception as e:
        print("Google login error:", e)
        return redirect("/login?error=google")


# ===============================
# PAGE ROUTES
# ===============================

@app.route("/")
def landing():
    """Public landing page"""
    has_demo = os.path.exists(os.path.join(app.static_folder, "demo.mp4"))
    return render_template("landing.html", logged_in="user" in session, has_demo=has_demo)


@app.route("/privacy")
def privacy():
    return render_template("legal.html", page="privacy", title="Privacy Policy")


@app.route("/terms")
def terms():
    return render_template("legal.html", page="terms", title="Terms of Use")


@app.route("/login")
def login():
    """Sign in / create account page"""
    if "user" in session:
        return redirect("/dashboard")
    error = None
    if request.args.get("error") == "google":
        error = "Google sign-in didn't complete. Please try again or use email."
    return render_template("login.html", error=error)


@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")  # ← redirect to /login, not /
    
    user = get_current_user()
    display_name = user["name"] if user else "User"
    return render_template("dashboard.html", display_name=display_name)


@app.route("/generated_videos/<filename>")
def get_video(filename):
    return send_from_directory(VIDEO_DIR, filename)

# ===============================
# API ROUTES FOR REAL DATA
# ===============================

@app.route("/api/me")
def api_me():
    if "user" not in session:
        return jsonify({"logged_in": False}), 401

    user = get_current_user()
    if not user:
        return jsonify({"logged_in": False}), 404

    return jsonify({
        "logged_in": True,
        "name": user["name"],
        "email": user["email"]
    })


@app.route("/api/stats")
def api_stats():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    user_email = session["user"]
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) AS cnt FROM videos WHERE user_email=?", (user_email,))
    total_videos = c.fetchone()["cnt"]

    c.execute("SELECT COUNT(*) AS cnt FROM feedback WHERE user_email=?", (user_email,))
    total_feedback = c.fetchone()["cnt"]

    c.execute("SELECT AVG(rating) AS avg_rating FROM feedback WHERE user_email=?", (user_email,))
    avg_rating_row = c.fetchone()
    avg_rating = avg_rating_row["avg_rating"] if avg_rating_row["avg_rating"] is not None else 0

    c.execute("SELECT COUNT(*) AS cnt FROM favorites WHERE user_email=?", (user_email,))
    favorite_count = c.fetchone()["cnt"]

    c.execute("""
        SELECT filename, created_at
        FROM videos
        WHERE user_email=?
        ORDER BY id DESC
        LIMIT 1
    """, (user_email,))
    last_video = c.fetchone()

    conn.close()

    return jsonify({
        "total_videos": total_videos,
        "total_feedback": total_feedback,
        "avg_rating": round(avg_rating, 1),
        "favorite_count": favorite_count,
        "last_video": dict(last_video) if last_video else None
    })


@app.route("/api/recent_videos")
def api_recent_videos():
    if "user" not in session:
        return jsonify([]), 401

    user_email = session["user"]
    search = request.args.get("search", "").strip().lower()
    favorites_only = request.args.get("favorites", "").strip() == "1"
    req_language = request.args.get("language", "").strip().lower()

    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT v.task_id, v.title, v.filename, v.original_code, v.created_at, v.language, v.explain_mode,
               CASE WHEN f.id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite
        FROM videos v
        LEFT JOIN favorites f
          ON v.task_id = f.task_id AND f.user_email = ?
        WHERE v.user_email=?
        ORDER BY v.id DESC
        LIMIT 50
    """, (user_email, user_email))

    rows = c.fetchall()
    conn.close()

    results = []
    for row in rows:
        is_favorite = bool(row["is_favorite"])

        if favorites_only and not is_favorite:
            continue

        filename = row["filename"] or ""
        title = row["title"] or filename
        original_code = row["original_code"] or ""
        language = (row["language"] or "").lower()
        explain_mode = (row["explain_mode"] or "").lower()

        if search:
            combined = f"{title} {filename} {original_code}".lower()
            if search not in combined:
                continue

        if req_language and req_language != language:
            continue

        results.append({
            "task_id": row["task_id"],
            "filename": filename,
            "title": title,
            "created_at": row["created_at"],
            "video_url": f"/download/{row['task_id']}",
            "is_favorite": is_favorite,
            "language": language,
            "explain_mode": explain_mode,
            "original_code": original_code
        })

    return jsonify(results[:24])


@app.route("/api/video/<tid>")
def api_video_details(tid):
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT v.task_id, v.title, v.filename, v.original_code, v.created_at, v.language, v.explain_mode,
               CASE WHEN f.id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite
        FROM videos v
        LEFT JOIN favorites f ON v.task_id = f.task_id AND f.user_email = ?
        WHERE v.user_email = ? AND v.task_id = ?
    """, (session["user"], session["user"], tid))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Video not found"}), 404
    meta = _load_video_meta(tid)
    return jsonify({
        "task_id": row["task_id"],
        "title": row["title"] or row["filename"],
        "filename": row["filename"],
        "original_code": row["original_code"] or "",
        "created_at": row["created_at"],
        "language": row["language"] or "",
        "explain_mode": row["explain_mode"] or "",
        "is_favorite": bool(row["is_favorite"]),
        "video_url": f"/download/{row['task_id']}",
        "steps_meta": meta.get("steps_meta", []),
        "complexity": meta.get("complexity", {})
    })


@app.route("/api/last_video")
def api_last_video():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    user_email = session["user"]
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
    SELECT v.task_id, v.title, v.filename, v.original_code, v.created_at, v.language, v.explain_mode,
           CASE WHEN f.id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite
    FROM videos v
    LEFT JOIN favorites f
      ON v.task_id = f.task_id AND f.user_email = ?
    WHERE v.user_email=?
    ORDER BY v.id DESC
    LIMIT 1
""", (user_email, user_email))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({})

    task_id = row["task_id"]
    task_data = _load_video_meta(task_id)

    return jsonify({
     "task_id": row["task_id"],
     "title": row["title"] or row["filename"],
     "filename": row["filename"],
     "original_code": row["original_code"],
     "created_at": row["created_at"],
     "language": row["language"] or "",
     "explain_mode": row["explain_mode"] or "",
     "is_favorite": bool(row["is_favorite"]),
     "video_url": f"/download/{row['task_id']}",
     "steps_meta": task_data.get("steps_meta", []),
     "complexity": task_data.get("complexity", {})
   })

# ===============================
# VIDEO RENDER HELPERS
# ===============================

_INDIC_FONT_CACHE = {}


def _find_indic_font(script):
    """Find a font file that really contains Devanagari / Telugu glyphs.
    Debian ships Noto either as static files (NotoSansDevanagari-Regular.ttf)
    or as variable fonts (NotoSansDevanagari[wdth,wght].ttf), so search by pattern."""
    if script in _INDIC_FONT_CACHE:
        return _INDIC_FONT_CACHE[script]
    import glob
    import subprocess
    name = "NotoSansTelugu" if script == "telugu" else "NotoSansDevanagari"
    lang = "te" if script == "telugu" else "hi"
    found = None
    hits = []
    for root in ("/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts")):
        hits += glob.glob(os.path.join(root, "**", name + "*.ttf"), recursive=True)
    hits = [h for h in hits if not any(w in os.path.basename(h) for w in ("UI", "Condensed", "Semi", "Extra", "Display"))]
    hits.sort(key=lambda p: (0 if "Regular" in p or "[" in p else 1, len(p)))
    if hits:
        found = hits[0]
    if not found:
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", f":lang={lang}"], capture_output=True, text=True, timeout=5).stdout.strip()
            if out and os.path.exists(out):
                found = out
        except Exception:
            pass
    _INDIC_FONT_CACHE[script] = found
    return found


def _load_font(size=24, bold=False, language="english"):
    candidates = []

    if language in {"hindi", "marathi", "telugu"}:
        script = "telugu" if language == "telugu" else "devanagari"
        found = _find_indic_font(script)
        if found:
            try:
                font = ImageFont.truetype(found, size=size)
                try:
                    font.set_variation_by_name("Bold" if bold else "Regular")
                except Exception:
                    pass
                return font
            except Exception:
                pass
        candidates = [
            "C:/Windows/Fonts/NirmalaB.ttf" if bold else "C:/Windows/Fonts/Nirmala.ttf",
            "C:/Windows/Fonts/mangal.ttf",
            "C:/Windows/Fonts/gautami.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    else:
        if bold:
            candidates = [
                "C:/Windows/Fonts/arialbd.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            ]
        else:
            candidates = [
                "C:/Windows/Fonts/arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            ]

    for path in candidates:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size=size)
        except Exception:
            pass

    return ImageFont.load_default()

FONT_PT = _load_font(24, bold=True)
FONT_XL = _load_font(38, bold=True)
FONT_LG = _load_font(28, bold=True)
FONT_MD = _load_font(22, bold=False)
FONT_SM = _load_font(18, bold=False)
FONT_BADGE = _load_font(18, bold=True)
FONT_SUB = _load_font(22, bold=False)
FONT_VAR = _load_font(18, bold=False)
FONT_CODE_BADGE = _load_font(17, bold=True)
FONT_TINY = _load_font(15, bold=False)


# -------------------------------
# Utility helpers
# -------------------------------

def _hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _wrap_text(text, font, max_width, draw):
    words = str(text).split()
    if not words:
        return [""]

    lines = []
    current = words[0]

    for word in words[1:]:
        trial = current + " " + word
        width = draw.textbbox((0, 0), trial, font=font)[2]
        if width <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines


def _rounded_box(draw, xy, fill, outline=None, width=2, radius=22):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _normalize_value(val):
    try:
        if isinstance(val, str):
            stripped = val.strip()
            if (
                (stripped.startswith("[") and stripped.endswith("]"))
                or (stripped.startswith("{") and stripped.endswith("}"))
                or (stripped.startswith("(") and stripped.endswith(")"))
            ):
                return ast.literal_eval(stripped)
        return val
    except Exception:
        return val


def _safe_repr(val, max_len=80):
    try:
        text = str(_normalize_value(val))
    except Exception:
        text = str(val)
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _value_as_list(val):
    v = _normalize_value(val)
    if isinstance(v, (list, tuple)):
        return list(v)
    return None


def _safe_int(val):
    try:
        return int(val)
    except Exception:
        return None


def _truncate_words(text, max_words=10):
    words = str(text).replace("\n", " ").split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + "..."


def _first_sentence(text):
    text = str(text).strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    return parts[0].strip()


def _simplify_subtitle(text, max_words=22):
    """Keep whole sentences; only cut (with an ellipsis) when the text is really long."""
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = sentences[0]
    for s in sentences[1:]:
        if len((out + " " + s).split()) <= max_words:
            out += " " + s
        else:
            break
    words = out.split()
    if len(words) > max_words:
        out = " ".join(words[:max_words]).rstrip(",;:") + "\u2026"
    return out


def _is_emptyish(val):
    if val is None:
        return True
    if isinstance(val, str):
        stripped = val.strip().lower()
        return stripped in {"", "none", "null", "waiting", "n/a"}
    if isinstance(val, (list, tuple, set, dict)):
        return len(val) == 0
    return False


def _short_focus_label(text, limit=34):
    """Show the real line of code (shortened at a character limit), not a generic label."""
    text = " ".join(str(text or "").split())
    if not text:
        return "Current step"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _compact_value_description(value):
    value = _normalize_value(value)

    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    if isinstance(value, tuple):
        return f"tuple[{len(value)}]"
    if isinstance(value, str) and len(value) > 26:
        return value[:23] + "..."
    return str(value)


def _diff_variables(prev_vars, curr_vars):
    changed = set()
    prev_vars = prev_vars or {}
    curr_vars = curr_vars or {}

    all_keys = set(prev_vars.keys()) | set(curr_vars.keys())
    for k in all_keys:
        if _normalize_value(prev_vars.get(k)) != _normalize_value(curr_vars.get(k)):
            changed.add(k)
    return changed


def _extract_changed_items(prev_vars, curr_vars, limit=4):
    changed_keys = list(_diff_variables(prev_vars, curr_vars))
    items = []
    for k in changed_keys[:limit]:
        items.append((k, curr_vars.get(k)))
    return items


def _extract_non_empty_items(curr_vars, limit=4):
    items = []
    for k, v in curr_vars.items():
        if not _is_emptyish(v):
            items.append((k, v))
        if len(items) >= limit:
            break
    return items


def _extract_array_like(variables):
    for v in variables.values():
        arr = _value_as_list(v)
        if arr is not None:
            return arr
    return None


def _extract_named_array(variables):
    for k, v in variables.items():
        arr = _value_as_list(v)
        if arr is not None:
            return str(k), arr
    return None, None


def _extract_loop_counter(variables):
    preferred = ["i", "j", "k", "index", "idx", "count", "step"]
    normalized = {str(k).lower(): _normalize_value(v) for k, v in variables.items()}

    for key in preferred:
        if key in normalized and isinstance(normalized[key], (int, float)):
            return key, normalized[key]

    for k, v in normalized.items():
        if isinstance(v, (int, float)):
            return k, v

    return None, None


def _extract_stack_values(variables):
    for key, v in variables.items():
        norm = _normalize_value(v)
        if "stack" in str(key).lower() and isinstance(norm, list):
            return norm
    arr = _extract_array_like(variables)
    if arr is not None:
        return arr
    return []


def _extract_condition_info(variables):
    for key, v in variables.items():
        norm = _normalize_value(v)
        if isinstance(norm, bool):
            return str(key), bool(norm)
    return None, None


def _extract_recursion_depth(variables):
    best = 1
    for k, v in variables.items():
        norm = _normalize_value(v)
        if isinstance(norm, int) and ("n" in str(k).lower() or "depth" in str(k).lower() or "level" in str(k).lower()):
            best = max(1, min(6, abs(norm)))
    return best


def _extract_array_from_code(full_code):
    matches = re.findall(r"(\w+)\s*=\s*(\[[^\n]+\])", full_code)
    for name, arr_text in matches:
        try:
            arr = ast.literal_eval(arr_text)
            if isinstance(arr, list):
                return name, arr
        except Exception:
            pass
    return None, None


def _extract_linked_list_output(curr_vars, output_text):
    candidates = []
    if output_text:
        candidates.append(str(output_text))

    for _, v in curr_vars.items():
        val = str(v)
        if "->" in val:
            candidates.append(val)

    for c in candidates:
        cleaned = c.strip()
        if "->" in cleaned:
            return cleaned
    return None


def _parse_linked_list_values(text):
    if not text:
        return []
    cleaned = str(text).replace("None", "").strip()
    parts = [p.strip() for p in cleaned.split("->")]
    return [p for p in parts if p][:8]


def _extract_result_value(curr_vars, output_text="", narration=""):
    for key in ["result", "answer", "index", "found_index", "mid"]:
        if key in curr_vars:
            value = _safe_int(curr_vars.get(key))
            if value is not None:
                return value

    joined = f"{output_text} {narration}"
    match = re.search(r"index\s*[:=]?\s*(-?\d+)", joined, re.IGNORECASE)
    if match:
        return _safe_int(match.group(1))

    match = re.search(r"found.*?(\d+)", joined, re.IGNORECASE)
    if match:
        return _safe_int(match.group(1))

    return None


def _looks_like_print_scene(focus_text, narration, output_text):
    text = " ".join([str(focus_text), str(narration), str(output_text)]).lower()
    return any(word in text for word in ["print", "output", "display", "console", "show result", "message"])


def _looks_like_input_scene(focus_text, narration, output_text, curr_vars):
    text = " ".join([str(focus_text), str(narration), str(output_text)]).lower()
    if any(word in text for word in ["input", "enter choice", "waiting", "menu", "prompt", "user choice"]):
        return True

    for key, value in curr_vars.items():
        if str(key).lower() in {"choice", "option", "selection", "user_input"} and _is_emptyish(value):
            return True

    return False


def _looks_like_return_scene(focus_text, narration, output_text):
    text = " ".join([str(focus_text), str(narration), str(output_text)]).lower()
    return "return" in text


def _extract_print_literal_from_focus_line(focus_text):
    text = str(focus_text or "").strip()
    if not text:
        return ""

    matches = re.findall(r'["\']([^"\']{1,100})["\']', text)
    if matches:
        return " ".join(matches[:2]).strip()

    return ""


def _extract_print_lines_from_code(full_code):
    lines = []
    for raw in str(full_code).splitlines():
        line = raw.strip()
        if "print(" in line:
            parts = re.findall(r'["\']([^"\']{1,120})["\']', line)
            if parts:
                txt = " ".join(parts).strip()
                if txt:
                    lines.append(txt)

    cleaned = []
    seen = set()
    for line in lines:
        if line not in seen:
            cleaned.append(line)
            seen.add(line)
    return cleaned[:8]


def _extract_print_text(curr_vars, output_text, narration, focus_text="", full_code=""):
    for key in ["output", "message", "result", "printed", "text"]:
        if key in curr_vars and not _is_emptyish(curr_vars[key]):
            return _safe_repr(curr_vars[key], 80)

    if output_text and len(str(output_text).strip()) > 0 and str(output_text).strip().lower() not in {"none", "null"}:
        return str(output_text).strip()

    focus_literal = _extract_print_literal_from_focus_line(focus_text)
    if focus_literal:
        return focus_literal

    code_prints = _extract_print_lines_from_code(full_code)
    if code_prints:
        return code_prints[-1]

    if narration and len(str(narration).strip()) > 0:
        return str(narration).strip()

    return "Program produced output."


def _extract_waiting_label(curr_vars, focus_text, narration):
    for key in ["choice", "option", "selection", "user_input"]:
        if key in curr_vars:
            return f"Waiting for {key}"

    text = " ".join([str(focus_text), str(narration)]).lower()
    if "menu" in text:
        return "Waiting for menu input"
    if "input" in text:
        return "Waiting for user input"
    return "Waiting for input"


def _extract_menu_lines_from_code(full_code):
    lines = []
    for raw in str(full_code).splitlines():
        line = raw.strip()
        if "print(" in line:
            m = re.search(r'print\((["\'])(.*?)\1', line)
            if m:
                txt = m.group(2).strip()
                if 0 < len(txt) <= 40:
                    lines.append(txt)

    cleaned = []
    seen = set()
    for line in lines:
        if line not in seen:
            cleaned.append(line)
            seen.add(line)
    return cleaned[:4]


def _has_recursion(code):
    """True only if a function really calls itself (a call from outside its body does not count)."""
    code = str(code or "")
    try:
        tree = ast.parse(code)
    except Exception:
        tree = None
    if tree is not None:
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call):
                        f = node.func
                        if (isinstance(f, ast.Name) and f.id == fn.name) or (isinstance(f, ast.Attribute) and f.attr == fn.name and isinstance(f.value, ast.Name) and f.value.id == "self"):
                            return True
        return False
    return False


def _detect_algorithm(full_code, scene_type, variables):
    code_lower = str(full_code or "").lower()
    scene_lower = str(scene_type or "").lower()
    keys = {str(k).lower() for k in variables.keys()}

    if (
        "class node" in code_lower
        or "class linkedlist" in code_lower
        or ".next" in code_lower
        or "temp = temp.next" in code_lower
        or "head" in keys
    ):
        return "linked_list"

    if "binary_search" in code_lower or ("low" in keys and "high" in keys and "mid" in keys):
        return "binary_search"

    if any(word in code_lower for word in ["bubble", "selection", "insertion", "sort", "sorted"]):
        return "sorting"

    if "stack" in code_lower or "stack" in scene_lower:
        return "stack"

    if _has_recursion(full_code):
        return "recursion"

    if "for " in code_lower or "while " in code_lower or "loop" in scene_lower:
        return "loop"

    if "if " in code_lower or "elif " in code_lower or "condition" in scene_lower:
        return "condition"

    if _extract_array_like(variables) is not None:
        return "array"

    return "default"


def _build_generic_payload(full_code, prev_vars, curr_vars, output_text, narration, focus_text):
    changed_items = _extract_changed_items(prev_vars, curr_vars, limit=4)

    if _looks_like_print_scene(focus_text, narration, output_text):
        return {
            "action": "print_output",
            "text": _extract_print_text(curr_vars, output_text, narration, focus_text, full_code)
        }

    if _looks_like_input_scene(focus_text, narration, output_text, curr_vars):
        return {
            "action": "waiting_input",
            "label": _extract_waiting_label(curr_vars, focus_text, narration),
            "menu_lines": _extract_menu_lines_from_code(full_code)
        }

    if len(changed_items) == 1:
        key, value = changed_items[0]
        norm = _normalize_value(value)

        if isinstance(norm, list):
            prev_norm = _normalize_value(prev_vars.get(key, []))
            prev_len = len(prev_norm) if isinstance(prev_norm, list) else 0
            curr_len = len(norm)
            if curr_len > prev_len:
                return {
                    "key": key,
                    "action": "list_append",
                    "items": norm[-6:],
                    "added": norm[-1] if norm else ""
                }
            if curr_len < prev_len:
                return {
                    "key": key,
                    "action": "list_remove",
                    "items": norm[-6:]
                }
            return {
                "key": key,
                "action": "list_update",
                "items": norm[-6:]
            }

        if isinstance(norm, dict):
            prev_norm = _normalize_value(prev_vars.get(key, {}))
            prev_keys = set(prev_norm.keys()) if isinstance(prev_norm, dict) else set()
            curr_keys = set(norm.keys())
            added_keys = list(curr_keys - prev_keys)
            changed_keys = []
            for sub_key in curr_keys & prev_keys:
                if prev_norm.get(sub_key) != norm.get(sub_key):
                    changed_keys.append(sub_key)
            return {
                "key": key,
                "action": "dict_update",
                "items": norm,
                "added_keys": added_keys[:4],
                "changed_keys": changed_keys[:4]
            }

        return {
            "key": key,
            "action": "scalar_update",
            "value": _compact_value_description(norm)
        }

    key, value = _extract_loop_counter(curr_vars)
    if key is not None and len(changed_items) > 0:
        return {
            "key": key,
            "action": "loop_progress",
            "value": value,
            "changed_items": changed_items
        }

    return {
        "action": "generic_data",
        "changed_items": changed_items if changed_items else _extract_non_empty_items(curr_vars, limit=3),
        "summary": output_text or narration
    }


def _infer_visual_type_from_payload(payload):
    action = str(payload.get("action", "")).lower()
    mapping = {
        "scalar_update": "scalar_update",
        "list_append": "list_append",
        "list_remove": "list_remove",
        "list_update": "list_iteration",
        "dict_update": "dict_update",
        "loop_progress": "loop_progress",
        "print_output": "print_output",
        "waiting_input": "waiting_input",
        "function_return": "function_return",
        "function_call": "function_call",
        "branch_true": "conditional_branch",
        "branch_false": "conditional_branch",
        "linked_structure": "linked_structure",
        "search_window": "search_window",
        "swap_operation": "swap_operation",
        "generic_data": "generic_data",
    }
    return mapping.get(action, "")


def _create_gradient_background(width, height):
    img = Image.new("RGB", (width, height), "#071b2b")
    draw = ImageDraw.Draw(img)

    top = _hex_to_rgb("#071b2b")
    bottom = _hex_to_rgb("#123f50")

    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(top[0] + (bottom[0] - top[0]) * ratio)
        g = int(top[1] + (bottom[1] - top[1]) * ratio)
        b = int(top[2] + (bottom[2] - top[2]) * ratio)
        draw.line((0, y, width, y), fill=(r, g, b))

    return img


def _is_result_scene(scene_title, output_text, narration):
    text = f"{scene_title} {output_text} {narration}".lower()

    keywords = [
        "found",
        "result",
        "success",
        "completed",
        "finished",
        "done",
        "returned"
    ]

    return any(k in text for k in keywords)


def _choose_layout(scene_title, visual_type, output_text, narration):
    if _is_result_scene(scene_title, output_text, narration):
        return "result"

    if visual_type in ["print_output"]:
        return "terminal"

    return "standard"


def _detect_complexity(code):
    """Time/space complexity from a real AST analysis (see complexity.py)."""
    try:
        return complexity_analyze(_clean_code(code) if isinstance(code, str) else str(code or ""))
    except Exception as e:  # never break video generation because of analysis
        print("complexity analysis failed:", e)
        return {"algorithm": "Unknown", "category": "General", "time_complexity": "Unavailable",
                "space_complexity": "Unavailable", "best_case": "–", "worst_case": "–",
                "key_operations": "–", "explanation": "Could not analyse this code.", "confidence": "low"}


def _translate_text(text, language):
    text = str(text or "").strip()
    lang = _normalize_language(language)

    if not text:
        return text

    if lang == "english":
        return text

    try:
        prompt = f"""
Translate the following educational programming sentence into {_get_groq_language_name(lang)}.

Rules:
- keep it short
- keep the meaning same
- keep beginner-friendly tone
- do not add extra explanation
- return only translated text
- no quotes
- keep technical words like Python, variable, loop, function, print as-is when natural

Text:
{text}
"""

        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )

        translated = str(completion.choices[0].message.content or "").strip()
        translated = translated.replace('"', "").replace("'", "").strip()
        return translated if translated else text
    except Exception as e:
        print("Translation error:", e)
        return text


# -------------------------------
# Background and main panels
# -------------------------------

def _build_background(scene_title, output_path):
    img = _create_gradient_background(VIDEO_W, VIDEO_H)
    draw = ImageDraw.Draw(img)

    draw.text((48, 22), "CODE2VIDEO AI STUDIO", font=FONT_XL, fill=(255, 255, 255))
    draw.text((50, 66), scene_title[:48], font=FONT_MD, fill=(153, 238, 255))

    draw.line((0, 100, VIDEO_W, 100), fill=(255, 255, 255, 28), width=1)

    for x, col in zip([VIDEO_W - 150, VIDEO_W - 118, VIDEO_W - 86], [(45, 212, 191), (96, 165, 250), (167, 139, 250)]):
        draw.ellipse((x, 38, x + 12, 50), fill=col)

    img.save(output_path)


POS_CODE, POS_VARS, POS_CONCEPT, POS_SUB = (42, 108), (894, 108), (42, 450), (116, 600)

_CODE_METRICS = {}


def _make_code_style():
    from pygments.styles import get_style_by_name
    try:
        base = get_style_by_name("one-dark")
    except Exception:
        base = get_style_by_name("monokai")

    class C2VStyle(base):
        background_color = "#0c1424"

    return C2VStyle


_CODE_STYLE = _make_code_style()


def _code_formatter(font_size, start=1):
    return ImageFormatter(style=_CODE_STYLE, font_size=font_size, line_numbers=True, line_number_start=start,
                          line_number_bg="#0c1424", line_number_fg="#4b5b78", line_number_separator=False)


def _code_metrics(font_size=20):
    """(line step in px, top padding in px) of pygments' image output, measured not guessed."""
    if font_size not in _CODE_METRICS:
        lexer = PythonLexer(stripnl=False)
        h1 = Image.open(BytesIO(highlight("x", lexer, _code_formatter(font_size)))).height
        h2 = Image.open(BytesIO(highlight("x\ny", lexer, _code_formatter(font_size)))).height
        step = max(1, h2 - h1)
        _CODE_METRICS[font_size] = (step, max(0, h1 - step))
    return _CODE_METRICS[font_size]


def _resolve_active_line(code, line_index, focus_text=""):
    """Find the code line a step is about. The AI's line_index is often off by one, so the
    focus text (the line it quotes) is used to snap to the right line."""
    lines = str(code).split("\n")
    try:
        idx = int(line_index)
    except Exception:
        idx = 0
    idx = max(0, min(idx, len(lines) - 1))
    focus = " ".join(str(focus_text or "").split())
    if len(focus) < 3:
        return idx
    norm = [" ".join(l.split()) for l in lines]
    cands = [i for i, l in enumerate(norm) if l and (l == focus or focus in l or (len(l) >= 4 and l in focus))]
    if cands:
        return min(cands, key=lambda i: abs(i - idx))
    import difflib
    best, best_r = None, 0.0
    for i, l in enumerate(norm):
        if not l:
            continue
        r = difflib.SequenceMatcher(None, focus, l).ratio()
        if r > best_r:
            best, best_r = i, r
    return best if best is not None and best_r >= 0.6 else idx


def _build_code_overlay(full_code, active_line, focus_text, output_path):
    short_focus = _short_focus_label(focus_text)
    all_lines = str(full_code).split("\n")
    total = len(all_lines)
    active_line = max(0, min(int(active_line), total - 1))

    panel = Image.new("RGBA", (760, 355), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)

    shadow = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((12, 14, 748, 343), radius=28, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    panel.alpha_composite(shadow)

    _rounded_box(draw, (0, 0, 736, 330), fill=(10, 21, 37, 238), outline=(79, 209, 197, 120), width=2, radius=28)

    draw.text((28, 18), "CODE FLOW", font=FONT_LG, fill=(255, 255, 255))
    draw.text((28, 52), short_focus, font=FONT_SM, fill=(150, 226, 255))

    max_w, area_h = 676, 196
    longest = max((len(l.expandtabs(4)) for l in all_lines), default=1)
    want = min(total, 6)
    font_size = 23
    for fs in (31, 28, 26, 24):
        st, pd = _code_metrics(fs)
        if want * st + 2 * pd <= area_h and (longest + 5) * fs * 0.6 <= max_w:
            font_size = fs
            break
    step, pad = _code_metrics(font_size)
    max_lines = max(4, min(8, (area_h - 2 * pad) // step))

    # show a window of lines around the active line instead of shrinking the whole program
    if total <= max_lines:
        start, end = 0, total
    else:
        start = max(0, min(active_line - max_lines // 2, total - max_lines))
        end = start + max_lines
    window = "\n".join(l if l.strip() else " " for l in all_lines[start:end])

    code_img = Image.open(BytesIO(highlight(window, PythonLexer(stripnl=False), _code_formatter(font_size, start + 1)))).convert("RGBA")
    scale = 1.0
    if code_img.width > max_w:
        scale = max_w / code_img.width
        code_img = code_img.resize((max_w, max(1, int(code_img.height * scale))), Image.Resampling.LANCZOS)

    code_x, code_y = 28, 80
    bg_h = code_img.height + 16
    _rounded_box(draw, (code_x - 10, code_y - 8, code_x + max(code_img.width, 380) + 10, code_y - 8 + bg_h), fill=(12, 20, 36, 255), radius=12)
    panel.alpha_composite(code_img, (code_x, code_y))

    # translucent band + accent bar on the active line
    row = active_line - start
    row_top = code_y + int((pad + row * step) * scale)
    row_h = max(2, int(step * scale))
    band_w = max(code_img.width, 380)
    glow = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.rectangle((code_x - 2, row_top, code_x + band_w, row_top + row_h), fill=(45, 212, 191, 55))
    gd.rectangle((code_x - 2, row_top, code_x + 2, row_top + row_h), fill=(94, 234, 212, 255))
    panel.alpha_composite(glow)

    if total > max_lines:
        tag = f"lines {start + 1}\u2013{end} of {total}"
        tb = draw.textbbox((0, 0), tag, font=FONT_SM)
        draw.text((708 - (tb[2] - tb[0]), 22), tag, font=FONT_SM, fill=(150, 226, 255))

    badge_box = draw.textbbox((0, 0), short_focus, font=FONT_CODE_BADGE)
    badge_w = min(560, badge_box[2] - badge_box[0] + 34)
    _rounded_box(draw, (28, 286, 28 + badge_w, 316), fill=(20, 94, 115, 235), outline=(94, 224, 213), width=2, radius=14)
    draw.text((42, 293), short_focus, font=FONT_CODE_BADGE, fill=(236, 255, 255))

    panel.save(output_path)


_GENERIC_TAKEAWAYS = {"", "none", "null", "program state updated.", "program state updated", "the program continues logically.",
                      "the program continues logically", "program continues", "continues", "no output", "program produced output."}


def _make_takeaway(output_text, focus_text, variables, changed_keys):
    """A short, specific one-liner for the KEY TAKEAWAY box (never a vague filler sentence)."""
    text = " ".join(str(output_text or "").split())
    if text.lower() not in _GENERIC_TAKEAWAYS:
        return text
    focus = " ".join(str(focus_text or "").split())
    m = re.match(r"def\s+(\w+)\s*\(([^)]*)\)", focus)
    if m:
        params = ", ".join(p.strip() for p in m.group(2).split(",") if p.strip())
        return f"Defines {m.group(1)}({params}); it runs only when called"
    m = re.match(r"class\s+(\w+)", focus)
    if m:
        return f"Defines the class {m.group(1)}"
    items = [(k, v) for k, v in variables.items() if k in changed_keys and not _is_emptyish(v)][:2]
    if items:
        return ", ".join(f"{k} = {_safe_repr(v, 14)}" for k, v in items)
    if focus:
        return _short_focus_label(focus)
    return "Moving on to the next step"


def _build_vars_overlay(variables, scene_type, output_text, changed_keys, output_path, focus_text=""):
    panel = Image.new("RGBA", (360, 355), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)

    shadow = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((12, 14, 348, 343), radius=28, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    panel.alpha_composite(shadow)

    _rounded_box(draw, (0, 0, 336, 330), fill=(11, 29, 34, 240), outline=(167, 139, 250, 120), width=2, radius=28)

    draw.text((24, 18), "STATE PANEL", font=FONT_LG, fill=(255, 255, 255))
    scene_label = str(scene_type).upper().replace("_", " ")
    draw.text((24, 54), scene_label[:24], font=FONT_BADGE, fill=(167, 139, 250))

    y = 86

    shown_items = []
    for k, v in variables.items():
        if k in changed_keys and not _is_emptyish(v):
            shown_items.append((k, v))

    if not shown_items:
        shown_items = _extract_non_empty_items(variables, limit=3)

    shown_items = shown_items[:2]

    if not shown_items:
        _rounded_box(draw, (24, y, 312, y + 58), fill=(18, 48, 58, 220), outline=(94, 224, 213), width=2, radius=16)
        draw.text((40, y + 17), "No state changed in this step", font=FONT_SM, fill=(218, 240, 247))
        y += 70

    for key, val in shown_items:
        changed = key in changed_keys
        box_fill = (24, 78, 99, 240) if changed else (18, 48, 58, 220)
        outline = (167, 139, 250) if changed else (94, 224, 213)
        title_color = (221, 214, 254) if changed else (221, 214, 254)

        _rounded_box(draw, (24, y, 312, y + 58), fill=box_fill, outline=outline, width=3 if changed else 2, radius=16)
        draw.text((40, y + 8), str(key)[:18], font=FONT_BADGE, fill=title_color)

        value_text = _safe_repr(val, 48)
        lines = _wrap_text(value_text, FONT_VAR, 230, draw)
        for idx, line in enumerate(lines[:2]):
            draw.text((40, y + 28 + idx * 15), line, font=FONT_VAR, fill=(236, 255, 255))
        y += 66

    takeaway = _make_takeaway(output_text, focus_text, variables, changed_keys)

    if "found" in str(output_text).lower():
        result_idx = _extract_result_value(variables, output_text, "")
        takeaway = f"Target found at index {result_idx}" if result_idx is not None else "Target found successfully"

    _rounded_box(draw, (24, 230, 312, 322), fill=(26, 21, 52, 235), outline=(167, 139, 250), width=2, radius=18)
    draw.text((40, 241), "KEY TAKEAWAY", font=FONT_BADGE, fill=(221, 214, 254))
    out_lines = _wrap_text(takeaway, FONT_SM, 250, draw)
    if len(out_lines) > 3:
        out_lines = out_lines[:3]
        out_lines[2] = out_lines[2].rstrip(".,;: ") + "\u2026"
    for idx, line in enumerate(out_lines):
        draw.text((40, 266 + idx * 19), line, font=FONT_SM, fill=(237, 233, 254))

    panel.save(output_path)

def _split_runs(text):
    """Split text into (is_latin, chunk) runs so each run can use a font that has its glyphs."""
    runs = []
    for ch in str(text):
        latin = ord(ch) < 0x250 or ch in "\u2026\u2013\u2014\u2018\u2019\u201c\u201d"
        if ch.isspace() and runs:
            latin = runs[-1][0]
        if runs and runs[-1][0] == latin:
            runs[-1][1] += ch
        else:
            runs.append([latin, ch])
    return runs


def _mixed_width(draw, text, indic_font, latin_font):
    return sum(draw.textlength(chunk, font=(latin_font if latin else indic_font)) for latin, chunk in _split_runs(text))


def _draw_mixed(draw, xy, text, indic_font, latin_font, fill):
    x, y = xy
    for latin, chunk in _split_runs(text):
        font = latin_font if latin else indic_font
        draw.text((x, y), chunk, font=font, fill=fill)
        x += draw.textlength(chunk, font=font)


def _wrap_mixed(text, indic_font, latin_font, max_width, draw):
    words = str(text).split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        trial = current + " " + word
        if _mixed_width(draw, trial, indic_font, latin_font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _build_subtitle_overlay(narration, step_no, total_steps, output_path, language="english"):
    simple = _simplify_subtitle(narration)

    subtitle_font = _load_font(24, bold=False, language=language)
    badge_font = _load_font(18, bold=True, language="english")
    latin_font = _load_font(24, bold=False, language="english")

    panel = Image.new("RGBA", (1040, 92), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)

    shadow = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((8, 8, 1032, 82), radius=18, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    panel.alpha_composite(shadow)

    box_h = 74
    _rounded_box(draw, (0, 0, 1024, box_h), fill=(3, 10, 18, 235), outline=(80, 198, 255, 60), width=2, radius=18)

    badge = f"STEP {step_no}/{total_steps}"
    badge_box = draw.textbbox((0, 0), badge, font=badge_font)
    badge_w = badge_box[2] - badge_box[0] + 22
    by = (box_h - 32) // 2
    _rounded_box(draw, (18, by, 18 + badge_w, by + 32), fill=(18, 75, 95, 235), outline=(94, 224, 213), width=2, radius=10)
    draw.text((30, by + 6), badge, font=badge_font, fill=(237, 255, 255))

    text_x = 18 + badge_w + 22
    lines = _wrap_mixed(simple, subtitle_font, latin_font, 1024 - text_x - 24, draw)
    if len(lines) > 2:
        lines = lines[:2]
        lines[1] = lines[1].rstrip(".,;: ") + "\u2026"
    line_h = 31
    top = (box_h - line_h * len(lines)) // 2 + 1
    for idx, line in enumerate(lines):
        _draw_mixed(draw, (text_x, top + idx * line_h), line, subtitle_font, latin_font, (255, 255, 255))

    panel.save(output_path)


# -------------------------------
# Semantic visuals
# -------------------------------

def _draw_array_boxes(draw, arr, x0, y0, box_w=70, box_h=56, highlight_idxs=None, dim_idxs=None):
    highlight_idxs = set(highlight_idxs or [])
    dim_idxs = set(dim_idxs or [])

    for idx, item in enumerate(arr[:10]):
        x = x0 + idx * (box_w + 8)
        fill = (24, 78, 99, 240)
        outline = (94, 224, 213)
        text_color = (255, 255, 255)

        if idx in dim_idxs:
            fill = (16, 42, 58, 230)
            outline = (66, 130, 150)
            text_color = (205, 220, 230)

        if idx in highlight_idxs:
            fill = (42, 94, 120, 250)
            outline = (167, 139, 250)
            text_color = (237, 233, 254)

        _rounded_box(draw, (x, y0, x + box_w, y0 + box_h), fill=fill, outline=outline, width=3 if idx in highlight_idxs else 2, radius=12)
        txt = str(item)[:7]
        bbox = draw.textbbox((0, 0), txt, font=FONT_BADGE)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((x + box_w / 2 - tw / 2, y0 + box_h / 2 - th / 2 - 2), txt, font=FONT_BADGE, fill=text_color)


def _draw_pointer(draw, label, idx, x0, y_base, box_w=70):
    if idx is None or idx < 0 or idx > 9:
        return
    x = x0 + idx * (box_w + 8) + box_w / 2
    draw.line((x, y_base, x, y_base + 18), fill=(167, 139, 250), width=3)
    draw.polygon([(x, y_base + 18), (x - 7, y_base + 8), (x + 7, y_base + 8)], fill=(167, 139, 250))
    label_bbox = draw.textbbox((0, 0), label, font=FONT_BADGE)
    lw = label_bbox[2] - label_bbox[0]
    _rounded_box(draw, (x - lw / 2 - 10, y_base - 34, x + lw / 2 + 10, y_base - 6), fill=(18, 75, 95, 235), outline=(94, 224, 213), width=2, radius=10)
    draw.text((x - lw / 2, y_base - 30), label, font=FONT_BADGE, fill=(255, 255, 255))


def _draw_old_pointer(draw, label, idx, x0, y_base, box_w=70):
    if idx is None or idx < 0 or idx > 9:
        return
    x = x0 + idx * (box_w + 8) + box_w / 2
    draw.line((x, y_base, x, y_base + 14), fill=(110, 140, 155), width=2)
    draw.polygon([(x, y_base + 14), (x - 5, y_base + 7), (x + 5, y_base + 7)], fill=(110, 140, 155))
    draw.text((x - 12, y_base - 26), label, font=FONT_SM, fill=(150, 170, 180))


def _panel_base(title, subtitle, height=156):
    panel = Image.new("RGBA", (760, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    _rounded_box(draw, (0, 0, 760, height - 8), fill=(8, 27, 38, 220), outline=(94, 224, 213, 120), width=2, radius=18)
    draw.text((22, 8), title, font=FONT_PT, fill=(255, 255, 255))
    draw.text((22, 38), subtitle, font=FONT_SM, fill=(173, 235, 255))
    return panel, draw


def _build_scalar_update_overlay(payload, output_path):
    panel, draw = _panel_base("VALUE CHANGE", "A variable changed in this step")
    key = payload.get("key", "value")
    value = payload.get("value", "")

    _rounded_box(draw, (24, 60, 320, 96), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=12)
    draw.text((40, 70), f"{key}: {value}"[:34], font=FONT_BADGE, fill=(255, 255, 255))
    panel.save(output_path)


def _build_list_change_overlay(payload, output_path):
    action = payload.get("action", "list_update")
    title = "LIST APPEND" if action == "list_append" else ("LIST REMOVE" if action == "list_remove" else "LIST UPDATE")
    panel, draw = _panel_base(title, "List structure changed visually")

    items = payload.get("items", [])[:7]
    added = payload.get("added", "")

    highlight = {len(items) - 1} if action == "list_append" and items else set()
    _draw_array_boxes(draw, items, 24, 54, box_w=78, box_h=40, highlight_idxs=highlight)

    if action == "list_append":
        draw.text((620, 14), f"+ {str(added)[:12]}", font=FONT_BADGE, fill=(221, 214, 254))
    elif action == "list_remove":
        draw.text((604, 14), "item removed", font=FONT_BADGE, fill=(221, 214, 254))

    panel.save(output_path)


def _build_dict_update_overlay(payload, output_path):
    panel, draw = _panel_base("DICT UPDATE", "Keys were added or updated")
    items = payload.get("items", {})
    added_keys = payload.get("added_keys", [])
    changed_keys = payload.get("changed_keys", [])

    x = 24
    y = 58
    shown = list(items.items())[:4]

    for idx, (k, v) in enumerate(shown):
        highlight = (k in added_keys) or (k in changed_keys)
        fill = (42, 94, 120, 250) if highlight else (24, 78, 99, 240)
        outline = (167, 139, 250) if highlight else (94, 224, 213)
        left = x + (idx % 2) * 350
        top = y + (idx // 2) * 44
        _rounded_box(draw, (left, top, left + 320, top + 36), fill=fill, outline=outline, width=3 if highlight else 2, radius=10)
        draw.text((left + 12, top + 8), f"{k}: {_safe_repr(v, 24)}", font=FONT_SM, fill=(255, 255, 255))

    panel.save(output_path)


def _build_loop_progress_overlay(payload, output_path):
    panel, draw = _panel_base("LOOP PROGRESS", "Iteration values change live")
    key = payload.get("key", "i")
    value = payload.get("value", 0)
    changed_items = payload.get("changed_items", [])

    _rounded_box(draw, (24, 58, 180, 96), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=12)
    draw.text((44, 68), f"{key} = {value}", font=FONT_BADGE, fill=(255, 255, 255))

    x = 220
    for item_key, item_val in changed_items[:3]:
        _rounded_box(draw, (x, 58, x + 160, 96), fill=(24, 78, 99, 240), outline=(94, 224, 213), width=2, radius=12)
        draw.text((x + 10, 68), f"{item_key}: {_compact_value_description(item_val)}"[:22], font=FONT_BADGE, fill=(255, 255, 255))
        x += 176

    panel.save(output_path)


def _build_print_output_overlay(payload, output_path):
    panel, draw = _panel_base("OUTPUT CONSOLE", "What the program prints", height=124)

    _rounded_box(draw, (24, 62, 736, 112), fill=(7, 14, 20, 235), outline=(94, 224, 213), width=2, radius=12)
    console_text = _safe_repr(payload.get("text", "Program produced output."), 100)

    lines = _wrap_text(console_text, FONT_MD, 650, draw)
    y0 = 74 if len(lines) == 1 else 66
    for idx, line in enumerate(lines[:2]):
        draw.text((42, y0 + idx * 22), line, font=FONT_MD, fill=(210, 245, 210))

    panel.save(output_path)


def _build_waiting_input_overlay(payload, output_path):
    panel, draw = _panel_base("WAITING FOR INPUT", "Program is waiting for the next user action", height=124)

    label = payload.get("label", "Waiting for input")
    menu_lines = payload.get("menu_lines", [])[:3]

    _rounded_box(draw, (24, 62, 280, 112), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=12)
    lines = _wrap_text(label, FONT_MD, 210, draw)
    for idx, line in enumerate(lines[:2]):
        draw.text((40, 68 + idx * 20), line, font=FONT_MD, fill=(255, 255, 255))

    if menu_lines:
        left = 300
        top = 62
        _rounded_box(draw, (left, top, 736, 112), fill=(7, 14, 20, 235), outline=(94, 224, 213), width=2, radius=12)
        for i, line in enumerate(menu_lines[:2]):
            draw.text((left + 14, top + 6 + i * 20), line[:48], font=FONT_TINY, fill=(210, 245, 210))

    panel.save(output_path)


def _build_function_return_overlay(payload, output_path):
    panel, draw = _panel_base("RETURN VALUE", "A function returned a value")
    value = _safe_repr(payload.get("value", "result"), 32)

    _rounded_box(draw, (54, 58, 246, 96), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=12)
    draw.text((84, 69), value, font=FONT_BADGE, fill=(255, 255, 255))
    draw.line((246, 77, 360, 77), fill=(167, 139, 250), width=3)
    draw.polygon([(360, 77), (350, 71), (350, 83)], fill=(167, 139, 250))
    draw.text((380, 66), "returned to caller", font=FONT_BADGE, fill=(221, 214, 254))

    panel.save(output_path)


def _build_function_call_overlay(payload, output_path):
    panel, draw = _panel_base("FUNCTION CALL", "Execution is entering a function")
    name = payload.get("name", "function")

    _rounded_box(draw, (40, 66, 290, 108), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=12)
    draw.text((58, 76), name[:22], font=FONT_BADGE, fill=(255, 255, 255))
    draw.line((290, 87, 400, 87), fill=(167, 139, 250), width=3)
    draw.polygon([(412, 87), (400, 80), (400, 94)], fill=(167, 139, 250))
    draw.text((428, 77), "entering body", font=FONT_BADGE, fill=(221, 214, 254))

    panel.save(output_path)


def _build_conditional_branch_overlay(payload, output_path):
    outcome = payload.get("outcome", "unknown")
    panel, draw = _panel_base("CONDITION FLOW", "A branch was chosen")

    draw.polygon([(150, 78), (210, 48), (270, 78), (210, 108)], fill=(24, 78, 99, 240), outline=(167, 139, 250))
    draw.text((188, 68), "if", font=FONT_BADGE, fill=(255, 255, 255))

    if outcome == "true":
        true_color = (167, 139, 250)
        false_color = (94, 224, 213)
    elif outcome == "false":
        true_color = (94, 224, 213)
        false_color = (255, 160, 122)
    else:
        true_color = (94, 224, 213)
        false_color = (94, 224, 213)

    draw.line((270, 78, 366, 54), fill=true_color, width=3)
    draw.line((270, 78, 366, 102), fill=false_color, width=3)
    draw.text((374, 46), "true", font=FONT_BADGE, fill=(255, 255, 255))
    draw.text((374, 94), "false", font=FONT_BADGE, fill=(255, 255, 255))

    panel.save(output_path)


def _build_generic_data_overlay(payload, output_path):
    panel, draw = _panel_base("PROGRAM INSIGHT", "Changed data is summarized here")
    changed_items = payload.get("changed_items", [])

    if not changed_items:
        draw.text((24, 66), _safe_repr(payload.get("summary", "No major data change in this step."), 88), font=FONT_MD, fill=(255, 255, 255))
    else:
        x = 24
        for key, value in changed_items[:3]:
            _rounded_box(draw, (x, 62, x + 220, 98), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=12)
            draw.text((x + 12, 71), f"{key}: {_compact_value_description(value)}"[:26], font=FONT_BADGE, fill=(255, 255, 255))
            x += 236

    panel.save(output_path)


# -------------------------------
# Optional specialized visuals
# -------------------------------

def _build_binary_search_overlay(curr_vars, prev_vars, full_code, output_text, narration, output_path):
    _, arr = _extract_named_array(curr_vars)
    if arr is None:
        _, arr = _extract_array_from_code(full_code)
    if arr is None:
        arr = [1, 3, 5, 7, 9]
    arr = arr[:10]
    n = len(arr)

    low = _safe_int(curr_vars.get("low"))
    high = _safe_int(curr_vars.get("high"))
    mid = _safe_int(curr_vars.get("mid"))
    target = curr_vars.get("target", "")
    tnorm = _normalize_value(target)

    def _is_target(i):
        return i is not None and 0 <= i < n and target != "" and str(_normalize_value(arr[i])) == str(tnorm)

    # "found" only when the middle element really equals the target (or the step reports that index)
    found_idx = None
    if _is_target(mid):
        found_idx = mid
    else:
        explicit = _safe_int(curr_vars.get("result") if "result" in curr_vars else curr_vars.get("found_index"))
        if _is_target(explicit):
            found_idx = explicit

    empty_range = low is not None and high is not None and low > high and found_idx is None

    if found_idx is not None:
        title, sub = "SEARCH RESULT", f"Target {target} found at index {found_idx}"
    elif empty_range:
        title, sub = "SEARCH WINDOW", "Search range is empty, so the target is not in the list"
    else:
        title, sub = "SEARCH WINDOW", "L = low   M = mid   H = high"

    panel, draw = _panel_base(title, sub, height=156)
    if target != "":
        tt = f"target = {target}"[:20]
        tb = draw.textbbox((0, 0), tt, font=FONT_BADGE)
        draw.text((738 - (tb[2] - tb[0]), 14), tt, font=FONT_BADGE, fill=(221, 214, 254))

    bw = min(70, int((716 - 8 * (n - 1)) / max(1, n)))
    x0, y_box, bh = 22, 87, 42
    active = set(range(max(0, low), min(n - 1, high) + 1)) if (low is not None and high is not None) else set(range(n))

    for idx, item in enumerate(arr):
        x = x0 + idx * (bw + 8)
        fill, outline, tcol, w = (24, 78, 99, 240), (94, 224, 213), (255, 255, 255), 2
        if found_idx is not None:
            if idx == found_idx:
                fill, outline, tcol, w = (20, 110, 70, 250), (167, 139, 250), (255, 255, 255), 3
            else:
                fill, outline, tcol = (16, 42, 58, 230), (66, 130, 150), (170, 190, 200)
        elif idx not in active:
            fill, outline, tcol = (16, 42, 58, 230), (66, 130, 150), (150, 170, 180)
        elif idx == mid:
            fill, outline, w = (42, 94, 120, 250), (167, 139, 250), 3
        _rounded_box(draw, (x, y_box, x + bw, y_box + bh), fill=fill, outline=outline, width=w, radius=10)
        txt = str(item)[:6]
        bb = draw.textbbox((0, 0), txt, font=FONT_BADGE)
        draw.text((x + bw / 2 - (bb[2] - bb[0]) / 2, y_box + bh / 2 - (bb[3] - bb[1]) / 2 - 3), txt, font=FONT_BADGE, fill=tcol)
        ib = draw.textbbox((0, 0), str(idx), font=FONT_SM)
        draw.text((x + bw / 2 - (ib[2] - ib[0]) / 2, y_box + bh + 4), str(idx), font=FONT_SM, fill=(120, 150, 165))

    # pointer labels above the boxes (labels that share a cell are merged, e.g. "L M")
    labels = {}
    if found_idx is not None:
        labels[found_idx] = ["FOUND"]
    else:
        for name, val in (("L", low), ("M", mid), ("H", high)):
            if val is not None and 0 <= val < n:
                labels.setdefault(val, []).append(name)
    for idx, names in labels.items():
        label = " ".join(names)
        cx = x0 + idx * (bw + 8) + bw / 2
        lb = draw.textbbox((0, 0), label, font=FONT_BADGE)
        lw = lb[2] - lb[0]
        good = found_idx is not None
        _rounded_box(draw, (cx - lw / 2 - 10, 58, cx + lw / 2 + 10, 78),
                     fill=(20, 110, 70, 240) if good else (18, 75, 95, 235),
                     outline=(167, 139, 250) if good else (94, 224, 213), width=2, radius=8)
        draw.text((cx - lw / 2, 59), label, font=FONT_BADGE, fill=(255, 255, 255))
        draw.polygon([(cx, y_box - 2), (cx - 6, y_box - 10), (cx + 6, y_box - 10)], fill=(167, 139, 250))

    panel.save(output_path)


def _build_sorting_overlay(curr_vars, prev_vars, full_code, output_path):
    panel, draw = _panel_base("SORTING VISUAL", "Changed bars show swap or compare")

    _, curr_arr = _extract_named_array(curr_vars)
    if curr_arr is None:
        _, curr_arr = _extract_array_from_code(full_code)
    if curr_arr is None:
        curr_arr = [5, 1, 4, 2, 8]

    _, prev_arr = _extract_named_array(prev_vars or {})
    if prev_arr is None:
        prev_arr = curr_arr

    changed_idxs = []
    for idx, (a, b) in enumerate(zip(prev_arr[:10], curr_arr[:10])):
        if a != b:
            changed_idxs.append(idx)

    i_idx = _safe_int(curr_vars.get("i"))
    j_idx = _safe_int(curr_vars.get("j"))
    compare_idxs = set(i for i in [i_idx, j_idx] if i is not None)
    compare_idxs.update(changed_idxs)

    base_y = 140
    x0 = 22
    max_val = max([1] + [abs(int(x)) if isinstance(x, (int, float)) or str(x).isdigit() else 1 for x in curr_arr[:10]])

    for idx, item in enumerate(curr_arr[:10]):
        try:
            value_num = abs(int(item))
        except Exception:
            value_num = idx + 1
        x = x0 + idx * 72
        h = max(22, int((value_num / max_val) * 62))
        y = base_y - h

        fill = (24, 78, 99, 240)
        outline = (94, 224, 213)
        if idx in compare_idxs:
            fill = (42, 94, 120, 250)
            outline = (167, 139, 250)

        _rounded_box(draw, (x, y, x + 54, base_y), fill=fill, outline=outline, width=3 if idx in compare_idxs else 2, radius=10)

        txt = str(item)[:4]
        bbox = draw.textbbox((0, 0), txt, font=FONT_BADGE)
        tw = bbox[2] - bbox[0]
        draw.text((x + 27 - tw / 2, y + 2), txt, font=FONT_BADGE, fill=(255, 255, 255))

    if len(changed_idxs) >= 2:
        v1, v2 = curr_arr[changed_idxs[0]], curr_arr[changed_idxs[1]]
        tag = f"swap  {v1} \u21c4 {v2}"
        tb = draw.textbbox((0, 0), tag, font=FONT_BADGE)
        draw.text((738 - (tb[2] - tb[0]), 14), tag, font=FONT_BADGE, fill=(221, 214, 254))

    panel.save(output_path)


def _build_stack_overlay(curr_vars, prev_vars, output_path):
    panel, draw = _panel_base("STACK FRAME", "Push and pop are shown on the top frame")

    curr_stack = _extract_stack_values(curr_vars)
    prev_stack = _extract_stack_values(prev_vars or {})
    if not curr_stack and not prev_stack:
        curr_stack = []

    action = "steady"
    moving_value = None

    if len(curr_stack) > len(prev_stack):
        action = "push"
        moving_value = curr_stack[-1]
    elif len(curr_stack) < len(prev_stack):
        action = "pop"
        moving_value = prev_stack[-1] if prev_stack else None

    tower_x = 560
    base_y = 142

    stable_stack = curr_stack[:]
    if action == "push" and stable_stack:
        stable_stack = stable_stack[:-1]

    for idx, val in enumerate(stable_stack[-4:]):
        y = base_y - (len(stable_stack[-4:]) - idx) * 26
        _rounded_box(draw, (tower_x, y, tower_x + 130, y + 22), fill=(24, 78, 99, 240), outline=(94, 224, 213), width=2, radius=8)
        draw.text((tower_x + 14, y + 2), str(_normalize_value(val))[:12], font=FONT_BADGE, fill=(255, 255, 255))

    top_slot_y = base_y - len(stable_stack[-4:]) * 26

    if action == "push" and moving_value is not None:
        _rounded_box(draw, (400, 62, 530, 86), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=8)
        draw.text((414, 66), f"incoming {str(_normalize_value(moving_value))}"[:16], font=FONT_BADGE, fill=(255, 255, 255))
        draw.line((530, 74, tower_x, top_slot_y + 10), fill=(167, 139, 250), width=3)
        draw.polygon([(tower_x, top_slot_y + 10), (tower_x + 8, top_slot_y + 5), (tower_x + 8, top_slot_y + 15)], fill=(167, 139, 250))
        _rounded_box(draw, (tower_x, top_slot_y, tower_x + 130, top_slot_y + 22), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=8)
        draw.text((tower_x + 14, top_slot_y + 2), str(_normalize_value(moving_value))[:12], font=FONT_BADGE, fill=(255, 255, 255))
    elif action == "pop" and moving_value is not None:
        pop_y = base_y - max(1, len(prev_stack[-4:])) * 26
        _rounded_box(draw, (tower_x, pop_y, tower_x + 130, pop_y + 22), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=8)
        draw.text((tower_x + 14, pop_y + 2), str(_normalize_value(moving_value))[:12], font=FONT_BADGE, fill=(255, 255, 255))
        _rounded_box(draw, (400, 62, 530, 86), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=8)
        draw.text((416, 66), f"popped {str(_normalize_value(moving_value))}"[:15], font=FONT_BADGE, fill=(255, 255, 255))
        draw.line((tower_x, pop_y + 10, 530, 74), fill=(167, 139, 250), width=3)
        draw.polygon([(530, 74), (538, 70), (538, 78)], fill=(167, 139, 250))

    panel.save(output_path)


def _build_linked_list_overlay(curr_vars, prev_vars, output_text, output_path):
    panel, draw = _panel_base("LINKED LIST TRAVERSAL", "Nodes are visited in sequence")

    chain_text = _extract_linked_list_output(curr_vars, output_text)
    values = _parse_linked_list_values(chain_text)

    if not values:
        arr = _extract_array_like(curr_vars)
        if arr:
            values = [str(x) for x in arr[:6]]
        else:
            values = ["10", "20", "30"]

    x = 26
    y = 96
    box_w = 78
    current_idx = 0

    temp_val = curr_vars.get("temp")
    if temp_val is not None:
        temp_str = str(_normalize_value(temp_val))
        for idx, val in enumerate(values):
            if temp_str == str(val):
                current_idx = idx
                break

    prev_temp_val = (prev_vars or {}).get("temp")
    prev_idx = None
    if prev_temp_val is not None:
        prev_temp_str = str(_normalize_value(prev_temp_val))
        for idx, val in enumerate(values):
            if prev_temp_str == str(val):
                prev_idx = idx
                break

    for idx, val in enumerate(values[:6]):
        fill = (42, 94, 120, 250) if idx == current_idx else (24, 78, 99, 240)
        outline = (167, 139, 250) if idx == current_idx else (94, 224, 213)

        _rounded_box(draw, (x, y, x + box_w, y + 36), fill=fill, outline=outline, width=3 if idx == current_idx else 2, radius=12)
        bbox = draw.textbbox((0, 0), str(val), font=FONT_BADGE)
        tw = bbox[2] - bbox[0]
        draw.text((x + box_w / 2 - tw / 2, y + 8), str(val)[:8], font=FONT_BADGE, fill=(255, 255, 255))

        if idx < len(values[:6]) - 1:
            start_x = x + box_w
            end_x = x + box_w + 28
            draw.line((start_x + 6, y + 18, end_x, y + 18), fill=(167, 139, 250), width=3)
            draw.polygon([(end_x, y + 18), (end_x - 8, y + 14), (end_x - 8, y + 22)], fill=(167, 139, 250))

        x += 112

    if prev_idx is not None and prev_idx != current_idx:
        old_x = 26 + prev_idx * 112 + box_w / 2
        draw.text((old_x - 12, 66), "old", font=FONT_SM, fill=(150, 170, 180))

    current_x = 26 + current_idx * 112 + box_w / 2
    _rounded_box(draw, (current_x - 28, 58, current_x + 28, 80), fill=(18, 75, 95, 235), outline=(94, 224, 213), width=2, radius=10)
    draw.text((current_x - 18, 61), "temp", font=FONT_SM, fill=(255, 255, 255))
    draw.line((current_x, 80, current_x, 92), fill=(167, 139, 250), width=3)
    draw.polygon([(current_x, 94), (current_x - 7, 85), (current_x + 7, 85)], fill=(167, 139, 250))

    panel.save(output_path)


def _build_recursion_overlay(curr_vars, prev_vars, output_text, output_path):
    return_mode = "return" in str(output_text).lower() or "return" in " ".join(curr_vars.keys()).lower()
    title = "RECURSION RETURN" if return_mode else "RECURSION TREE"
    subtitle = "Return values move upward" if return_mode else "Calls branch deeper"

    panel, draw = _panel_base(title, subtitle)

    depth = _extract_recursion_depth(curr_vars)
    centers = [(84, 102), (204, 84), (204, 126), (332, 76), (332, 106), (332, 134)]
    nodes = min(max(depth + 1, 2), len(centers))

    for i, (cx, cy) in enumerate(centers[:nodes]):
        if i > 0:
            px, py = centers[(i - 1) // 2]
            color = (167, 139, 250) if return_mode else (94, 224, 213)
            draw.line((px + 28, py, cx - 28, cy), fill=color, width=3)

        fill = (42, 94, 120, 250) if return_mode and i == 0 else (24, 78, 99, 240)
        outline = (167, 139, 250)
        draw.ellipse((cx - 24, cy - 13, cx + 24, cy + 13), fill=fill, outline=outline, width=2)
        draw.text((cx - 11, cy - 8), f"f{i}", font=FONT_BADGE, fill=(255, 255, 255))

    panel.save(output_path)


# -------------------------------
# Planner and renderer routing
# -------------------------------

def _fallback_visual_plan(full_code, scene_type, focus_text, output_text, narration, prev_vars, curr_vars):
    algorithm = _detect_algorithm(full_code, scene_type, curr_vars)

    if algorithm == "binary_search":
        result_idx = _extract_result_value(curr_vars, output_text, narration)
        low = _safe_int(curr_vars.get("low"))
        high = _safe_int(curr_vars.get("high"))
        mid = _safe_int(curr_vars.get("mid"))

        found_by_vars = (
            (result_idx is not None and result_idx >= 0)
            or (mid is not None and "found" in str(output_text).lower())
            or ("found" in str(narration).lower())
            or (
                low is not None
                and high is not None
                and mid is not None
                and "target found" in str(scene_type).lower()
            )
        )

        if found_by_vars:
            return {
                "visual_type": "special_binary_search",
                "payload": {"mode": "found"}
            }

        return {
            "visual_type": "special_binary_search",
            "payload": {"mode": "window"}
        }

    if algorithm == "sorting":
        return {"visual_type": "special_sorting", "payload": {}}
    if algorithm == "stack":
        return {"visual_type": "special_stack", "payload": {}}
    if algorithm == "linked_list":
        return {"visual_type": "special_linked_list", "payload": {}}
    if algorithm == "recursion":
        return {"visual_type": "special_recursion", "payload": {}}

    m_def = re.match(r"\s*def\s+(\w+)\s*\(([^)]*)\)", str(focus_text or ""))
    if m_def:
        return {"visual_type": "function_def", "payload": {"name": m_def.group(1), "params": [p.strip().split("=")[0].strip() for p in m_def.group(2).split(",") if p.strip()]}}

    if _looks_like_return_scene(focus_text, narration, output_text):
        return {
            "visual_type": "function_return",
            "payload": {"value": _extract_print_text(curr_vars, output_text, narration, focus_text, full_code)}
        }

    if _looks_like_input_scene(focus_text, narration, output_text, curr_vars):
        return {
            "visual_type": "waiting_input",
            "payload": {
                "label": _extract_waiting_label(curr_vars, focus_text, narration),
                "menu_lines": _extract_menu_lines_from_code(full_code)
            }
        }

    if _looks_like_print_scene(focus_text, narration, output_text):
        return {
            "visual_type": "print_output",
            "payload": {"text": _extract_print_text(curr_vars, output_text, narration, focus_text, full_code)}
        }

    if "if" in str(focus_text).lower() or "branch" in str(scene_type).lower():
        _, branch_val = _extract_condition_info(curr_vars)
        outcome = "true" if branch_val is True else "false" if branch_val is False else "unknown"
        return {
            "visual_type": "conditional_branch",
            "payload": {"outcome": outcome}
        }

    generic_payload = _build_generic_payload(full_code, prev_vars, curr_vars, output_text, narration, focus_text)
    inferred = _infer_visual_type_from_payload(generic_payload)

    if inferred:
        return {"visual_type": inferred, "payload": generic_payload}

    return {"visual_type": "generic_data", "payload": generic_payload}


def _visual_fits(visual_type, full_code, variables):
    """Stop the AI from picking an algorithm visual (e.g. recursion tree) for code that isn't that algorithm."""
    code = str(full_code or "")
    low = code.lower()
    keys = {str(k).lower() for k in (variables or {}).keys()}
    if visual_type == "special_recursion":
        return _has_recursion(code)
    if visual_type == "special_binary_search":
        return "binary" in low or {"low", "high", "mid"} <= keys or {"left", "right", "mid"} <= keys
    if visual_type == "special_sorting":
        return any(w in low for w in ("sort", "swap", "bubble", "insertion", "selection")) or bool(re.search(r"(\w+)\[[^\]]+\]\s*,\s*\1\[[^\]]+\]\s*=", code))
    if visual_type == "special_stack":
        return "stack" in low or (".pop(" in low and ".append(" in low)
    if visual_type == "special_linked_list":
        return ".next" in low or "class node" in low
    return True


def _build_function_def_overlay(payload, output_path):
    panel, draw = _panel_base("FUNCTION DEFINED", "Saved now, runs only when called")
    name = str(payload.get("name", "function"))[:20]
    params = [p for p in payload.get("params", []) if p][:4]
    x = 24
    _rounded_box(draw, (x, 64, x + 170, 108), fill=(42, 94, 120, 250), outline=(167, 139, 250), width=3, radius=12)
    draw.text((x + 14, 74), f"{name}()", font=FONT_BADGE, fill=(255, 255, 255))
    draw.line((x + 172, 86, x + 214, 86), fill=(167, 139, 250), width=3)
    draw.polygon([(x + 214, 79), (x + 214, 93), (x + 226, 86)], fill=(167, 139, 250))
    px = x + 240
    if not params:
        draw.text((px, 74), "takes no inputs", font=FONT_SM, fill=(218, 240, 247))
    for p in params:
        w = max(70, int(draw.textlength(p, font=FONT_BADGE)) + 36)
        _rounded_box(draw, (px, 64, px + w, 108), fill=(24, 78, 99, 240), outline=(94, 224, 213), width=2, radius=12)
        draw.text((px + 18, 74), p[:12], font=FONT_BADGE, fill=(255, 255, 255))
        px += w + 12
    draw.text((24, 118), "parameters are the inputs it will receive", font=FONT_SM, fill=(150, 226, 255))
    panel.save(output_path)


def _build_visual_overlay(visual_type, payload, full_code, curr_vars, prev_vars, output_text, narration, output_path):
    if visual_type == "print_output":
        payload = dict(payload or {})
        if str(payload.get("text", "")).strip().lower() in _GENERIC_TAKEAWAYS:
            generic = str(output_text or "").strip().lower() in _GENERIC_TAKEAWAYS
            payload["text"] = _extract_print_text(curr_vars, "" if generic else output_text, narration, "", full_code)
        _build_print_output_overlay(payload, output_path)
    elif visual_type == "waiting_input":
        _build_waiting_input_overlay(payload, output_path)
    elif visual_type == "scalar_update":
        _build_scalar_update_overlay(payload, output_path)
    elif visual_type in {"list_append", "list_remove", "list_iteration"}:
        _build_list_change_overlay(payload, output_path)
    elif visual_type == "dict_update":
        _build_dict_update_overlay(payload, output_path)
    elif visual_type == "loop_progress":
        _build_loop_progress_overlay(payload, output_path)
    elif visual_type == "function_return":
        _build_function_return_overlay(payload, output_path)
    elif visual_type == "function_call":
        _build_function_call_overlay(payload, output_path)
    elif visual_type == "conditional_branch":
        _build_conditional_branch_overlay(payload, output_path)
    elif visual_type == "function_def":
        _build_function_def_overlay(payload, output_path)
    elif visual_type == "special_binary_search":
        _build_binary_search_overlay(curr_vars, prev_vars, full_code, output_text, narration, output_path)
    elif visual_type == "special_sorting":
        _build_sorting_overlay(curr_vars, prev_vars, full_code, output_path)
    elif visual_type == "special_stack":
        _build_stack_overlay(curr_vars, prev_vars, output_path)
    elif visual_type == "special_linked_list":
        _build_linked_list_overlay(curr_vars, prev_vars, output_text, output_path)
    elif visual_type == "special_recursion":
        _build_recursion_overlay(curr_vars, prev_vars, output_text, output_path)
    else:
        _build_generic_data_overlay(payload, output_path)


# -------------------------------
# Step parsing
# -------------------------------

def _safe_step_data(step, idx):
    scene_type = str(step.get("scene_type", "explanation")).strip() or "explanation"
    scene_title = str(step.get("scene_title", f"Step {idx + 1}")).strip() or f"Step {idx + 1}"
    focus_text = str(step.get("focus_text", "Understanding the current line")).strip() or "Understanding the current line"
    output_text = str(step.get("output_text", "The program continues logically.")).strip() or "The program continues logically."
    narration = str(step.get("narration", "This step explains the current logic.")).strip() or "This step explains the current logic."

    try:
        line_index = int(step.get("line_index", 0))
    except Exception:
        line_index = 0

    variables = step.get("vars", {})
    if not isinstance(variables, dict):
        variables = {}

    normalized_variables = {}
    for k, v in variables.items():
        normalized_variables[str(k)] = _normalize_value(v)

    visual_type = str(step.get("visual_type", "")).strip()
    payload = step.get("visual_payload", {})
    if not isinstance(payload, dict):
        payload = {}

    return scene_type, scene_title, focus_text, output_text, narration, line_index, normalized_variables, visual_type, payload


# -------------------------------
# Voice and recording
# -------------------------------

async def _generate_voice(text, path, voice=DEFAULT_VOICE, speed="normal"):
    rate_value = SUPPORTED_VOICE_SPEEDS.get(_normalize_voice_speed(speed), "-5%")

    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=rate_value,
        pitch="+2Hz",
        volume="+10%"
    )
    await communicate.save(path)


def _save_video_record(user_email, tid, code, title="", language="english", explain_mode="beginner"):
    clean_title = str(title or "").strip()
    if not clean_title:
        clean_title = f"{tid}.mp4"

    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO videos(user_email, task_id, title, filename, original_code, language, explain_mode)
        VALUES(?,?,?,?,?,?,?)
    """, (
        user_email,
        tid,
        clean_title,
        f"{tid}.mp4",
        code,
        language,
        explain_mode
    ))
    conn.commit()
    conn.close()


# -------------------------------
# Clip creation
# -------------------------------

def _make_step_clip(
    bg_path,
    code_path,
    vars_path,
    subtitle_path,
    concept_path,
    audio_path,
    duration,
    scene_title,
    visual_type,
    output_text,
    narration
):
    safe_duration = max(0.7, duration - 0.04)

    bg_clip = ImageClip(bg_path).with_duration(safe_duration)
    bg_clip = bg_clip.with_effects([vfx.Resize(lambda t: 1 + 0.006 * t)])

    code_clip = ImageClip(code_path).with_duration(safe_duration)
    code_clip = code_clip.with_position(POS_CODE)

    vars_clip = ImageClip(vars_path).with_duration(safe_duration)
    vars_clip = vars_clip.with_position(POS_VARS)

    layout = _choose_layout(scene_title, visual_type, output_text, narration)

    concept_clip = ImageClip(concept_path).with_duration(safe_duration)
    subtitle_clip = ImageClip(subtitle_path).with_duration(safe_duration)

    concept_clip = concept_clip.with_position(POS_CONCEPT)

    subtitle_clip = subtitle_clip.with_position(POS_SUB)

    audio_clip = AudioFileClip(audio_path).subclipped(0, safe_duration)

    final = CompositeVideoClip(
        [bg_clip, code_clip, vars_clip, concept_clip, subtitle_clip],
        size=(VIDEO_W, VIDEO_H)
    ).with_duration(safe_duration).with_audio(audio_clip)

    return final


# ===============================
# VIDEO WORKER (UPDATED WITH GROQ)
# ===============================

LOW_MEMORY_RENDER = os.getenv("LOW_MEMORY_RENDER", "false").lower() == "true"


def _video_write_kwargs():
    return dict(
        fps=int(os.getenv("VIDEO_FPS", "30")),
        codec="libx264",
        bitrate=os.getenv("VIDEO_BITRATE", "12000k"),
        audio_codec="aac",
        preset=os.getenv("VIDEO_PRESET", "slow"),
        threads=int(os.getenv("VIDEO_THREADS", "4")),
        logger=None
    )


FAST_RENDER = os.getenv("FAST_RENDER", "false").lower() == "true"


def _render_step_fast(bg_path, code_path, vars_path, subtitle_path, concept_path, audio_path,
                      duration, scene_title, visual_type, output_text, narration, out_path):
    import subprocess
    import imageio_ffmpeg

    safe_duration = max(0.7, duration - 0.04)

    canvas = Image.open(bg_path).convert("RGBA")
    if canvas.size != (VIDEO_W, VIDEO_H):
        canvas = canvas.resize((VIDEO_W, VIDEO_H))

    def _layer(path, pos, scale=1.0):
        img = Image.open(path).convert("RGBA")
        if scale != 1.0:
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        canvas.alpha_composite(img, dest=(int(pos[0]), int(pos[1])))

    layout = _choose_layout(scene_title, visual_type, output_text, narration)
    _layer(code_path, POS_CODE)
    _layer(vars_path, POS_VARS)
    _layer(concept_path, POS_CONCEPT)
    _layer(subtitle_path, POS_SUB)

    frame_path = out_path.replace(".mp4", ".png")
    canvas.convert("RGB").crop((0, 0, VIDEO_W, VIDEO_H)).save(frame_path)

    fps = os.getenv("VIDEO_FPS", "30")
    try:
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
             "-loop", "1", "-framerate", fps, "-i", frame_path,
             "-i", audio_path,
             "-t", f"{safe_duration:.3f}",
             "-c:v", "libx264", "-tune", "stillimage",
             "-preset", os.getenv("VIDEO_PRESET", "slow"),
             "-pix_fmt", "yuv420p", "-r", fps,
             "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
             "-threads", os.getenv("VIDEO_THREADS", "4"),
             out_path],
            check=True
        )
    finally:
        try:
            os.remove(frame_path)
        except Exception:
            pass


def _concat_segments(segment_paths, out_path, tid):
    """Join per-step mp4s with ffmpeg's concat demuxer (stream copy, no re-encode, tiny RAM)."""
    import subprocess
    import imageio_ffmpeg
    list_path = os.path.join(TEMP_DIR, f"{tid}_segments.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for seg in segment_paths:
            f.write(f"file '{os.path.abspath(seg)}'\n")
    try:
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", "-movflags", "+faststart", out_path],
            check=True
        )
    finally:
        try:
            os.remove(list_path)
        except Exception:
            pass


def _worker(tid, code):
    temp_files = []
    previous_vars = {}

    try:
        tasks[tid]["status"] = "Analyzing code"
        tasks[tid]["progress"] = 10

        language = _normalize_language(tasks[tid].get("language", "english"))
        explain_mode = _normalize_explain_mode(tasks[tid].get("explain_mode", "beginner"))
        voice_speed = _normalize_voice_speed(tasks[tid].get("voice_speed", "normal"))
        voice = _get_voice_for_language(language)

        # ✅ USING GROQ CLIENT (REPLACED GEMINI)
        explanation_style = (
            "Narration must be beginner-friendly, very simple, and easy to understand."
            if explain_mode == "beginner"
            else "Narration can be more technical and slightly more advanced."
        )

        prompt = f"""
You are generating an educational animation plan for Python learners.

Analyze this Python code and return ONLY a JSON array with 5 to 7 steps.

Each step must have exactly these keys:
- scene_type
- scene_title
- line_index  (0-based index of the code line this step explains; focus_text must quote that exact line)
- vars
- narration
- focus_text
- output_text
- visual_type
- visual_payload

Allowed visual_type values:
- auto
- scalar_update
- list_append
- list_remove
- list_iteration
- dict_update
- object_field_update
- conditional_branch
- loop_progress
- function_call
- function_return
- search_window
- swap_operation
- linked_structure
- print_output
- waiting_input
- generic_data

Rules:
- narration must be short and teacher-like
- narration should be one short sentence when possible
- explain only the main idea of the step
- vars should contain only important variables for that step
- choose visual_type only if it clearly matches the step
- scene_title, focus_text, and output_text must remain in English
- narration must be written in English only
- {explanation_style}
- output only JSON
- no markdown
- no backticks

Python code:
{code}
"""

        # ✅ GROQ API CALL
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )

        text_result = completion.choices[0].message.content or ""

        match = re.search(r"\[.*\]", text_result, re.DOTALL)
        if not match:
            raise Exception("Could not extract JSON steps from Groq response.")

        steps = json.loads(match.group(0))

        if not steps or not isinstance(steps, list):
            raise Exception("No execution steps generated.")

        clips = []
        total_steps = len(steps)
        tasks[tid]["steps_meta"] = []

        for i, step in enumerate(steps):
            tasks[tid]["status"] = f"Rendering animated step {i+1}"
            tasks[tid]["progress"] = int(((i + 1) / total_steps) * 88)

            (
                scene_type,
                scene_title,
                focus_text,
                output_text,
                narration,
                line_index,
                variables,
                ai_visual_type,
                ai_payload,
            ) = _safe_step_data(step, i)

            changed_keys = _diff_variables(previous_vars, variables)

            if ai_visual_type and ai_visual_type.lower() != "auto":
                _vt = {"search_window": "special_binary_search", "swap_operation": "special_sorting", "linked_structure": "special_linked_list"}.get(ai_visual_type, ai_visual_type)
                if _vt.startswith("special_") and not _visual_fits(_vt, code, variables):
                    ai_visual_type = "auto"
                elif re.match(r"\s*def\s", str(focus_text or "")) and _vt not in {"special_recursion"}:
                    ai_visual_type = "auto"

            if not ai_visual_type or ai_visual_type.lower() == "auto":
                plan = _fallback_visual_plan(
                    code,
                    scene_type,
                    focus_text,
                    output_text,
                    narration,
                    previous_vars,
                    variables
                )
                visual_type = plan["visual_type"]
                payload = plan["payload"]
            else:
                visual_type = ai_visual_type
                payload = ai_payload if ai_payload else {}

                if visual_type == "search_window":
                    visual_type = "special_binary_search"
                elif visual_type == "swap_operation":
                    visual_type = "special_sorting"
                elif visual_type == "linked_structure":
                    visual_type = "special_linked_list"

            bg_p = os.path.join(TEMP_DIR, f"bg_{tid}_{i}.png")
            code_p = os.path.join(TEMP_DIR, f"code_{tid}_{i}.png")
            vars_p = os.path.join(TEMP_DIR, f"vars_{tid}_{i}.png")
            sub_p = os.path.join(TEMP_DIR, f"sub_{tid}_{i}.png")
            concept_p = os.path.join(TEMP_DIR, f"concept_{tid}_{i}.png")
            aud_p = os.path.join(TEMP_DIR, f"aud_{tid}_{i}.mp3")

            temp_files.extend([bg_p, code_p, vars_p, sub_p, concept_p, aud_p])

            subtitle_source = _simplify_subtitle(narration)
            subtitle_text = _translate_text(subtitle_source, language)
            voice_text = subtitle_text

            tasks[tid]["steps_meta"].append({
                "step_no": i + 1,
                "scene_title": scene_title,
                "focus_text": focus_text,
                "output_text": output_text,
                "narration": subtitle_text
            })

            _build_background(scene_title, bg_p)
            _build_code_overlay(code, _resolve_active_line(code, line_index, focus_text), focus_text, code_p)
            _build_vars_overlay(variables, scene_type, output_text, changed_keys, vars_p, focus_text=focus_text)
            if visual_type == "function_call" and not (payload or {}).get("name"):
                m_call = re.search(r"(\w+)\s*\(([^)]*)\)", str(focus_text or ""))
                if m_call and m_call.group(1) not in {"print", "input", "len", "range"}:
                    payload = dict(payload or {})
                    payload["name"] = f"{m_call.group(1)}({m_call.group(2)})"
            _build_visual_overlay(
                visual_type,
                payload,
                code,
                variables,
                previous_vars,
                output_text,
                narration,
                concept_p
            )
            _build_subtitle_overlay(subtitle_text, i + 1, total_steps, sub_p, language=language)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_generate_voice(voice_text, aud_p, voice=voice, speed=voice_speed))
            loop.close()

            audio_for_duration = AudioFileClip(aud_p)
            duration = max(0.9, audio_for_duration.duration)
            audio_for_duration.close()

            if FAST_RENDER:
                # Every layer of a step is a still image, so flatten them into ONE
                # frame with PIL and let ffmpeg encode "still image + audio" directly.
                # Far cheaper than MoviePy compositing 5 layers on every frame.
                seg_path = os.path.join(TEMP_DIR, f"{tid}_seg_{i}.mp4")
                _render_step_fast(bg_p, code_p, vars_p, sub_p, concept_p, aud_p, duration,
                                  scene_title, visual_type, output_text, narration, seg_path)
                temp_files.append(seg_path)
                clips.append(seg_path)
                previous_vars = dict(variables)
                continue

            clip = _make_step_clip(
                bg_p,
                code_p,
                vars_p,
                sub_p,
                concept_p,
                aud_p,
                duration,
                scene_title,
                visual_type,
                output_text,
                narration
            )
            if LOW_MEMORY_RENDER:
                # Encode this step to its own small mp4 right away and free it,
                # so only ONE step is ever held in RAM (Render free tier = 512MB)
                seg_path = os.path.join(TEMP_DIR, f"{tid}_seg_{i}.mp4")
                clip.write_videofile(seg_path, **_video_write_kwargs())
                try:
                    clip.close()
                except Exception:
                    pass
                temp_files.append(seg_path)
                clips.append(seg_path)
            else:
                clips.append(clip)

            previous_vars = dict(variables)

        tasks[tid]["status"] = "Compositing final educational video"
        tasks[tid]["progress"] = 95

        out = os.path.join(VIDEO_DIR, f"{tid}.mp4")

        if FAST_RENDER or LOW_MEMORY_RENDER:
            _concat_segments(clips, out, tid)
        else:
            final = concatenate_videoclips(clips, method="compose")
            final = final.with_duration(sum(c.duration for c in clips))
            final.write_videofile(out, **_video_write_kwargs())

            try:
                final.close()
            except Exception:
                pass

            for clip in clips:
                try:
                    clip.close()
                except Exception:
                    pass

        user_email = tasks[tid].get("user_email")
        if user_email:
           _save_video_record(
               user_email=user_email,
               tid=tid,
               code=code,
               title=tasks[tid].get("title", ""),
               language=tasks[tid].get("language", "english"),
               explain_mode=tasks[tid].get("explain_mode", "beginner")
            )

        _save_subtitle_file(tid, tasks[tid].get("steps_meta", []))
        _save_video_meta(tid)

        tasks[tid].update({
            "status": "completed",
            "progress": 100,
            "video_url": f"/download/{tid}"
        })

    except Exception as e:
        tasks[tid]["status"] = "error"
        tasks[tid]["message"] = str(e)
        print("Worker error:", e)

    finally:
        for file_path in temp_files:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass


# ===============================
# VALIDATION
# ===============================

def _clean_code(code):
    """Fix invisible characters/indentation that sneak in when code is copy-pasted
    (chat apps, PDFs, websites): non-breaking spaces, zero-width chars, tabs, CRLF,
    and a common leading indent on every line."""
    import textwrap
    code = str(code or "")
    for bad, good in {"\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u200b": "",
                      "\ufeff": "", "\r\n": "\n", "\r": "\n", "\t": "    "}.items():
        code = code.replace(bad, good)
    code = "\n".join(line.rstrip() for line in code.split("\n"))
    return textwrap.dedent(code).strip("\n")


def _auto_title(code):
    """Readable default title when the user leaves it blank (instead of a UUID filename)."""
    m = re.search(r"^\s*def\s+([A-Za-z_]\w*)", code or "", re.M)
    if m:
        return m.group(1).replace("_", " ").strip().capitalize() + " function"
    m = re.search(r"^\s*class\s+([A-Za-z_]\w*)", code or "", re.M)
    if m:
        return m.group(1) + " class"
    return "Python snippet"


def validate_python_logic(code):
    try:
        non_python_patterns = [
            r"#include",
            r"std::cout",
            r"public static void",
            r"printf\(",
            r"console\.log"
        ]

        for pattern in non_python_patterns:
            if re.search(pattern, code):
                return False, "Non-Python logic detected."

        ast.parse(code)
        return True, "Valid"

    except SyntaxError as e:
        line = f" (line {e.lineno})" if getattr(e, "lineno", None) else ""
        return False, f"Syntax Error{line}: {e.msg}"
    

def _video_meta_path(tid):
    return os.path.join(VIDEO_DIR, f"{tid}_details.json")


def _save_video_meta(tid):
    try:
        t = tasks.get(tid, {})
        with open(_video_meta_path(tid), "w", encoding="utf-8") as f:
            json.dump({"steps_meta": t.get("steps_meta", []), "complexity": t.get("complexity", {})}, f, ensure_ascii=False)
    except Exception as e:
        print("Could not save video meta:", e)


def _load_video_meta(tid):
    t = tasks.get(tid)
    if t and t.get("steps_meta"):
        return {"steps_meta": t.get("steps_meta", []), "complexity": t.get("complexity", {})}
    try:
        with open(_video_meta_path(tid), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"steps_meta": [], "complexity": {}}


def _save_subtitle_file(tid, steps_meta):
        subtitle_path = os.path.join(SUBTITLE_DIR, f"{tid}.srt")

        lines = []
        current_time = 0

        for i, step in enumerate(steps_meta, start=1):
            text = str(step.get("narration", "")).strip()
            if not text:
                continue

            start_sec = current_time
            end_sec = current_time + 3
            current_time = end_sec

            def fmt(sec):
                hrs = sec // 3600
                mins = (sec % 3600) // 60
                secs = sec % 60
                return f"{hrs:02}:{mins:02}:{secs:02},000"

            lines.append(str(i))
            lines.append(f"{fmt(start_sec)} --> {fmt(end_sec)}")
            lines.append(text)
            lines.append("")

        with open(subtitle_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return subtitle_path


# ===============================
# VIDEO GENERATION ROUTES
# ===============================

@app.route("/generate", methods=["POST"])
def generate():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    code = _clean_code(data.get("code", ""))
    title = str(data.get("title", "")).strip()[:80] or _auto_title(_clean_code(data.get("code", "")))
    language = _normalize_language(data.get("language", "english"))
    explain_mode = _normalize_explain_mode(data.get("explain_mode", "beginner"))
    voice_speed = _normalize_voice_speed(data.get("voice_speed", "normal"))
    theme = _normalize_theme(data.get("theme", "ocean"))
    ok, msg = validate_python_logic(code)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400

    complexity = _detect_complexity(code)

    tid = str(uuid.uuid4())
    tasks[tid] = {
        "status": "Initialized",
        "progress": 0,
        "user_email": session["user"],
        "language": language,
        "explain_mode": explain_mode,
        "voice_speed": voice_speed,
        "theme": theme,
        "title": title,
        "complexity": complexity,
        "steps_meta": []
    }

    threading.Thread(target=_worker, args=(tid, code), daemon=True).start()
    return jsonify({"task_id": tid, "complexity": complexity})


@app.route("/status/<tid>")
def status(tid):
    task = tasks.get(tid, {})
    return jsonify(task)


@app.route("/download/<tid>")
def download(tid):
    file_path = os.path.join(VIDEO_DIR, f"{tid}.mp4")

    if not os.path.exists(file_path):
        return jsonify({"status": "error", "message": "Video file not found"}), 404

    return send_file(file_path, as_attachment=False)


@app.route("/download_zip/<tid>")
def download_zip(tid):
    file_path = os.path.join(VIDEO_DIR, f"{tid}.mp4")

    if not os.path.exists(file_path):
        return jsonify({"status": "error", "message": "Video file not found"}), 404

    zip_path = os.path.join(VIDEO_DIR, f"{tid}.zip")
    metadata_path = os.path.join(VIDEO_DIR, f"{tid}_metadata.json")

    try:
        with open(metadata_path, "w", encoding="utf-8") as meta_file:
            json.dump(tasks.get(tid, {}), meta_file, ensure_ascii=False, indent=2)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(file_path, arcname=f"{tid}.mp4")
            zipf.write(metadata_path, arcname="metadata.json")

        return send_file(zip_path, as_attachment=True)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ===============================
# FEEDBACK ROUTE
# ===============================

@app.route("/submit_feedback", methods=["POST"])
def submit_feedback():
    if "user" not in session:
        return jsonify({"status": "error", "message": "Login required"}), 401

    data = request.json or {}
    task_id = data.get("task_id")
    rating = data.get("rating")
    comment = data.get("comment", "").strip()
    user_email = session.get("user")

    if not task_id:
        return jsonify({"status": "error", "message": "Task ID is required"}), 400

    if not rating:
        return jsonify({"status": "error", "message": "Rating is required"}), 400

    try:
        rating = int(rating)
        if rating < 1 or rating > 5:
            return jsonify({"status": "error", "message": "Rating must be between 1 and 5"}), 400
    except Exception:
        return jsonify({"status": "error", "message": "Invalid rating"}), 400

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO feedback(user_email, task_id, rating, comment) VALUES(?,?,?,?)",
        (user_email, task_id, rating, comment)
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "success", "message": "Feedback submitted successfully"})


# ===============================
# FEEDBACK VIEW
# ===============================

@app.route("/all_feedback")
def all_feedback():
    # Only the signed-in user's own feedback (previously exposed everyone's emails)
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT user_email, task_id, rating, comment, created_at
        FROM feedback
        WHERE user_email = ?
        ORDER BY id DESC
    """, (session["user"],))
    rows = c.fetchall()
    conn.close()

    return jsonify([
        {
            "user_email": row["user_email"],
            "task_id": row["task_id"],
            "rating": row["rating"],
            "comment": row["comment"],
            "created_at": row["created_at"]
        }
        for row in rows
    ])


# ===============================
# EXTRA API ROUTES (UPDATED WITH GROQ)
# ===============================

@app.route("/api/languages")
def api_languages():
    return jsonify({
        "default": "english",
        "languages": [
            {"key": key, "label": value["label"]}
            for key, value in SUPPORTED_LANGUAGES.items()
        ]
    })


@app.route("/api/options")
def api_options():
    return jsonify({
        "languages": [{"key": k, "label": v["label"]} for k, v in SUPPORTED_LANGUAGES.items()],
        "explain_modes": [{"key": k, "label": v} for k, v in SUPPORTED_EXPLAIN_MODES.items()],
        "voice_speeds": [{"key": k, "label": k.title()} for k in SUPPORTED_VOICE_SPEEDS.keys()],
        "themes": [{"key": k, "label": v} for k, v in SUPPORTED_THEMES.items()],
        "defaults": {
            "language": "english",
            "explain_mode": "beginner",
            "voice_speed": "normal",
            "theme": "ocean"
        }
    })


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(app.root_path, "static"), "favicon.ico")


@app.route("/api/analyze_code", methods=["POST"])
def api_analyze_code():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    code = _clean_code(data.get("code", ""))

    if not code:
        return jsonify({"error": "Code is required"}), 400

    return jsonify(_detect_complexity(code))


@app.route("/api/concept_summary", methods=["POST"])
def api_concept_summary():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    code = _clean_code(data.get("code", ""))

    if not code:
        return jsonify({"error": "Code required"}), 400

    try:
        prompt = f"""
Analyze this Python code and return ONLY valid JSON (the summary should be 2-3 plain sentences; time/space complexity will be verified separately):

{{
  "algorithm": "...",
  "time_complexity": "...",
  "space_complexity": "...",
  "summary": "short summary"
}}

Code:
{code}
"""

        # ✅ GROQ API CALL
        
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )

        text_result = completion.choices[0].message.content or ""

        match = re.search(r"\{.*\}", text_result, re.DOTALL)
        if not match:
            return jsonify({"error": "Parsing failed"}), 500

        out = json.loads(match.group(0))
        # The AST analysis is the source of truth for the numbers; the LLM only writes the summary.
        det = _detect_complexity(code)
        if det.get("confidence") != "low":
            out["algorithm"] = det["algorithm"]
            out["time_complexity"] = det["time_complexity"]
            out["space_complexity"] = det["space_complexity"]
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/explain_line", methods=["POST"])
def api_explain_line():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    code = _clean_code(data.get("code", ""))
    line = data.get("line", "").strip()
    language = _normalize_language(data.get("language", "english"))

    if not code or not line:
        return jsonify({"error": "Code and line are required"}), 400

    try:
        prompt = f"""
Explain this Python line in simple words.

Full code:
{code}

Selected line:
{line}
"""

        # ✅ GROQ API CALL
        
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )

        explanation = str(completion.choices[0].message.content or "").strip()
        if language != "english":
            explanation = _translate_text(explanation, language)

        return jsonify({"explanation": explanation})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ask_doubt", methods=["POST"])
def api_ask_doubt():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    code = _clean_code(data.get("code", ""))
    question = str(data.get("question", "")).strip()
    task_id = str(data.get("task_id", "")).strip()
    explain_mode = _normalize_explain_mode(data.get("explain_mode", "beginner"))
    language = _normalize_language(data.get("language", "english"))

    if not code or not question:
        return jsonify({"error": "Code and question are required"}), 400

    try:
        mode_instruction = (
            "Answer for a beginner in simple words."
            if explain_mode == "beginner"
            else "Answer with a more technical explanation."
        )

        prompt = f"""
You are an AI coding tutor.

{mode_instruction}
Answer the user's doubt about this Python code.
Keep the answer concise and accurate.
Answer in English only.

Python code:
{code}

User question:
{question}
"""

        # ✅ GROQ API CALL
       
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}]
        )

        answer = str(completion.choices[0].message.content or "").strip()
        if not answer:
            return jsonify({"error": "Failed to get answer"}), 500

        if language != "english":
            answer = _translate_text(answer, language)

        if task_id:
            conn = get_conn()
            c = conn.cursor()
            c.execute("""
                INSERT INTO doubts(user_email, task_id, question, answer)
                VALUES (?, ?, ?, ?)
            """, (session["user"], task_id, question, answer))
            conn.commit()
            conn.close()

        return jsonify({"answer": answer})
    except Exception as e:
        print("Ask doubt error:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/favorite", methods=["POST"])
def api_favorite():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    task_id = data.get("task_id")

    if not task_id:
        return jsonify({"error": "Task ID required"}), 400

    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT OR IGNORE INTO favorites(user_email, task_id) VALUES(?, ?)",
            (session["user"], task_id)
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "saved"})
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500


@app.route("/api/unfavorite", methods=["POST"])
def api_unfavorite():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    task_id = data.get("task_id")

    if not task_id:
        return jsonify({"error": "Task ID required"}), 400

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "DELETE FROM favorites WHERE user_email=? AND task_id=?",
        (session["user"], task_id)
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "removed"})


@app.route("/api/video/<tid>/favorite", methods=["POST"])
def api_video_favorite_toggle(tid):
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    user_email = session["user"]
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT id FROM favorites
        WHERE user_email=? AND task_id=?
    """, (user_email, tid))
    existing = c.fetchone()

    if existing:
        c.execute("""
            DELETE FROM favorites
            WHERE user_email=? AND task_id=?
        """, (user_email, tid))
        conn.commit()
        conn.close()
        return jsonify({"status": "removed", "is_favorite": False})

    c.execute("""
        INSERT OR IGNORE INTO favorites(user_email, task_id)
        VALUES (?, ?)
    """, (user_email, tid))
    conn.commit()
    conn.close()

    return jsonify({"status": "saved", "is_favorite": True})


@app.route("/api/leaderboard")
def api_leaderboard():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT user_email, COUNT(*) AS videos
        FROM videos
        GROUP BY user_email
        ORDER BY videos DESC
        LIMIT 5
    """)

    rows = c.fetchall()
    conn.close()

    def _mask(email):
        name, _, domain = str(email).partition("@")
        return (name[:2] + "***@" + domain) if domain else "***"
    return jsonify([
        {"user": _mask(row["user_email"]), "videos": row["videos"]}
        for row in rows
    ])


@app.route("/api/voice_preview", methods=["POST"])
def api_voice_preview():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    language = _normalize_language(data.get("language", "english"))
    voice_speed = _normalize_voice_speed(data.get("voice_speed", "normal"))

    voice = _get_voice_for_language(language)
    sample_text = {
        "english": "Hello, this is your Code2Video voice preview.",
        "hindi": "नमस्ते, यह आपका कोड टू वीडियो वॉइस प्रीव्यू है।",
        "marathi": "नमस्कार, हा तुमचा कोड टू व्हिडिओ व्हॉइस प्रीव्यू आहे.",
        "telugu": "నమస్కారం, ఇది మీ కోడ్ టు వీడియో వాయిస్ ప్రివ్యూ."
    }.get(language, "Hello, this is your Code2Video voice preview.")

    preview_path = os.path.join(TEMP_DIR, f"voice_preview_{uuid.uuid4().hex}.mp3")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            _generate_voice(sample_text, preview_path, voice=voice, speed=voice_speed)
        )
        loop.close()

        return send_file(preview_path, mimetype="audio/mpeg", as_attachment=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route("/api/video/<tid>/subtitles")
def api_video_subtitles(tid):
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    subtitle_path = os.path.join(SUBTITLE_DIR, f"{tid}.srt")

    if not os.path.exists(subtitle_path):
        return jsonify({"error": "Subtitle file not found"}), 404

    return send_file(
        subtitle_path,
        mimetype="application/x-subrip",
        as_attachment=True,
        download_name=f"{tid}.srt"
    )


@app.route("/api/video/<tid>/doubts")
def api_video_doubts(tid):
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT question, answer, created_at
        FROM doubts
        WHERE user_email=? AND task_id=?
        ORDER BY id DESC
    """, (session["user"], tid))
    rows = c.fetchall()
    conn.close()

    return jsonify({
        "items": [
            {
                "question": row["question"],
                "answer": row["answer"],
                "created_at": row["created_at"]
            }
            for row in rows
        ]
    })

# ===============================
# RUN
# ===============================

def open_browser():
    time.sleep(2)  # wait for server to start
    webbrowser.open("http://127.0.0.1:5000")

# Only open browser if not in debug mode or on first run
if __name__ == "__main__":
    # Don't auto-open browser - let the splash screen handle it
    # threading.Thread(target=open_browser).start()
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "true").lower() == "true"
    )