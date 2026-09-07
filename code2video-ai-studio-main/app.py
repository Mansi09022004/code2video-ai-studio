import os
import threading
import uuid
import json
import ast
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
app.secret_key = os.getenv("FLASK_SECRET_KEY", "supersecretkey123")

app.config.update(
    SESSION_COOKIE_NAME="google-login-session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False
)

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# ===============================
# GROQ CLIENT INITIALIZATION (REPLACED GEMINI)
# ===============================

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
client = Groq(api_key=GROQ_API_KEY)

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

    return jsonify({"status": "error", "message": "Invalid login"}), 401


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
        return f"Google Login Error: {str(e)}"


# ===============================
# PAGE ROUTES
# ===============================

@app.route("/")
def splash():
    """Splash screen - entry point of the application"""
    return render_template("splash.html")


@app.route("/login")
def login():
    """Login page - shown after splash screen"""
    return render_template("login.html")


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
            "explain_mode": explain_mode
        })

    return jsonify(results[:6])


@app.route("/api/public_recent_videos")
def api_public_recent_videos():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT filename, created_at
        FROM videos
        ORDER BY id DESC
        LIMIT 5
    """)
    rows = c.fetchall()
    conn.close()

    return jsonify([
        {
            "filename": row["filename"],
            "created_at": row["created_at"]
        }
        for row in rows
    ])


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
    task_data = tasks.get(task_id, {})

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

def _load_font(size=24, bold=False, language="english"):
    candidates = []

    if language in {"hindi", "marathi", "telugu"}:
        if bold:
            candidates = [
                "C:/Windows/Fonts/NirmalaB.ttf",
                "C:/Windows/Fonts/Nirmala.ttf",
                "C:/Windows/Fonts/mangal.ttf",
                "C:/Windows/Fonts/gautami.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansTelugu-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ]
        else:
            candidates = [
                "C:/Windows/Fonts/Nirmala.ttf",
                "C:/Windows/Fonts/mangal.ttf",
                "C:/Windows/Fonts/gautami.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansTelugu-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
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


def _simplify_subtitle(text):
    return _truncate_words(_first_sentence(text), 10)


def _is_emptyish(val):
    if val is None:
        return True
    if isinstance(val, str):
        stripped = val.strip().lower()
        return stripped in {"", "none", "null", "waiting", "n/a"}
    if isinstance(val, (list, tuple, set, dict)):
        return len(val) == 0
    return False


def _short_focus_label(text):
    text = str(text or "").strip()
    if not text:
        return "Current step"

    replacements = [
        ("print(", "Print output"),
        ("return ", "Return value"),
        ("append(", "Append item"),
        ("pop(", "Remove item"),
        ("insert(", "Insert item"),
        ("update(", "Update data"),
        ("input(", "Read input"),
        ("while ", "Loop step"),
        ("for ", "Loop step"),
        ("if ", "Condition check"),
        ("elif ", "Condition check"),
        ("else", "Else branch"),
    ]

    lowered = text.lower()
    for src, dst in replacements:
        if src.lower() in lowered:
            return dst

    words = text.replace("\n", " ").split()
    if len(words) <= 4:
        return text
    return " ".join(words[:4])


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

    fn_names = re.findall(r"def\s+([a-zA-Z_]\w*)\s*\(", code_lower)
    for fn in fn_names:
        start = code_lower.find(f"def {fn}")
        if start != -1:
            body = code_lower[start:].replace(f"def {fn}", "", 1)
            if re.search(rf"\b{re.escape(fn)}\s*\(", body):
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
    """Enhanced code analysis with more granular complexity detection"""
    code_lower = str(code or "").lower()
    code_str = str(code or "")
    
    # ========== DETECT MULTIPLE ALGORITHMS IN SAME CODE ==========
    algorithms_found = []
    
    # 1. Search Algorithms
    if "binary_search" in code_lower or ("low" in code_lower and "high" in code_lower and "mid" in code_lower):
        if "recursive" in code_lower or "return binary_search" in code_lower:
            algorithms_found.append({
                "algorithm": "Binary Search (Recursive)",
                "category": "Divide and Conquer / Search",
                "time_complexity": "O(log n)",
                "space_complexity": "O(log n) - recursion stack",
                "key_operations": "Divide array by half each iteration",
                "best_case": "O(1) - target at middle",
                "worst_case": "O(log n) - target at ends"
            })
        else:
            algorithms_found.append({
                "algorithm": "Binary Search (Iterative)",
                "category": "Divide and Conquer / Search",
                "time_complexity": "O(log n)",
                "space_complexity": "O(1)",
                "key_operations": "low, high, mid pointers",
                "best_case": "O(1) - target at middle",
                "worst_case": "O(log n) - target at ends"
            })
    
    if "linear_search" in code_lower or ("for" in code_lower and "if" in code_lower and "return i" in code_lower):
        algorithms_found.append({
            "algorithm": "Linear Search",
            "category": "Search Algorithm",
            "time_complexity": "O(n)",
            "space_complexity": "O(1)",
            "key_operations": "Sequential scanning",
            "best_case": "O(1) - first element",
            "worst_case": "O(n) - last element or not found"
        })
    
    # 2. Sorting Algorithms - MORE GRANULAR
    if "quicksort" in code_lower or "quick_sort" in code_lower:
        # Check for different implementations
        if "partition" in code_lower:
            algorithms_found.append({
                "algorithm": "Quick Sort (Hoare Partition)",
                "category": "Divide and Conquer / Sorting",
                "time_complexity": "Average: O(n log n), Worst: O(n²)",
                "space_complexity": "O(log n)",
                "key_operations": "Pivot selection, partition, recursion",
                "best_case": "O(n log n) - balanced partitions",
                "worst_case": "O(n²) - already sorted array"
            })
        else:
            algorithms_found.append({
                "algorithm": "Quick Sort (Lomuto Partition)",
                "category": "Divide and Conquer / Sorting",
                "time_complexity": "Average: O(n log n), Worst: O(n²)",
                "space_complexity": "O(log n)",
                "key_operations": "Pivot selection, partition, recursion",
                "best_case": "O(n log n) - balanced partitions",
                "worst_case": "O(n²) - already sorted array"
            })
    
    if "mergesort" in code_lower or "merge_sort" in code_lower:
        algorithms_found.append({
            "algorithm": "Merge Sort",
            "category": "Divide and Conquer / Sorting",
            "time_complexity": "O(n log n)",
            "space_complexity": "O(n)",
            "key_operations": "Divide array, merge sorted halves",
            "best_case": "O(n log n) - always divide and conquer",
            "worst_case": "O(n log n) - always divide and conquer"
        })
    
    if "bubble" in code_lower:
        # Check for optimization
        if "swapped" in code_lower or "flag" in code_lower:
            algorithms_found.append({
                "algorithm": "Bubble Sort (Optimized)",
                "category": "Comparison Sort",
                "time_complexity": "O(n²)",
                "space_complexity": "O(1)",
                "key_operations": "Adjacent swaps with early exit",
                "best_case": "O(n) - already sorted",
                "worst_case": "O(n²) - reverse sorted"
            })
        else:
            algorithms_found.append({
                "algorithm": "Bubble Sort (Standard)",
                "category": "Comparison Sort",
                "time_complexity": "O(n²)",
                "space_complexity": "O(1)",
                "key_operations": "Adjacent swaps without optimization",
                "best_case": "O(n²) - always compares all pairs",
                "worst_case": "O(n²) - reverse sorted"
            })
    
    if "insertion" in code_lower:
        algorithms_found.append({
            "algorithm": "Insertion Sort",
            "category": "Comparison Sort",
            "time_complexity": "O(n²)",
            "space_complexity": "O(1)",
            "key_operations": "Shift elements, insert at correct position",
            "best_case": "O(n) - already sorted",
            "worst_case": "O(n²) - reverse sorted"
        })
    
    if "selection" in code_lower:
        algorithms_found.append({
            "algorithm": "Selection Sort",
            "category": "Comparison Sort",
            "time_complexity": "O(n²)",
            "space_complexity": "O(1)",
            "key_operations": "Find minimum, swap with current position",
            "best_case": "O(n²) - always scans all elements",
            "worst_case": "O(n²) - always scans all elements"
        })
    
    # 3. Data Structures - MORE SPECIFIC
    if "stack" in code_lower and ("push" in code_lower or "pop" in code_lower):
        if "is_empty" in code_lower:
            algorithms_found.append({
                "algorithm": "Stack (Array Implementation)",
                "category": "Data Structure",
                "time_complexity": "Push/Pop/Peek/IsEmpty: O(1)",
                "space_complexity": "O(n)",
                "key_operations": "LIFO - Last In First Out",
                "best_case": "O(1) - all operations constant time",
                "worst_case": "O(1) - all operations constant time"
            })
        else:
            algorithms_found.append({
                "algorithm": "Stack (Basic Operations)",
                "category": "Data Structure",
                "time_complexity": "Push/Pop: O(1)",
                "space_complexity": "O(n)",
                "key_operations": "LIFO - Last In First Out",
                "best_case": "O(1) - all operations constant time",
                "worst_case": "O(1) - all operations constant time"
            })
    
    if "queue" in code_lower and ("enqueue" in code_lower or "dequeue" in code_lower):
        if "collections" in code_lower and "deque" in code_lower:
            algorithms_found.append({
                "algorithm": "Queue (deque Implementation)",
                "category": "Data Structure",
                "time_complexity": "Enqueue/Dequeue: O(1)",
                "space_complexity": "O(n)",
                "key_operations": "FIFO - First In First Out",
                "best_case": "O(1) - all operations constant time",
                "worst_case": "O(1) - all operations constant time"
            })
        else:
            algorithms_found.append({
                "algorithm": "Queue (Array Implementation)",
                "category": "Data Structure",
                "time_complexity": "Enqueue: O(1), Dequeue: O(n)",
                "space_complexity": "O(n)",
                "key_operations": "FIFO - First In First Out",
                "best_case": "O(1) - enqueue operations",
                "worst_case": "O(n) - dequeue shifts elements"
            })
    
    if "linkedlist" in code_lower or "linked_list" in code_lower or "class node" in code_lower:
        if "reverse" in code_lower:
            algorithms_found.append({
                "algorithm": "Linked List Reversal (Iterative)",
                "category": "Linked List",
                "time_complexity": "O(n)",
                "space_complexity": "O(1)",
                "key_operations": "Reverse pointers iteratively",
                "best_case": "O(n) - traverses all nodes",
                "worst_case": "O(n) - traverses all nodes"
            })
        elif "has_cycle" in code_lower or "detect_cycle" in code_lower:
            algorithms_found.append({
                "algorithm": "Linked List Cycle Detection (Floyd's Cycle)",
                "category": "Linked List",
                "time_complexity": "O(n)",
                "space_complexity": "O(1)",
                "key_operations": "Slow-fast pointer technique",
                "best_case": "O(n) - traverses all nodes",
                "worst_case": "O(n) - traverses all nodes"
            })
        else:
            algorithms_found.append({
                "algorithm": "Linked List Operations",
                "category": "Data Structure",
                "time_complexity": "Search: O(n), Insert/Delete: O(1)",
                "space_complexity": "O(n)",
                "key_operations": "Node traversal, pointer manipulation",
                "best_case": "O(1) - insert at head",
                "worst_case": "O(n) - search/delete at tail"
            })
    
    # 4. Tree Algorithms - MORE SPECIFIC
    if "binarytree" in code_lower or "binary_tree" in code_lower or "class treenode" in code_lower:
        if "inorder" in code_lower:
            algorithms_found.append({
                "algorithm": "Binary Tree Inorder Traversal (Recursive)",
                "category": "Tree / DFS",
                "time_complexity": "O(n)",
                "space_complexity": "O(h) where h = tree height",
                "key_operations": "Left → Root → Right",
                "best_case": "O(n) - visits all nodes",
                "worst_case": "O(n) - visits all nodes"
            })
        elif "preorder" in code_lower:
            algorithms_found.append({
                "algorithm": "Binary Tree Preorder Traversal (Recursive)",
                "category": "Tree / DFS",
                "time_complexity": "O(n)",
                "space_complexity": "O(h) where h = tree height",
                "key_operations": "Root → Left → Right",
                "best_case": "O(n) - visits all nodes",
                "worst_case": "O(n) - visits all nodes"
            })
        elif "postorder" in code_lower:
            algorithms_found.append({
                "algorithm": "Binary Tree Postorder Traversal (Recursive)",
                "category": "Tree / DFS",
                "time_complexity": "O(n)",
                "space_complexity": "O(h) where h = tree height",
                "key_operations": "Left → Right → Root",
                "best_case": "O(n) - visits all nodes",
                "worst_case": "O(n) - visits all nodes"
            })
        elif "level_order" in code_lower or "bfs" in code_lower:
            algorithms_found.append({
                "algorithm": "Binary Tree Level Order Traversal (BFS)",
                "category": "Tree / BFS",
                "time_complexity": "O(n)",
                "space_complexity": "O(n)",
                "key_operations": "Visit nodes level by level",
                "best_case": "O(n) - visits all nodes",
                "worst_case": "O(n) - visits all nodes"
            })
        else:
            algorithms_found.append({
                "algorithm": "Binary Tree Operations",
                "category": "Tree Data Structure",
                "time_complexity": "O(n)",
                "space_complexity": "O(n)",
                "key_operations": "Tree traversal and manipulation",
                "best_case": "O(n) - visits all nodes",
                "worst_case": "O(n) - visits all nodes"
            })
    
    # 5. Graph Algorithms
    if "bfs" in code_lower or "breadth_first" in code_lower:
        if "queue" in code_lower:
            algorithms_found.append({
                "algorithm": "Breadth-First Search (BFS) - Queue Based",
                "category": "Graph Traversal",
                "time_complexity": "O(V + E)",
                "space_complexity": "O(V)",
                "key_operations": "Explore neighbors level by level",
                "best_case": "O(V + E) - visits all vertices/edges",
                "worst_case": "O(V + E) - visits all vertices/edges"
            })
        else:
            algorithms_found.append({
                "algorithm": "Breadth-First Search (BFS)",
                "category": "Graph Traversal",
                "time_complexity": "O(V + E)",
                "space_complexity": "O(V)",
                "key_operations": "Explore neighbors level by level",
                "best_case": "O(V + E) - visits all vertices/edges",
                "worst_case": "O(V + E) - visits all vertices/edges"
            })
    
    if "dfs" in code_lower or "depth_first" in code_lower:
        if "recursive" in code_lower:
            algorithms_found.append({
                "algorithm": "Depth-First Search (DFS) - Recursive",
                "category": "Graph Traversal",
                "time_complexity": "O(V + E)",
                "space_complexity": "O(V)",
                "key_operations": "Explore as deep as possible first",
                "best_case": "O(V + E) - visits all vertices/edges",
                "worst_case": "O(V + E) - visits all vertices/edges"
            })
        else:
            algorithms_found.append({
                "algorithm": "Depth-First Search (DFS) - Iterative",
                "category": "Graph Traversal",
                "time_complexity": "O(V + E)",
                "space_complexity": "O(V)",
                "key_operations": "Explore as deep as possible first",
                "best_case": "O(V + E) - visits all vertices/edges",
                "worst_case": "O(V + E) - visits all vertices/edges"
            })
    
    if "dijkstra" in code_lower:
        if "heap" in code_lower or "priorityqueue" in code_lower:
            algorithms_found.append({
                "algorithm": "Dijkstra's Algorithm (Priority Queue)",
                "category": "Shortest Path",
                "time_complexity": "O((V + E) log V)",
                "space_complexity": "O(V)",
                "key_operations": "Relax edges using min-heap",
                "best_case": "O((V + E) log V)",
                "worst_case": "O((V + E) log V)"
            })
        else:
            algorithms_found.append({
                "algorithm": "Dijkstra's Algorithm (Array Based)",
                "category": "Shortest Path",
                "time_complexity": "O(V²)",
                "space_complexity": "O(V)",
                "key_operations": "Relax edges using array",
                "best_case": "O(V²) - scans all vertices",
                "worst_case": "O(V²) - scans all vertices"
            })
    
    # 6. Dynamic Programming
    if "fibonacci" in code_lower or "fib(" in code_lower:
        if "memo" in code_lower or "cache" in code_lower or "dp" in code_lower:
            algorithms_found.append({
                "algorithm": "Fibonacci (DP with Memoization)",
                "category": "Dynamic Programming",
                "time_complexity": "O(n)",
                "space_complexity": "O(n)",
                "key_operations": "Cache computed values",
                "best_case": "O(n) - computes each once",
                "worst_case": "O(n) - computes each once"
            })
        else:
            algorithms_found.append({
                "algorithm": "Fibonacci (Recursive)",
                "category": "Recursion",
                "time_complexity": "O(2ⁿ)",
                "space_complexity": "O(n)",
                "key_operations": "Recursive calls with repeated work",
                "best_case": "O(2ⁿ) - exponential",
                "worst_case": "O(2ⁿ) - exponential"
            })
    
    if "knapsack" in code_lower:
        if "dp" in code_lower or "table" in code_lower:
            algorithms_found.append({
                "algorithm": "0/1 Knapsack (DP Table)",
                "category": "Dynamic Programming",
                "time_complexity": "O(n × W)",
                "space_complexity": "O(n × W)",
                "key_operations": "Fill DP table iteratively",
                "best_case": "O(n × W) - fills all entries",
                "worst_case": "O(n × W) - fills all entries"
            })
        else:
            algorithms_found.append({
                "algorithm": "0/1 Knapsack (Recursive)",
                "category": "Dynamic Programming",
                "time_complexity": "O(2ⁿ)",
                "space_complexity": "O(n)",
                "key_operations": "Recursive decisions with repeated work",
                "best_case": "O(2ⁿ) - exponential",
                "worst_case": "O(2ⁿ) - exponential"
            })
    
    # 7. Mathematical / Number Theory
    if "prime" in code_lower:
        if "sqrt" in code_lower:
            algorithms_found.append({
                "algorithm": "Prime Number Detection (Optimized)",
                "category": "Number Theory",
                "time_complexity": "O(√n)",
                "space_complexity": "O(1)",
                "key_operations": "Check up to sqrt(n)",
                "best_case": "O(√n) - checks until sqrt",
                "worst_case": "O(√n) - checks until sqrt"
            })
        else:
            algorithms_found.append({
                "algorithm": "Prime Number Detection (Naive)",
                "category": "Number Theory",
                "time_complexity": "O(n)",
                "space_complexity": "O(1)",
                "key_operations": "Check all numbers up to n",
                "best_case": "O(n) - checks all numbers",
                "worst_case": "O(n) - checks all numbers"
            })
    
    if "factorial" in code_lower:
        if "return n * factorial" in code_lower:
            algorithms_found.append({
                "algorithm": "Factorial (Recursive)",
                "category": "Recursion / Math",
                "time_complexity": "O(n)",
                "space_complexity": "O(n)",
                "key_operations": "Recursive multiplication",
                "best_case": "O(n) - n recursive calls",
                "worst_case": "O(n) - n recursive calls"
            })
        else:
            algorithms_found.append({
                "algorithm": "Factorial (Iterative)",
                "category": "Mathematics",
                "time_complexity": "O(n)",
                "space_complexity": "O(1)",
                "key_operations": "Loop multiplication",
                "best_case": "O(n) - n iterations",
                "worst_case": "O(n) - n iterations"
            })
    
    # 8. String/Array Manipulation - MORE SPECIFIC
    if "reverse" in code_lower and ("string" in code_lower or "array" in code_lower or "list" in code_lower):
        if "slicing" in code_lower or "::" in code_lower:
            algorithms_found.append({
                "algorithm": "String/Array Reversal (Slicing)",
                "category": "String/Array Manipulation",
                "time_complexity": "O(n)",
                "space_complexity": "O(n)",
                "key_operations": "Python slicing [::-1]",
                "best_case": "O(n) - creates new list",
                "worst_case": "O(n) - creates new list"
            })
        else:
            algorithms_found.append({
                "algorithm": "String/Array Reversal (Two-Pointer)",
                "category": "String/Array Manipulation",
                "time_complexity": "O(n)",
                "space_complexity": "O(1)",
                "key_operations": "Swap elements from both ends",
                "best_case": "O(n) - n/2 swaps",
                "worst_case": "O(n) - n/2 swaps"
            })
    
    if "palindrome" in code_lower:
        if "slicing" in code_lower or "::" in code_lower:
            algorithms_found.append({
                "algorithm": "Palindrome Checker (Slicing)",
                "category": "String Manipulation",
                "time_complexity": "O(n)",
                "space_complexity": "O(n)",
                "key_operations": "Compare string with reverse",
                "best_case": "O(n) - creates new string",
                "worst_case": "O(n) - creates new string"
            })
        else:
            algorithms_found.append({
                "algorithm": "Palindrome Checker (Two-Pointer)",
                "category": "String Manipulation",
                "time_complexity": "O(n)",
                "space_complexity": "O(1)",
                "key_operations": "Compare from both ends",
                "best_case": "O(n) - checks n/2 pairs",
                "worst_case": "O(n) - checks n/2 pairs"
            })
    
    # 9. Matrix Operations
    if "matrix" in code_lower or "mat" in code_lower:
        if "transpose" in code_lower:
            algorithms_found.append({
                "algorithm": "Matrix Transpose",
                "category": "Matrix Operations",
                "time_complexity": "O(m × n)",
                "space_complexity": "O(m × n)",
                "key_operations": "Swap rows and columns",
                "best_case": "O(m × n) - visits all elements",
                "worst_case": "O(m × n) - visits all elements"
            })
        elif "multiply" in code_lower:
            algorithms_found.append({
                "algorithm": "Matrix Multiplication",
                "category": "Matrix Operations",
                "time_complexity": "O(m × n × p)",
                "space_complexity": "O(m × p)",
                "key_operations": "Dot product of rows and columns",
                "best_case": "O(m × n × p) - standard algorithm",
                "worst_case": "O(m × n × p) - standard algorithm"
            })
        else:
            algorithms_found.append({
                "algorithm": "Matrix Operations",
                "category": "Matrix",
                "time_complexity": "Depends on operation",
                "space_complexity": "Depends on operation",
                "key_operations": "Various matrix operations",
                "best_case": "Depends on operation",
                "worst_case": "Depends on operation"
            })
    
    # 10. If no specific algorithm found
    if not algorithms_found:
        # Check code characteristics
        function_count = len(re.findall(r"def\s+\w+\s*\(", code_lower))
        conditional_count = code_lower.count("if ") + code_lower.count("elif ")
        var_count = len(re.findall(r"\b\w+\s*=\s*", code_lower))
        loop_count = code_lower.count("for ") + code_lower.count("while ")
        
        # Determine if it's a class-based program
        if "class " in code_lower:
            algorithms_found.append({
                "algorithm": "Object-Oriented Program",
                "category": "OOP",
                "time_complexity": "Depends on methods",
                "space_complexity": "Depends on memory usage",
                "key_operations": "Class methods and attributes",
                "best_case": "Depends on specific operations",
                "worst_case": "Depends on specific operations"
            })
        elif loop_count == 0 and conditional_count > 0:
            algorithms_found.append({
                "algorithm": "Conditional Branching Program",
                "category": "Decision Making",
                "time_complexity": "O(1)",
                "space_complexity": "O(1)",
                "key_operations": "If-else decisions",
                "best_case": "O(1) - constant time",
                "worst_case": "O(1) - constant time"
            })
        elif loop_count == 1:
            algorithms_found.append({
                "algorithm": "Single Loop Iteration",
                "category": "Iterative",
                "time_complexity": "O(n)",
                "space_complexity": "O(1)",
                "key_operations": "For/While loop",
                "best_case": "O(n) - n iterations",
                "worst_case": "O(n) - n iterations"
            })
        elif loop_count == 2:
            if "for i in range" in code_lower and "for j in range" in code_lower:
                algorithms_found.append({
                    "algorithm": "Nested Loop (N² Operations)",
                    "category": "Iterative",
                    "time_complexity": "O(n²)",
                    "space_complexity": "O(1)",
                    "key_operations": "Nested loops",
                    "best_case": "O(n²) - n² iterations",
                    "worst_case": "O(n²) - n² iterations"
                })
            else:
                algorithms_found.append({
                    "algorithm": "Two Independent Loops",
                    "category": "Iterative",
                    "time_complexity": "O(n)",
                    "space_complexity": "O(1)",
                    "key_operations": "Sequential loops",
                    "best_case": "O(n) - n iterations",
                    "worst_case": "O(n) - n iterations"
                })
        elif loop_count >= 3:
            algorithms_found.append({
                "algorithm": "Multiple Nested Loops (Nᵏ)",
                "category": "Iterative",
                "time_complexity": "O(nᵏ)",
                "space_complexity": "O(1)",
                "key_operations": "K-level nested loops",
                "best_case": "O(nᵏ) - all combinations",
                "worst_case": "O(nᵏ) - all combinations"
            })
        elif function_count > 2:
            algorithms_found.append({
                "algorithm": "Multi-Function Program",
                "category": "General Python",
                "time_complexity": "Depends on functions",
                "space_complexity": "Depends on functions",
                "key_operations": "Multiple function calls",
                "best_case": "Depends on functions",
                "worst_case": "Depends on functions"
            })
        else:
            algorithms_found.append({
                "algorithm": "Basic Python Program",
                "category": "General",
                "time_complexity": "O(1) to O(n)",
                "space_complexity": "O(1) to O(n)",
                "key_operations": "Sequential execution",
                "best_case": "O(1) - simple operations",
                "worst_case": "O(n) - with loops"
            })
    
    # If multiple algorithms found, combine them
    if len(algorithms_found) > 1:
        algorithm_names = [a["algorithm"] for a in algorithms_found]
        combined_name = " + ".join(algorithm_names[:2])
        if len(algorithm_names) > 2:
            combined_name += f" + {len(algorithm_names) - 2} more"
        
        return {
            "algorithm": combined_name,
            "category": algorithms_found[0]["category"],
            "time_complexity": algorithms_found[0]["time_complexity"],
            "space_complexity": algorithms_found[0]["space_complexity"],
            "key_operations": algorithms_found[0]["key_operations"],
            "best_case": algorithms_found[0]["best_case"],
            "worst_case": algorithms_found[0]["worst_case"],
            "detected_count": len(algorithms_found),
            "detected_algorithms": [a["algorithm"] for a in algorithms_found]
        }
    
    result = algorithms_found[0] if algorithms_found else {
        "algorithm": "Unknown Program",
        "category": "General",
        "time_complexity": "Unknown",
        "space_complexity": "Unknown",
        "key_operations": "Code analysis required",
        "best_case": "Unknown",
        "worst_case": "Unknown"
    }
    
    return result
    
    # ========== DEFAULT - Try to infer from code patterns ==========
    
    # Count number of functions
    function_count = len(re.findall(r"def\s+\w+\s*\(", code_lower))
    
    # Count conditionals
    conditional_count = code_lower.count("if ") + code_lower.count("elif ")
    
    # Count variable assignments
    var_count = len(re.findall(r"\b\w+\s*=\s*", code_lower))
    
    if function_count > 2:
        algorithm_name = "Multi-Function Program"
        complexity = "O(n × m)"
    elif conditional_count > 5:
        algorithm_name = "Multi-Condition Decision Tree"
        complexity = "O(1) to O(n)"
    elif var_count > 10:
        algorithm_name = "Data Processing Script"
        complexity = "Depends on operations"
    else:
        algorithm_name = "Basic Python Program"
        complexity = "O(1) to O(n)"
    
    return {
        "algorithm": algorithm_name,
        "category": "General Python",
        "time_complexity": complexity,
        "space_complexity": "O(1) to O(n)"
    }


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
            model="llama-3.3-70b-versatile",  # Updated
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

    draw.text((48, 34), "CODE2VIDEO AI STUDIO", font=FONT_XL, fill=(255, 255, 255))
    draw.text((50, 78), scene_title[:48], font=FONT_MD, fill=(153, 238, 255))

    draw.line((0, 114, VIDEO_W, 114), fill=(255, 255, 255, 35), width=1)
    draw.line((890, 118, 890, 500), fill=(255, 255, 255, 24), width=1)
    draw.line((58, 626, 1222, 626), fill=(255, 255, 255, 18), width=1)

    for x in [62, 94, 126]:
        draw.ellipse((x, 26, x + 12, 38), fill=(255, 191, 36))

    for i in range(6):
        draw.line((56 + i * 180, 626, 56 + i * 180 + 120, 626), fill=(255, 191, 36), width=3)

    img.save(output_path)


def _build_code_overlay(full_code, active_line, focus_text, output_path):
    short_focus = _short_focus_label(focus_text)

    panel = Image.new("RGBA", (760, 355), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)

    shadow = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((12, 14, 748, 343), radius=28, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    panel.alpha_composite(shadow)

    _rounded_box(draw, (0, 0, 736, 330), fill=(10, 21, 37, 238), outline=(79, 209, 197, 120), width=2, radius=28)

    draw.text((28, 18), "CODE FLOW", font=FONT_LG, fill=(255, 255, 255))
    draw.text((28, 52), short_focus[:40], font=FONT_SM, fill=(150, 226, 255))

    formatter = ImageFormatter(style="monokai", font_size=20, line_numbers=True)
    code_img = Image.open(BytesIO(highlight(full_code, PythonLexer(), formatter))).convert("RGBA")

    max_w, max_h = 670, 185
    code_img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)

    code_x = 28
    code_y = 82

    code_bg = Image.new("RGBA", (max_w + 26, max_h + 24), (17, 24, 39, 220))
    cb_draw = ImageDraw.Draw(code_bg)
    cb_draw.rounded_rectangle((0, 0, max_w + 26, max_h + 24), radius=18, fill=(17, 24, 39, 220))
    panel.alpha_composite(code_bg, (code_x - 13, code_y - 12))
    panel.alpha_composite(code_img, (code_x, code_y))

    line_y = code_y + max(0, active_line) * 26

    glow = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.rounded_rectangle(
        (code_x - 4, line_y + 20, code_x + 500, line_y + 24),
        radius=3,
        fill=(255, 196, 54, 110)
    )
    glow = glow.filter(ImageFilter.GaussianBlur(3))
    panel.alpha_composite(glow)

    draw.line(
        (code_x - 2, line_y + 22, code_x + 500, line_y + 22),
        fill=(255, 211, 92, 220),
        width=2
    )

    badge_box = draw.textbbox((0, 0), short_focus, font=FONT_CODE_BADGE)
    badge_w = min(320, badge_box[2] - badge_box[0] + 34)
    _rounded_box(draw, (28, 280, 28 + badge_w, 310), fill=(20, 94, 115, 235), outline=(94, 224, 213), width=2, radius=14)
    draw.text((42, 288), short_focus[:28], font=FONT_CODE_BADGE, fill=(236, 255, 255))

    panel.save(output_path)


def _build_vars_overlay(variables, scene_type, output_text, changed_keys, output_path):
    panel = Image.new("RGBA", (360, 355), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)

    shadow = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((12, 14, 348, 343), radius=28, fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    panel.alpha_composite(shadow)

    _rounded_box(draw, (0, 0, 336, 330), fill=(11, 29, 34, 240), outline=(251, 191, 36, 120), width=2, radius=28)

    draw.text((24, 18), "STATE PANEL", font=FONT_LG, fill=(255, 255, 255))
    scene_label = str(scene_type).upper().replace("_", " ")
    draw.text((24, 54), scene_label[:24], font=FONT_BADGE, fill=(255, 201, 74))

    y = 86

    shown_items = []
    for k, v in variables.items():
        if k in changed_keys and not _is_emptyish(v):
            shown_items.append((k, v))

    if not shown_items:
        shown_items = _extract_non_empty_items(variables, limit=3)

    shown_items = shown_items[:3]

    if not shown_items:
        _rounded_box(draw, (24, y, 312, y + 58), fill=(18, 48, 58, 220), outline=(94, 224, 213), width=2, radius=16)
        draw.text((40, y + 17), "No state changed in this step", font=FONT_SM, fill=(218, 240, 247))
        y += 70

    for key, val in shown_items:
        changed = key in changed_keys
        box_fill = (24, 78, 99, 240) if changed else (18, 48, 58, 220)
        outline = (255, 214, 92) if changed else (94, 224, 213)
        title_color = (255, 225, 122) if changed else (255, 216, 107)

        _rounded_box(draw, (24, y, 312, y + 58), fill=box_fill, outline=outline, width=3 if changed else 2, radius=16)
        draw.text((40, y + 8), str(key)[:18], font=FONT_BADGE, fill=title_color)

        value_text = _safe_repr(val, 48)
        lines = _wrap_text(value_text, FONT_VAR, 230, draw)
        for idx, line in enumerate(lines[:2]):
            draw.text((40, y + 28 + idx * 15), line, font=FONT_VAR, fill=(236, 255, 255))
        y += 66

    takeaway = output_text if str(output_text).strip().lower() not in {"", "none", "null"} else "Program state updated."

    if "found" in str(output_text).lower():
        result_idx = _extract_result_value(variables, output_text, "")
        takeaway = f"Target found at index {result_idx}" if result_idx is not None else "Target found successfully"

    _rounded_box(draw, (24, 248, 312, 320), fill=(36, 25, 8, 230), outline=(255, 199, 90), width=2, radius=18)
    draw.text((40, 262), "KEY TAKEAWAY", font=FONT_BADGE, fill=(255, 208, 112))
    out_lines = _wrap_text(takeaway, FONT_SM, 240, draw)
    for idx, line in enumerate(out_lines[:2]):
        draw.text((40, 284 + idx * 16), line, font=FONT_SM, fill=(255, 246, 225))

    panel.save(output_path)

def _build_subtitle_overlay(narration, step_no, total_steps, output_path, language="english"):
    simple = _simplify_subtitle(narration)

    subtitle_font = _load_font(24, bold=False, language=language)
    badge_font = _load_font(18, bold=True, language="english")

    panel = Image.new("RGBA", (1040, 78), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)

    shadow = Image.new("RGBA", panel.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((8, 8, 1032, 70), radius=18, fill=(0, 0, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    panel.alpha_composite(shadow)

    _rounded_box(draw, (0, 0, 1024, 62), fill=(3, 10, 18, 235), outline=(80, 198, 255, 60), width=2, radius=18)

    badge = f"STEP {step_no}/{total_steps}"
    badge_box = draw.textbbox((0, 0), badge, font=badge_font)
    badge_w = badge_box[2] - badge_box[0] + 22
    _rounded_box(draw, (18, 18, 18 + badge_w, 50), fill=(18, 75, 95, 235), outline=(94, 224, 213), width=2, radius=10)
    draw.text((30, 24), badge, font=badge_font, fill=(237, 255, 255))

    lines = _wrap_text(simple, subtitle_font, 760, draw)
    for idx, line in enumerate(lines[:2]):
        draw.text((170, 18 + idx * 22), line, font=subtitle_font, fill=(255, 255, 255))

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
            outline = (255, 201, 74)
            text_color = (255, 245, 210)

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
    draw.line((x, y_base, x, y_base + 18), fill=(255, 191, 36), width=3)
    draw.polygon([(x, y_base + 18), (x - 7, y_base + 8), (x + 7, y_base + 8)], fill=(255, 191, 36))
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


def _panel_base(title, subtitle, height=96):
    panel = Image.new("RGBA", (760, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    _rounded_box(draw, (0, 0, 760, height - 8), fill=(8, 27, 38, 220), outline=(94, 224, 213, 120), width=2, radius=18)
    draw.text((22, 10), title, font=FONT_LG, fill=(255, 255, 255))
    draw.text((22, 36), subtitle, font=FONT_SM, fill=(173, 235, 255))
    return panel, draw


def _build_scalar_update_overlay(payload, output_path):
    panel, draw = _panel_base("VALUE CHANGE", "A variable changed in this step")
    key = payload.get("key", "value")
    value = payload.get("value", "")

    _rounded_box(draw, (24, 60, 320, 96), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=12)
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
        draw.text((620, 14), f"+ {str(added)[:12]}", font=FONT_BADGE, fill=(255, 227, 128))
    elif action == "list_remove":
        draw.text((604, 14), "item removed", font=FONT_BADGE, fill=(255, 227, 128))

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
        outline = (255, 201, 74) if highlight else (94, 224, 213)
        left = x + (idx % 2) * 350
        top = y + (idx // 2) * 22
        _rounded_box(draw, (left, top, left + 320, top + 18), fill=fill, outline=outline, width=3 if highlight else 2, radius=8)
        draw.text((left + 10, top + 1), f"{k}: {_safe_repr(v, 26)}", font=FONT_SM, fill=(255, 255, 255))

    panel.save(output_path)


def _build_loop_progress_overlay(payload, output_path):
    panel, draw = _panel_base("LOOP PROGRESS", "Iteration values change live")
    key = payload.get("key", "i")
    value = payload.get("value", 0)
    changed_items = payload.get("changed_items", [])

    _rounded_box(draw, (24, 58, 180, 96), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=12)
    draw.text((44, 68), f"{key} = {value}", font=FONT_BADGE, fill=(255, 255, 255))

    x = 220
    for item_key, item_val in changed_items[:3]:
        _rounded_box(draw, (x, 58, x + 160, 96), fill=(24, 78, 99, 240), outline=(94, 224, 213), width=2, radius=12)
        draw.text((x + 10, 68), f"{item_key}: {_compact_value_description(item_val)}"[:22], font=FONT_BADGE, fill=(255, 255, 255))
        x += 176

    panel.save(output_path)


def _build_print_output_overlay(payload, output_path):
    panel, draw = _panel_base("OUTPUT CONSOLE", "Program output is shown clearly", height=118)

    _rounded_box(draw, (24, 48, 736, 102), fill=(7, 14, 20, 235), outline=(94, 224, 213), width=2, radius=12)
    console_text = _safe_repr(payload.get("text", "Program produced output."), 100)

    lines = _wrap_text(console_text, FONT_MD, 650, draw)
    for idx, line in enumerate(lines[:2]):
        draw.text((42, 60 + idx * 22), line, font=FONT_MD, fill=(210, 245, 210))

    panel.save(output_path)


def _build_waiting_input_overlay(payload, output_path):
    panel, draw = _panel_base("WAITING FOR INPUT", "Program is waiting for the next user action", height=118)

    label = payload.get("label", "Waiting for input")
    menu_lines = payload.get("menu_lines", [])[:3]

    _rounded_box(draw, (24, 48, 280, 102), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=12)
    lines = _wrap_text(label, FONT_MD, 210, draw)
    for idx, line in enumerate(lines[:2]):
        draw.text((40, 60 + idx * 20), line, font=FONT_MD, fill=(255, 255, 255))

    if menu_lines:
        left = 300
        top = 48
        _rounded_box(draw, (left, top, 736, 102), fill=(7, 14, 20, 235), outline=(94, 224, 213), width=2, radius=12)
        for i, line in enumerate(menu_lines[:2]):
            draw.text((left + 14, top + 8 + i * 20), line[:48], font=FONT_TINY, fill=(210, 245, 210))

    panel.save(output_path)


def _build_function_return_overlay(payload, output_path):
    panel, draw = _panel_base("RETURN VALUE", "A function returned a value")
    value = _safe_repr(payload.get("value", "result"), 32)

    _rounded_box(draw, (54, 58, 246, 96), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=12)
    draw.text((84, 69), value, font=FONT_BADGE, fill=(255, 255, 255))
    draw.line((246, 77, 360, 77), fill=(255, 201, 74), width=3)
    draw.polygon([(360, 77), (350, 71), (350, 83)], fill=(255, 201, 74))
    draw.text((380, 66), "returned to caller", font=FONT_BADGE, fill=(255, 227, 128))

    panel.save(output_path)


def _build_function_call_overlay(payload, output_path):
    panel, draw = _panel_base("FUNCTION CALL", "Execution is entering a function")
    name = payload.get("name", "function")

    _rounded_box(draw, (60, 56, 250, 94), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=12)
    draw.text((86, 68), name[:20], font=FONT_BADGE, fill=(255, 255, 255))
    draw.line((250, 74, 370, 74), fill=(255, 201, 74), width=3)
    draw.polygon([(370, 74), (360, 68), (360, 80)], fill=(255, 201, 74))
    draw.text((392, 64), "entering body", font=FONT_BADGE, fill=(255, 227, 128))

    panel.save(output_path)


def _build_conditional_branch_overlay(payload, output_path):
    outcome = payload.get("outcome", "unknown")
    panel, draw = _panel_base("CONDITION FLOW", "A branch was chosen")

    draw.polygon([(150, 78), (210, 48), (270, 78), (210, 108)], fill=(24, 78, 99, 240), outline=(255, 201, 74))
    draw.text((188, 68), "if", font=FONT_BADGE, fill=(255, 255, 255))

    if outcome == "true":
        true_color = (255, 201, 74)
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
            _rounded_box(draw, (x, 62, x + 220, 98), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=12)
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

    low = _safe_int(curr_vars.get("low"))
    high = _safe_int(curr_vars.get("high"))
    mid = _safe_int(curr_vars.get("mid"))
    target = curr_vars.get("target", "")
    result_idx = _extract_result_value(curr_vars, output_text, narration)

    prev_low = _safe_int((prev_vars or {}).get("low"))
    prev_high = _safe_int((prev_vars or {}).get("high"))
    prev_mid = _safe_int((prev_vars or {}).get("mid"))

    found_mode = False

    if result_idx is not None and 0 <= result_idx < min(len(arr), 10):
        found_mode = True
    elif mid is not None and (
        "found" in str(output_text).lower()
        or "found" in str(narration).lower()
        or "target found" in str(output_text).lower()
        or "target found" in str(narration).lower()
    ):
        result_idx = mid
        found_mode = True

    if found_mode:
        panel, draw = _panel_base("SEARCH RESULT", "Target matched successfully")

        dim_idxs = set(i for i in range(min(len(arr), 10)) if i != result_idx)
        _draw_array_boxes(draw, arr, 22, 54, highlight_idxs={result_idx}, dim_idxs=dim_idxs)

        match_x = 22 + result_idx * (70 + 8) + 35
        _rounded_box(
            draw,
            (match_x - 42, 10, match_x + 42, 36),
            fill=(20, 110, 70, 240),
            outline=(255, 201, 74),
            width=2,
            radius=10
        )
        draw.text((match_x - 26, 15), "FOUND", font=FONT_BADGE, fill=(255, 255, 255))
        draw.line((match_x, 36, match_x, 54), fill=(255, 191, 36), width=3)
        draw.polygon([(match_x, 54), (match_x - 7, 44), (match_x + 7, 44)], fill=(255, 191, 36))

        if target != "":
            draw.text((566, 12), f"target={target}"[:18], font=FONT_BADGE, fill=(255, 227, 128))
        draw.text((566, 36), f"index={result_idx}", font=FONT_BADGE, fill=(255, 227, 128))

        panel.save(output_path)
        return

    panel, draw = _panel_base("SEARCH WINDOW", "Low, mid, and high pointers move")

    dim_idxs = set()
    if low is not None and high is not None:
        for idx in range(min(len(arr), 10)):
            if idx < low or idx > high:
                dim_idxs.add(idx)

    highlight = set(i for i in [low, mid, high] if i is not None)
    _draw_array_boxes(draw, arr, 22, 54, highlight_idxs=highlight, dim_idxs=dim_idxs)

    _draw_old_pointer(draw, "L", prev_low, 22, 108)
    _draw_old_pointer(draw, "M", prev_mid, 22, 108)
    _draw_old_pointer(draw, "H", prev_high, 22, 108)

    _draw_pointer(draw, "L", low, 22, 18)
    _draw_pointer(draw, "M", mid, 22, 18)
    _draw_pointer(draw, "H", high, 22, 18)

    if target != "":
        draw.text((620, 12), f"target={target}"[:16], font=FONT_BADGE, fill=(255, 227, 128))

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

    base_y = 96
    x0 = 22
    max_val = max([1] + [abs(int(x)) if isinstance(x, (int, float)) or str(x).isdigit() else 1 for x in curr_arr[:10]])

    for idx, item in enumerate(curr_arr[:10]):
        try:
            value_num = abs(int(item))
        except Exception:
            value_num = idx + 1
        x = x0 + idx * 72
        h = max(18, int((value_num / max_val) * 40))
        y = base_y - h

        fill = (24, 78, 99, 240)
        outline = (94, 224, 213)
        if idx in compare_idxs:
            fill = (42, 94, 120, 250)
            outline = (255, 201, 74)

        _rounded_box(draw, (x, y, x + 54, base_y), fill=fill, outline=outline, width=3 if idx in compare_idxs else 2, radius=10)

        txt = str(item)[:4]
        bbox = draw.textbbox((0, 0), txt, font=FONT_SM)
        tw = bbox[2] - bbox[0]
        draw.text((x + 27 - tw / 2, y - 20), txt, font=FONT_SM, fill=(255, 255, 255))

    if len(changed_idxs) >= 2:
        x1 = x0 + changed_idxs[0] * 72 + 27
        x2 = x0 + changed_idxs[1] * 72 + 27
        draw.line((x1, 16, x2, 16), fill=(255, 201, 74), width=3)
        draw.polygon([(x2, 16), (x2 - 8, 12), (x2 - 8, 20)], fill=(255, 201, 74))
        draw.text((x1, 22), "swap", font=FONT_BADGE, fill=(255, 230, 168))

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
    base_y = 102

    stable_stack = curr_stack[:]
    if action == "push" and stable_stack:
        stable_stack = stable_stack[:-1]

    for idx, val in enumerate(stable_stack[-4:]):
        y = base_y - (len(stable_stack[-4:]) - idx) * 26
        _rounded_box(draw, (tower_x, y, tower_x + 130, y + 22), fill=(24, 78, 99, 240), outline=(94, 224, 213), width=2, radius=8)
        draw.text((tower_x + 14, y + 2), str(_normalize_value(val))[:12], font=FONT_BADGE, fill=(255, 255, 255))

    top_slot_y = base_y - len(stable_stack[-4:]) * 26

    if action == "push" and moving_value is not None:
        _rounded_box(draw, (430, 30, 560, 54), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=8)
        draw.text((444, 34), f"incoming {str(_normalize_value(moving_value))}"[:16], font=FONT_BADGE, fill=(255, 255, 255))
        draw.line((560, 42, tower_x, top_slot_y + 10), fill=(255, 201, 74), width=3)
        draw.polygon([(tower_x, top_slot_y + 10), (tower_x + 8, top_slot_y + 5), (tower_x + 8, top_slot_y + 15)], fill=(255, 201, 74))
        _rounded_box(draw, (tower_x, top_slot_y, tower_x + 130, top_slot_y + 22), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=8)
        draw.text((tower_x + 14, top_slot_y + 2), str(_normalize_value(moving_value))[:12], font=FONT_BADGE, fill=(255, 255, 255))
    elif action == "pop" and moving_value is not None:
        pop_y = base_y - max(1, len(prev_stack[-4:])) * 26
        _rounded_box(draw, (tower_x, pop_y, tower_x + 130, pop_y + 22), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=8)
        draw.text((tower_x + 14, pop_y + 2), str(_normalize_value(moving_value))[:12], font=FONT_BADGE, fill=(255, 255, 255))
        _rounded_box(draw, (430, 20, 560, 44), fill=(42, 94, 120, 250), outline=(255, 201, 74), width=3, radius=8)
        draw.text((446, 24), f"popped {str(_normalize_value(moving_value))}"[:15], font=FONT_BADGE, fill=(255, 255, 255))
        draw.line((tower_x, pop_y + 10, 560, 32), fill=(255, 201, 74), width=3)
        draw.polygon([(560, 32), (552, 28), (552, 36)], fill=(255, 201, 74))

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
    y = 60
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
        outline = (255, 201, 74) if idx == current_idx else (94, 224, 213)

        _rounded_box(draw, (x, y, x + box_w, y + 36), fill=fill, outline=outline, width=3 if idx == current_idx else 2, radius=12)
        bbox = draw.textbbox((0, 0), str(val), font=FONT_BADGE)
        tw = bbox[2] - bbox[0]
        draw.text((x + box_w / 2 - tw / 2, y + 8), str(val)[:8], font=FONT_BADGE, fill=(255, 255, 255))

        if idx < len(values[:6]) - 1:
            start_x = x + box_w
            end_x = x + box_w + 28
            draw.line((start_x + 6, y + 18, end_x, y + 18), fill=(255, 201, 74), width=3)
            draw.polygon([(end_x, y + 18), (end_x - 8, y + 14), (end_x - 8, y + 22)], fill=(255, 201, 74))

        x += 112

    if prev_idx is not None and prev_idx != current_idx:
        old_x = 26 + prev_idx * 112 + box_w / 2
        draw.text((old_x - 12, 18), "old", font=FONT_SM, fill=(150, 170, 180))

    current_x = 26 + current_idx * 112 + box_w / 2
    _rounded_box(draw, (current_x - 28, 10, current_x + 28, 34), fill=(18, 75, 95, 235), outline=(94, 224, 213), width=2, radius=10)
    draw.text((current_x - 18, 14), "temp", font=FONT_SM, fill=(255, 255, 255))
    draw.line((current_x, 34, current_x, 58), fill=(255, 191, 36), width=3)
    draw.polygon([(current_x, 58), (current_x - 7, 48), (current_x + 7, 48)], fill=(255, 191, 36))

    panel.save(output_path)


def _build_recursion_overlay(curr_vars, prev_vars, output_text, output_path):
    return_mode = "return" in str(output_text).lower() or "return" in " ".join(curr_vars.keys()).lower()
    title = "RECURSION RETURN" if return_mode else "RECURSION TREE"
    subtitle = "Return values move upward" if return_mode else "Calls branch deeper"

    panel, draw = _panel_base(title, subtitle)

    depth = _extract_recursion_depth(curr_vars)
    centers = [(84, 80), (204, 56), (204, 102), (332, 44), (332, 80), (332, 116)]
    nodes = min(max(depth + 1, 2), len(centers))

    for i, (cx, cy) in enumerate(centers[:nodes]):
        if i > 0:
            px, py = centers[(i - 1) // 2]
            color = (255, 191, 36) if return_mode else (94, 224, 213)
            draw.line((px + 28, py, cx - 28, cy), fill=color, width=3)

        fill = (42, 94, 120, 250) if return_mode and i == 0 else (24, 78, 99, 240)
        outline = (255, 201, 74)
        draw.ellipse((cx - 24, cy - 18, cx + 24, cy + 18), fill=fill, outline=outline, width=2)
        draw.text((cx - 12, cy - 8), f"f{i}", font=FONT_BADGE, fill=(255, 255, 255))

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


def _build_visual_overlay(visual_type, payload, full_code, curr_vars, prev_vars, output_text, narration, output_path):
    if visual_type == "print_output":
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
    code_clip = code_clip.with_position((42, 122))

    vars_clip = ImageClip(vars_path).with_duration(safe_duration)
    vars_clip = vars_clip.with_position((894, 122))

    layout = _choose_layout(scene_title, visual_type, output_text, narration)

    concept_clip = ImageClip(concept_path).with_duration(safe_duration)
    subtitle_clip = ImageClip(subtitle_path).with_duration(safe_duration)

    if layout == "result":
        concept_clip = concept_clip.with_effects([vfx.Resize(1.05)])
        concept_clip = concept_clip.with_position((42, 490))
    elif layout == "terminal":
        concept_clip = concept_clip.with_position((42, 470))
    else:
        concept_clip = concept_clip.with_position((42, 470))

    subtitle_clip = subtitle_clip.with_position((116, 620))

    audio_clip = AudioFileClip(audio_path).subclipped(0, safe_duration)

    final = CompositeVideoClip(
        [bg_clip, code_clip, vars_clip, concept_clip, subtitle_clip],
        size=(VIDEO_W, VIDEO_H)
    ).with_duration(safe_duration).with_audio(audio_clip)

    return final


# ===============================
# VIDEO WORKER (UPDATED WITH GROQ)
# ===============================

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
- line_index
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
            model="llama-3.3-70b-versatile",  # ✅ NEW - working
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
            _build_code_overlay(code, line_index, focus_text, code_p)
            _build_vars_overlay(variables, scene_type, output_text, changed_keys, vars_p)
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
            clips.append(clip)

            previous_vars = dict(variables)

        tasks[tid]["status"] = "Compositing final educational video"
        tasks[tid]["progress"] = 95

        out = os.path.join(VIDEO_DIR, f"{tid}.mp4")

        final = concatenate_videoclips(clips, method="compose")
        final = final.with_duration(sum(c.duration for c in clips))

        final.write_videofile(
            out,
            fps=30,
            codec="libx264",
            bitrate="12000k",
            audio_codec="aac",
            preset="slow",
            threads=4,
            logger=None
        )

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
        return False, f"Syntax Error: {e.msg}"
    

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
    code = data.get("code", "")
    title = str(data.get("title", "")).strip()
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
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT user_email, task_id, rating, comment, created_at
        FROM feedback
        ORDER BY id DESC
    """)
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


@app.route("/api/analyze_code", methods=["POST"])
def api_analyze_code():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    code = data.get("code", "").strip()

    if not code:
        return jsonify({"error": "Code is required"}), 400

    return jsonify(_detect_complexity(code))


@app.route("/api/concept_summary", methods=["POST"])
def api_concept_summary():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    code = data.get("code", "").strip()

    if not code:
        return jsonify({"error": "Code required"}), 400

    try:
        prompt = f"""
Analyze this Python code and return ONLY valid JSON:

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
            model="llama-3.3-70b-versatile",  # Updated
            messages=[{"role": "user", "content": prompt}]
        )

        text_result = completion.choices[0].message.content or ""

        match = re.search(r"\{.*\}", text_result, re.DOTALL)
        if not match:
            return jsonify({"error": "Parsing failed"}), 500

        return jsonify(json.loads(match.group(0)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/explain_line", methods=["POST"])
def api_explain_line():
    if "user" not in session:
        return jsonify({"error": "Login required"}), 401

    data = request.json or {}
    code = data.get("code", "").strip()
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
            model="llama-3.3-70b-versatile",  # Updated
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
    code = str(data.get("code", "")).strip()
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
            model="llama-3.3-70b-versatile",  # Updated
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

    return jsonify([
        {"user": row["user_email"], "videos": row["videos"]}
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
    app.run(debug=True)