import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from ai_parser import parse_tasks
from datetime import datetime, timedelta
from collections import Counter
import uuid
import smtplib
from email.message import EmailMessage
from apscheduler.schedulers.background import BackgroundScheduler
from flask_socketio import SocketIO, emit, join_room
import json
from pywebpush import webpush
from dotenv import load_dotenv
import hashlib
import os

env = os.getenv("FLASK_ENV", "development")

if env == "production":
    load_dotenv(".env.production")
else:
    load_dotenv(".env.development")

# ----------------------------
# APP & SOCKETIO INIT
# ----------------------------
app = Flask(__name__)
# Get SECRET_KEY from env, fallback to dev-secret if not set
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")

#Secure session configuration
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=False,  
    SESSION_COOKIE_SAMESITE="Lax"
)

socketio = SocketIO(
    app,
    async_mode="eventlet",
    cors_allowed_origins="*",
    manage_session=True
)


DB_NAME = "tasks.db"

# ----------------------------
#Load VAPID keys from env
# ----------------------------
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")

# ----------------------------
# AUTH DECORATORS
# ----------------------------
from functools import wraps

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.", "warning")
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper

# ----------------------------
# DATABASE HELPERS
# ----------------------------
import sqlite3

DB_MODE_PRINTED = False

def get_db():
    global DB_MODE_PRINTED

    db_url = os.getenv("DATABASE_URL")

    if db_url:
        if not DB_MODE_PRINTED:
            print("🟣 Using PostgreSQL (production mode)")
            DB_MODE_PRINTED = True
        return psycopg2.connect(db_url)

    else:
        if not DB_MODE_PRINTED:
            print("🟢 Using SQLite (local dev mode)")
            DB_MODE_PRINTED = True
        return sqlite3.connect("smarttask.db")

def init_db():
    conn = get_db()

    if conn is None:
        print("⚠️ Skipping DB init (no connection)")
        return

    cursor = conn.cursor()

    is_sqlite = "sqlite3" in str(type(conn))

    # SQLite doesn't support this
    if is_sqlite:
        print("🟢 Initializing SQLite DB")
    else:
        print("🔵 Initializing PostgreSQL DB")

    # USERS
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id {} PRIMARY KEY,
            username TEXT UNIQUE,
            email TEXT UNIQUE,
            password TEXT
        )
    """.format("INTEGER" if is_sqlite else "SERIAL"))

    # TASKS
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id {} PRIMARY KEY,
            task TEXT,
            status TEXT,
            priority TEXT,
            deadline TEXT,
            due_time TEXT,
            notified INTEGER DEFAULT 0,
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """.format("INTEGER" if is_sqlite else "SERIAL"))

    # SUBSCRIPTIONS
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id {} PRIMARY KEY,
            endpoint TEXT UNIQUE,
            data TEXT,
            user_id INTEGER
        )
    """.format("INTEGER" if is_sqlite else "SERIAL"))

    # RESET TOKENS
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reset_tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

# ----------------------------
# INIT DB ON STARTUP (Render + Local)
# ----------------------------
try:
    init_db()
    print("✅ DB initialized (safe)")
except Exception as e:
    print("⚠️ DB init failed:", e)

# ----------------------------
# SUBSCRIPTIONS
# ----------------------------
def save_subscription(sub):
    if "user_id" not in session:
        return  # prevent saving without a logged-in user

    conn = get_db()
    cursor = conn.cursor()

    endpoint = sub.get("endpoint")
    user_id = session["user_id"]

    cursor.execute(
        "INSERT OR IGNORE INTO subscriptions (endpoint, data, user_id) VALUES (%s, %s, %s)",
        (endpoint, json.dumps(sub), user_id)
    )

    conn.commit()
    conn.close()

# ----------------------------
# SOCKET EVENTS
# ----------------------------
from flask_socketio import join_room

@socketio.on("connect")
def handle_connect():
    user_id = session.get("user_id")

    if not user_id:
        return False  # silently reject connection

    join_room(str(user_id))
    print(f"✅ User {user_id} connected to socket")

@socketio.on("disconnect")
def handle_disconnect():
    print("Client disconnected")

# ----------------------------
# TASK HELPERS
# ----------------------------
def get_tasks(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, task, status, priority, deadline, created_at
        FROM tasks
        WHERE user_id=%s
        ORDER BY CASE priority
            WHEN 'High' THEN 1
            WHEN 'Medium' THEN 2
            WHEN 'Low' THEN 3
        END
    """, (user_id,))
    tasks = cursor.fetchall()
    conn.close()
    return tasks

def generate_insight(tasks):
    total = len(tasks)
    completed = sum(1 for t in tasks if t[2] == "Completed")
    pending = total - completed
    high = sum(1 for t in tasks if t[3] == "High" and t[2] != "Completed")
    score = int((completed / total) * 100) if total else 0

    day_counter = Counter()
    for t in tasks:
        if t[2] == "Completed" and t[5]:
            try:
                day = datetime.strptime(t[5][:10], "%Y-%m-%d").strftime("%A")
                day_counter[day] += 1
            except Exception as e:
                print("Error:", e)

    suggestion = "Stay consistent."
    if day_counter:
        best_day = day_counter.most_common(1)[0][0]

        if high > 0:
            suggestion = "⚠️ You still have high priority tasks pending. Focus on them first."
        elif pending > 5:
            suggestion = "📌 You have many pending tasks. Try clearing small ones quickly."
        elif completed == total and total > 0:
            suggestion = "🎉 Perfect! Everything completed."
        else:
            suggestion = f"📈 You are most productive on {best_day}s. Plan important tasks then."

    return {
        "total": total,
        "completed": completed,
        "pending": pending,
        "high_priority": high,
        "score": score,
        "suggestion": suggestion
    }

def get_weekly_data(tasks):
    today = datetime.now().date()
    days = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
    day_labels = [d.strftime("%a") for d in days]

    weekly_counts = []

    for d in days:
        count = 0
        for t in tasks:
            status = t[2]       # Completed / Pending
            deadline = t[4]     # Deadline from DB
            created_at = t[5]   # Created date

            if status != "Completed":
                continue

            # Use deadline if exists, else fallback to created_at
            date_str = deadline if deadline else created_at
            if not date_str:
                continue

            try:
                task_date = datetime.fromisoformat(date_str[:10]).date()
                if task_date == d:
                    count += 1
            except Exception as e:
                print("Date parse error in get_weekly_data:", e)

        weekly_counts.append(count)

    return day_labels, weekly_counts

def check_reminders(tasks):
    today = datetime.now().date()
    reminders = []

    for t in tasks:
        deadline = t[4]
        if deadline:
            try:
                deadline_date = datetime.strptime(deadline, "%Y-%m-%d").date()
                if t[2] != "Completed":
                    if deadline_date == today:
                        reminders.append(f"⏰ '{t[1]}' is due today!")
                    elif deadline_date < today:
                        reminders.append(f"⚠️ '{t[1]}' is overdue!")
            except:
                pass

    return reminders

# ----------------------------
# PUSH NOTIFICATIONS
# ----------------------------
def send_push(title, user_id):
    conn = get_db()
    cursor = conn.cursor()

    # 🔹 Query all subscriptions at once
    cursor.execute("SELECT data, user_id FROM subscriptions")
    all_subs = cursor.fetchall()

    # Filter subscriptions for this user
    user_subs = [row for row in all_subs if row[1] == user_id]

    for row in user_subs:
        sub = json.loads(row[0])
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": "mailto:test@test.com"}
            )
        except Exception as e:
            print("Push error:", e)
            if "410" in str(e) or "gone" in str(e).lower():
                cursor.execute("DELETE FROM subscriptions WHERE data=%s", (row[0],))

    conn.commit()
    conn.close()

def notify_task(task):
    task_name = task['task']
    is_overdue = task.get('overdue', False)
    user_id = task.get("user_id")

    if not user_id:
        return

    # Always send if overdue or due today
    if is_overdue or task.get("deadline", False):
        send_push(task_name, user_id)
        socketio.emit(
            "task_due",
            {"title": f"{task_name}{' (Overdue!)' if is_overdue else ''}"},
            room=str(user_id)
        )

# ----------------------------
# AUTH CHECK
# ----------------------------
def is_logged_in():
    return "user_id" in session

# ----------------------------
# ROUTES
# ----------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    conn = None

    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        password = generate_password_hash(request.form["password"])

        try:
            conn = get_db()
            cursor = conn.cursor()

            cursor.execute(
                """
                INSERT INTO users (username, email, password)
                VALUES (%s, %s, %s)
                """,
                (username, email, password)
            )

            conn.commit()

            flash("Account created! Please login.", "success")
            return redirect("/login")

        except Exception as e:
            if conn:
                conn.rollback()

            if "duplicate key" in str(e):
                flash("Username or Email already exists!", "danger")
            else:
                print("❌ Register error:", e)
                flash("Something went wrong. Try again.", "danger")

        finally:
            if conn:
                conn.close()

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    conn = None

    if request.method == "POST":
        login_input = request.form.get("username")
        password = request.form.get("password")

        try:
            conn = get_db()
            cursor = conn.cursor()

            cursor.execute(
                "SELECT id, username, password FROM users WHERE username=%s OR email=%s",
                (login_input, login_input)
            )

            user = cursor.fetchone()

            if user and check_password_hash(user[2], password):
                session["user_id"] = user[0]
                session["username"] = user[1]

                flash(f"Welcome back, {user[1]}!", "success")
                return redirect("/")
            else:
                flash("Invalid credentials!", "danger")

        except Exception as e:
            print("❌ Login error:", e)
            flash("Login failed. Try again.", "danger")

        finally:
            if conn:
                conn.close()

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "warning")
    return redirect("/login")

@app.route("/subscribe", methods=["POST"])
def subscribe():
    data = request.get_json()
    if not data or "endpoint" not in data:
        return {"error": "Invalid subscription"}, 400
    save_subscription(data)
    print("✅ New subscription saved:", data)
    return {"success": True}

# ----------------------------
# FORGOT PASSWORD
# ----------------------------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email")

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, username FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()
        username = user[1] if user else "there"
        conn.close()

        if user:
            token = str(uuid.uuid4())
            hashed_token = hashlib.sha256(token.encode()).hexdigest()
            expires_at = (datetime.now() + timedelta(hours=1)).isoformat()

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO reset_tokens (token,user_id,expires_at) VALUES (%s, %s,%s)",
                (hashed_token, user[0], expires_at)
            )
            conn.commit()
            conn.close()

            msg = EmailMessage()
            msg['Subject'] = "SmartTask Password Reset"
            msg['From'] = os.getenv("EMAIL_USER")
            msg['To'] = email

            BASE_URL = os.getenv("BASE_URL", "http://localhost:5000")
            reset_link = f"{BASE_URL}/reset-password?token={token}"

            # Plain fallback (for non-HTML clients)
            msg.set_content("This email requires an HTML-supported client.")

            # HTML Email
            msg.add_alternative(f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reset Password</title>
</head>

<body style="margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,sans-serif;">

<!-- PREVIEW TEXT (hidden) -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">
Reset your SmartTask password and get back on track quickly.
</div>

<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;padding:20px 0;">
<tr>
<td align="center">

<table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:12px;padding:30px;box-shadow:0 8px 20px rgba(0,0,0,0.06);">

<tr>
<td align="center">

<!-- LOGO -->
<img src="https://cdn-icons-png.flaticon.com/512/1828/1828817.png" width="50" style="margin-bottom:10px;" />

<!-- BRAND -->
<h2 style="margin:0;font-size:24px;">
<span style="color:#4A90E2;font-weight:700;">Smart</span>
<span style="color:#111;font-weight:700;">Task</span>
</h2>

</td>
</tr>

<tr>
<td align="center" style="padding-top:20px;">

<h3 style="margin:0;color:#333;">Hi {username} 👋,</h3>

<p style="color:#666;font-size:15px;line-height:1.6;margin-top:15px;">
We received a request to reset your password.
</p>

<p style="color:#666;font-size:15px;line-height:1.6;">
Secure your account and get back to your tasks in seconds 🚀
</p>

</td>
</tr>

<!-- BUTTON -->
<tr>
<td align="center" style="padding:25px 0;">
<a href="{reset_link}"
   style="display:inline-block;padding:14px 30px;
          background:linear-gradient(135deg,#4A90E2,#007BFF);
          color:#ffffff;text-decoration:none;
          border-radius:10px;font-weight:bold;
          box-shadow:0 4px 12px rgba(0,123,255,0.3);">
Reset your password
</a>
</td>
</tr>

<!-- EXPIRY -->
<tr>
<td align="center">
<p style="font-size:13px;color:#999;">
This link will expire in 60 minutes.
</p>
</td>
</tr>

<!-- SECURITY WARNING -->
<tr>
<td align="center" style="padding-top:20px;">
<p style="font-size:13px;color:#d9534f;line-height:1.5;">
If you didn’t request this password reset, please secure your account immediately.
</p>
</td>
</tr>

<!-- FALLBACK LINK -->
<tr>
<td align="center" style="padding-top:15px;">
<p style="font-size:12px;color:#888;">
Or copy and paste this link into your browser:
</p>

<p style="font-size:12px;color:#4A90E2;word-break:break-all;">
{reset_link}
</p>
</td>
</tr>

<!-- FOOTER -->
<tr>
<td align="center" style="padding-top:30px;border-top:1px solid #eee;">
<p style="font-size:12px;color:#aaa;">
SmartTask • Productivity Simplified
</p>
</td>
</tr>

</table>

</td>
</tr>
</table>

</body>
</html>
""", subtype="html")

            try:
                print("📧 Attempting to send email...")

                email_user = os.getenv("EMAIL_USER")
                email_pass = os.getenv("EMAIL_PASS")

                print("🔑 EMAIL_USER:", email_user)
                print("🔑 EMAIL_PASS loaded:", bool(email_pass))

                if not email_user or not email_pass:
                    raise ValueError("Email credentials missing")

                with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
                    smtp.login(email_user, email_pass)
                    print("✅ Logged into SMTP")

                    smtp.send_message(msg)
                    print("✅ Email sent successfully")

                flash("Reset link sent to your email.", "info")

            except Exception as e:
                import traceback
                print("🔥 EMAIL ERROR:")
                traceback.print_exc()
                flash("Failed to send reset email. Try again later.", "danger")

        else:
            flash("If that email exists, a reset link has been sent.", "info")

    return render_template("forgot_password.html")

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = request.args.get("token")

    if not token:
        flash("Invalid or expired token.", "danger")
        return redirect("/login")

    # Hash token safely AFTER validation
    hashed_token = hashlib.sha256(token.encode()).hexdigest()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT user_id, expires_at FROM reset_tokens WHERE token=%s",
        (hashed_token,)
    )
    row = cursor.fetchone()

    # Validate token existence + expiry
    if not row:
        conn.close()
        flash("Invalid or expired token.", "danger")
        return redirect("/login")

    try:
        expiry_time = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        expiry_time = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")

    if expiry_time < datetime.now():
        cursor.execute("DELETE FROM reset_tokens WHERE token=%s", (hashed_token,))
        conn.commit()
        conn.close()
        flash("Token expired. Please request a new reset link.", "danger")
        return redirect("/login")

    # Handle password reset
    if request.method == "POST":
        new_password = generate_password_hash(request.form.get("password"))

        cursor.execute(
            "UPDATE users SET password=%s WHERE id=%s",
            (new_password, row[0])
        )

        # 🔥 one-time use token (important fix)
        cursor.execute(
            "DELETE FROM reset_tokens WHERE token=%s",
            (hashed_token,)
        )

        conn.commit()
        conn.close()

        flash("Password successfully reset! Please login.", "success")
        return redirect("/login")

    conn.close()
    return render_template("reset_password.html", token=token)

# ----------------------------
# DASHBOARD
# ----------------------------
@app.route("/")
def index():
    if not is_logged_in():
        return redirect("/login")
    user_id = session["user_id"]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users WHERE id=%s", (user_id,))
    user = cursor.fetchone()
    conn.close()
    if not user:
        session.clear()
        return redirect("/login")
    tasks = get_tasks(user_id)
    insight = generate_insight(tasks)
    return render_template(
        "index.html",
        tasks=tasks,
        insight=insight,
        username=user[0],
        vapid_public_key=VAPID_PUBLIC_KEY  # inject the VAPID key here
    )

# ----------------------------
# AI TASK ADD
# ----------------------------
@app.route("/ai_add", methods=["POST"])
def ai_add():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    text = request.form.get("task")
    user_priority = request.form.get("priority")
    deadline = request.form.get("deadline") or None  # No time column now

    parsed_tasks = parse_tasks(text)
    conn = get_db()
    cursor = conn.cursor()
    new_tasks = []

    for t in parsed_tasks:
        final_priority = user_priority if user_priority else t.get("priority", "Medium")

        cursor.execute("""
            INSERT INTO tasks (task,status,priority,deadline,notified,user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            t["task"],
            "Pending",
            final_priority,
            deadline,
            0,
            session["user_id"]
        ))

        new_tasks.append({
            "task": t["task"],
            "deadline": deadline,
            "overdue": False,
            "user_id": session["user_id"]
        })

    conn.commit()
    conn.close()

    # Notify only if deadline exists
    for nt in new_tasks:
        if nt["deadline"]:
            notify_task(nt)

    # Emit all added tasks once
    socketio.emit(
        "task_added",
        {"tasks": [nt["task"] for nt in new_tasks]},
        room=str(session["user_id"])
    )

    return jsonify({"success": True})

# ----------------------------
# COMPLETE / DELETE / EDIT
# ----------------------------
@app.route("/complete/<int:id>")
def complete(id):
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE tasks 
        SET status='Completed', notified=0 
        WHERE id=%s AND user_id=%s
    """, (id, session["user_id"]))
    conn.commit()
    conn.close()

    socketio.emit("task_completed", {"id": id}, room=str(session["user_id"]))
    return jsonify({"success": True})

@app.route("/delete/<int:id>")
def delete(id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id=%s AND user_id=%s", (id, session["user_id"]))
    conn.commit()
    conn.close()
    socketio.emit("task_deleted", {"id": id}, room=str(session["user_id"]))
    return jsonify({"success": True})

@app.route("/edit/<int:id>", methods=["POST"])
def edit(id):
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401

    user_id = session["user_id"]
    print("🔥 EDIT route hit for user:", user_id)

    task_text = request.form.get("task")
    priority = request.form.get("priority")
    deadline = request.form.get("deadline") or None

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE tasks
            SET task=%s, priority=%s, deadline=%s, status='Pending'
            WHERE id=%s AND user_id=%s
        """, (task_text, priority, deadline, id, user_id))
    except sqlite3.OperationalError as e:
        print("DB lock (edit):", e)
        return jsonify({"error": "Database busy, try again"}), 500

    conn.commit()
    conn.close()

    # 🔥 DEBUG + SAFE EMIT
    room = str(user_id)
    print("📡 Emitting task_updated to room:", room)

    socketio.emit("task_updated", {
    "id": id,
    "task": task_text,
    "status": "Pending"
    }, room=str(user_id))

    return jsonify({"success": True})

# ----------------------------
# API
# ----------------------------
@app.route("/api/tasks")
def api_tasks():
    if not is_logged_in():
        return jsonify({"error": "Not logged in"}), 401
    user_id = session["user_id"]
    tasks = get_tasks(user_id)
    insight = generate_insight(tasks)
    days, weekly = get_weekly_data(tasks)
    reminders = check_reminders(tasks)
    task_list = [{
        "id": t[0], "task": t[1], "status": t[2], "priority": t[3],
        "deadline": t[4], "created_at": t[5]
    } for t in tasks]
    return jsonify({
        "tasks": task_list,
        "total": insight["total"],
        "completed": insight["completed"],
        "pending": insight["pending"],
        "high_priority": insight["high_priority"],
        "score": insight["score"],
        "suggestion": insight["suggestion"],
        "days": days,
        "weekly": weekly,
        "reminders": reminders
    })

@app.errorhandler(Exception)
def handle_exception(e):
    print("❌ Global Error:", e)
    flash("Something went wrong. Please try again.", "danger")
    return redirect("/")

# ----------------------------
# SCHEDULER
# ----------------------------
def check_due_tasks():
    conn = get_db()

    if conn is None:
        print("⚠️ No DB connection. Skipping scheduled task check.")
        return

    cursor = conn.cursor()

    # Only select pending tasks that haven't been notified
    cursor.execute("""
        SELECT id, task, status, deadline, notified, user_id 
        FROM tasks 
        WHERE deadline IS NOT NULL AND status != 'Completed' AND notified = 0
    """)
    tasks = cursor.fetchall()

    now = datetime.now()

    for t in tasks:
        task_id, task_name, status, deadline, notified, user_id = t

        # Skip if no deadline
        if not deadline:
            continue

        try:
            due_dt = datetime.fromisoformat(deadline)
        except:
            try:
                due_dt = datetime.strptime(deadline, "%Y-%m-%d")
            except:
                continue

        # ----------------------------
        # Trigger notification if due
        # ----------------------------
        if now >= due_dt:
            notify_task({
                "task": task_name,
                "overdue": now > due_dt,
                "deadline": now.date() == due_dt.date(),
                "user_id": user_id
            })

            # ----------------------------
            # Mark task as notified
            # ----------------------------
            try:
                cursor.execute("UPDATE tasks SET notified=1 WHERE id=%s", (task_id,))
            except sqlite3.OperationalError as e:
                print("DB lock (scheduler):", e)

            # Emit update to the user's room (only if connected)
            socketio.emit(
                "task_due_update",
                {"id": task_id, "status": status},
                room=str(user_id)
            )

    conn.commit()
    conn.close()

# ----------------------------
# SCHEDULER INIT (Render-safe)
# ----------------------------
scheduler = BackgroundScheduler()

scheduler.add_job(
    check_due_tasks,
    'interval',
    seconds=10,
    max_instances=1,
    coalesce=True
)

if not scheduler.running:
    scheduler.start()

# ----------------------------
# RUN
# ----------------------------
if __name__ == "__main__":
    import socket


    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)

    if os.getenv("FLASK_ENV") == "development":
        print(f"Your app is accessible at http://{local_ip}:5000")

    print("🌍 ENV:", os.getenv("FLASK_ENV"))
    print("🔗 BASE_URL:", os.getenv("BASE_URL"))

    debug_mode = os.getenv("FLASK_ENV") == "development"
    port = int(os.environ.get("PORT", 5000))

    socketio.run(app, host="0.0.0.0", port=port, debug=debug_mode)