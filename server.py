from flask import Flask, jsonify, request, render_template, session, redirect
import os, uuid, hashlib, secrets, time, json
import urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

from db import Db, db_configured

# Always use Philippine Time (UTC+8) regardless of server OS timezone
PH = timezone(timedelta(hours=8))

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'timetrack-local-secret-2024')
app.config['TEMPLATES_AUTO_RELOAD'] = True        # always serve latest template
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.permanent_session_lifetime = timedelta(days=7)   # cookie survives refresh/browser restart

# Log out after this much inactivity (server-enforced)
SESSION_IDLE_SECONDS = int(os.environ.get('SESSION_IDLE_SECONDS', '3600'))
# These endpoints are background polls — they don't count as user activity
PASSIVE_PATHS = {'/api/config', '/api/server-time', '/api/ip-status'}

def start_session(**kwargs):
    """Fresh logged-in session that survives refreshes; keeps the IP bypass flag."""
    keep_bypass = session.get('ip_bypass')
    session.clear()
    session.permanent = True
    if keep_bypass:
        session['ip_bypass'] = True
    session['last_seen'] = int(time.time())
    for k, v in kwargs.items():
        session[k] = v

GOOGLE_CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()

# Google accounts that are always admins (comma-separated in ADMIN_EMAILS env)
ADMIN_EMAILS = {e.strip().lower() for e in
                os.environ.get('ADMIN_EMAILS', 'lpg.villarasa@gmail.com').split(',')
                if e.strip()}

# ── Database ──────────────────────────────────────────────────────────────
def get_db():
    return Db()

SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS employees (
        id               TEXT PRIMARY KEY,
        name             TEXT NOT NULL,
        hourly_rate      REAL NOT NULL DEFAULT 0,
        regular_hours    INTEGER NOT NULL DEFAULT 40,
        allowance        REAL NOT NULL DEFAULT 0,
        allowance_type   TEXT NOT NULL DEFAULT 'weekly',
        password_hash    TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS entries (
        id          TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL,
        clock_in    TEXT NOT NULL,
        clock_out   TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS weekly_adjustments (
        id          TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL,
        week_start  TEXT NOT NULL,
        bonus       REAL NOT NULL DEFAULT 0,
        notes       TEXT NOT NULL DEFAULT '',
        UNIQUE(employee_id, week_start)
    )''',
    '''CREATE TABLE IF NOT EXISTS breaks (
        id          TEXT PRIMARY KEY,
        entry_id    TEXT NOT NULL,
        employee_id TEXT NOT NULL,
        break_start TEXT NOT NULL,
        break_end   TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS payments (
        id          TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL,
        week_label  TEXT NOT NULL,
        week_start  TEXT NOT NULL,
        week_end    TEXT NOT NULL,
        amount      REAL NOT NULL DEFAULT 0,
        status      TEXT NOT NULL DEFAULT 'pending',
        paid_date   TEXT,
        notes       TEXT NOT NULL DEFAULT ''
    )''',
    "INSERT INTO settings VALUES ('pin','1234')    ON CONFLICT(key) DO NOTHING",
    "INSERT INTO settings VALUES ('ot_mult','1.5') ON CONFLICT(key) DO NOTHING",
]

def init_db():
    c = get_db()
    for stmt in SCHEMA:
        c.execute(stmt)
        c.commit()
    # Migrate existing DBs — add columns if missing
    for col, definition in [
        ('password_hash',  'TEXT'),
        ('allowance',      'REAL NOT NULL DEFAULT 0'),
        ('allowance_type', "TEXT NOT NULL DEFAULT 'weekly'"),
        ('daily_hours',     'REAL NOT NULL DEFAULT 8'),
        ('plain_password',  "TEXT NOT NULL DEFAULT '1234'"),
        ('daily_rate',      'REAL NOT NULL DEFAULT 0'),
        ('google_email',    'TEXT'),
        ('birth_year',      'INTEGER'),
        ('role',            "TEXT NOT NULL DEFAULT 'subcontractor'"),
    ]:
        try:
            c.execute(f'ALTER TABLE employees ADD COLUMN {col} {definition}')
            c.commit()
        except Exception:
            c.rollback()
    c.commit()
    c.close()

_db_ready = False
def ensure_db():
    global _db_ready
    if _db_ready or not db_configured():
        return
    init_db()
    _db_ready = True

def uid():
    return uuid.uuid4().hex[:16]

def now_str():
    # Always store as Philippine Time (UTC+8), no tz suffix so SQLite string ops work
    return datetime.now(PH).strftime('%Y-%m-%dT%H:%M:%S')

def ph_ms(dt_str):
    # Parse a stored naive datetime string as PHT and return Unix ms
    return int(datetime.fromisoformat(dt_str).replace(tzinfo=PH).timestamp() * 1000)

def hash_pw(pw):
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()

DEFAULT_PW = '1234'

def check_password(emp, password):
    stored = emp['password_hash']
    if not stored:
        # No password set yet → default is "1234"
        return password == DEFAULT_PW
    return stored == hash_pw(password)

def is_admin():
    return session.get('is_admin') is True

def current_emp_id():
    return session.get('emp_id')

def get_setting(c, key, default=''):
    row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default

def set_setting(c, key, value):
    c.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
              (key, str(value)))
    c.commit()

# ── IP restriction ─────────────────────────────────────────────────────────
def client_ip():
    xf = request.headers.get('X-Forwarded-For', '')
    if xf:
        return xf.split(',')[0].strip()
    return request.headers.get('X-Real-Ip') or request.remote_addr or ''

def allowed_ips():
    ips = []
    if db_configured():
        try:
            c = get_db()
            ips += [i.strip() for i in get_setting(c, 'allowed_ips').split(',') if i.strip()]
            c.close()
        except Exception:
            pass
    ips += [i.strip() for i in os.environ.get('ALLOWED_IPS', '').split(',') if i.strip()]
    return ips

# Google sign-in must stay reachable so the master admin can log in from anywhere
IP_EXEMPT_PATHS = ('/api/ip-status', '/api/ip-unlock', '/auth/google/start', '/auth/google/callback')

@app.before_request
def gate():
    if not db_configured():
        # No database yet → let the setup page render, block data APIs
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Database not configured yet'}), 503
        return None
    ensure_db()
    # Inactivity logout: expire logged-in sessions idle longer than the limit
    if session.get('emp_id') or session.get('is_admin'):
        now  = int(time.time())
        last = session.get('last_seen', now)
        if now - last > SESSION_IDLE_SECONDS:
            keep_bypass = session.get('ip_bypass')
            session.clear()
            if keep_bypass:
                session['ip_bypass'] = True
        elif request.path not in PASSIVE_PATHS:
            session['last_seen'] = now
    if session.get('master'):         # the master admin is never IP-restricted
        return None
    if request.path in IP_EXEMPT_PATHS:
        return None
    ips = allowed_ips()
    if not ips:                       # restriction not enabled
        return None
    if session.get('ip_bypass'):
        return None
    if client_ip() in ips:
        return None
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Access restricted: your network is not allowed'}), 403
    return blocked_page(), 403

@app.route('/api/ip-status')
def ip_status():
    ips = allowed_ips()
    return jsonify({
        'ip': client_ip(),
        'restricted': bool(ips),
        'allowed': (not ips) or session.get('ip_bypass') is True or session.get('master') is True or client_ip() in ips,
        'allowed_ips': ips if is_admin() else [],
    })

@app.route('/api/ip-unlock', methods=['POST'])
def ip_unlock():
    # Emergency door: the admin PIN lets the owner back in when their IP changes
    d = request.json or {}
    c = get_db()
    stored_pin = get_setting(c, 'pin', '1234')
    if d.get('pin', '') != stored_pin:
        c.close()
        time.sleep(0.8)               # slow down PIN guessing
        return jsonify({'error': 'Incorrect PIN'}), 401
    session.permanent = True
    session['ip_bypass'] = True
    if d.get('remember'):
        ips = [i.strip() for i in get_setting(c, 'allowed_ips').split(',') if i.strip()]
        me = client_ip()
        if me and me not in ips:
            ips.append(me)
            set_setting(c, 'allowed_ips', ','.join(ips))
    c.close()
    return jsonify({'ok': True})

def blocked_page():
    google_btn = ''
    if google_enabled():
        google_btn = ('<div style="margin:16px 0;border-top:1px solid #e2e8f0;padding-top:16px">'
                      '<p style="font-size:12px;color:#94a3b8;margin:0 0 10px">Owner? Sign in from anywhere:</p>'
                      '<button style="background:#fff;color:#334155;border:1px solid #cbd5e1" '
                      'onclick="location.href=\'/auth/google/start?mode=login\'">Sign in with Google</button></div>')
    return BLOCKED_PAGE.replace('<!--GOOGLE-->', google_btn)

BLOCKED_PAGE = '''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TrackPH — Access Restricted</title>
<style>body{font-family:system-ui,sans-serif;background:#f8fafc;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px}
.card{background:#fff;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.08);padding:36px 28px;max-width:380px;width:100%;text-align:center}
h1{font-size:20px;margin:12px 0 6px}p{color:#64748b;font-size:14px;line-height:1.5}
input{width:100%;padding:10px 12px;border:1px solid #cbd5e1;border-radius:10px;font-size:16px;text-align:center;letter-spacing:6px;box-sizing:border-box;margin:10px 0}
button{width:100%;padding:11px;border:none;border-radius:10px;background:#2563eb;color:#fff;font-size:14px;font-weight:700;cursor:pointer}
label{display:flex;gap:8px;align-items:center;justify-content:center;font-size:13px;color:#64748b;margin:10px 0}
#err{color:#dc2626;font-size:13px;min-height:18px;margin-top:8px}</style></head><body>
<div class="card"><div style="font-size:40px">🔒</div><h1>Access Restricted</h1>
<p>TrackPH is locked to the office network. Connect to the office Wi‑Fi / internet and reload this page.</p>
<!--GOOGLE-->
<p style="font-size:12px;color:#94a3b8">Admin? Enter your PIN to unlock from this network.</p>
<input id="pin" type="password" inputmode="numeric" maxlength="4" placeholder="••••">
<label><input id="rem" type="checkbox" style="width:auto;letter-spacing:0"> Also allow this IP from now on</label>
<button onclick="go()">Unlock</button><div id="err"></div></div>
<script>async function go(){
 const r=await fetch('/api/ip-unlock',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({pin:document.getElementById('pin').value,remember:document.getElementById('rem').checked})});
 if(r.ok){location.href='/';}else{document.getElementById('err').textContent='Incorrect PIN.';}
}
document.getElementById('pin').addEventListener('keydown',e=>{if(e.key==='Enter')go()});</script>
</body></html>'''

# ── Google OAuth ───────────────────────────────────────────────────────────
def google_enabled():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

def external_base():
    host = request.host
    scheme = 'http' if host.startswith(('localhost', '127.')) else 'https'
    return f'{scheme}://{host}'

@app.route('/auth/google/start')
def google_start():
    if not google_enabled():
        return redirect('/?glogin=error&reason=notconfigured')
    mode = request.args.get('mode', 'login')
    if mode == 'connect' and not (is_admin() or current_emp_id()):
        return redirect('/?glogin=error&reason=session')
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    session['oauth_mode']  = mode
    params = urllib.parse.urlencode({
        'client_id':     GOOGLE_CLIENT_ID,
        'redirect_uri':  external_base() + '/auth/google/callback',
        'response_type': 'code',
        'scope':         'openid email profile',
        'state':         state,
        'prompt':        'select_account',
    })
    return redirect('https://accounts.google.com/o/oauth2/v2/auth?' + params)

def _http_post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

def _http_get_json(url, token):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

@app.route('/auth/google/callback')
def google_callback():
    if request.args.get('error') or not request.args.get('code'):
        return redirect('/?glogin=error&reason=denied')
    if request.args.get('state') != session.get('oauth_state'):
        return redirect('/?glogin=error&reason=state')
    mode = session.pop('oauth_mode', 'login')
    session.pop('oauth_state', None)

    try:
        tok = _http_post_form('https://oauth2.googleapis.com/token', {
            'code':          request.args['code'],
            'client_id':     GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'redirect_uri':  external_base() + '/auth/google/callback',
            'grant_type':    'authorization_code',
        })
        info = _http_get_json('https://openidconnect.googleapis.com/v1/userinfo', tok['access_token'])
    except Exception:
        return redirect('/?glogin=error&reason=google')

    email = (info.get('email') or '').lower().strip()
    if not email or not info.get('email_verified', True):
        return redirect('/?glogin=error&reason=noemail')

    c = get_db()
    if mode == 'connect':
        if is_admin():
            # Admin accounts are fixed via ADMIN_EMAILS — nothing to connect
            c.close()
            return redirect('/?glogin=admin')
        eid = current_emp_id()
        if not eid:
            c.close()
            return redirect('/?glogin=error&reason=session')
        taken = c.execute('SELECT id FROM employees WHERE lower(google_email)=? AND id<>?', (email, eid)).fetchone()
        if taken:
            c.close()
            return redirect('/?glogin=error&reason=taken')
        c.execute('UPDATE employees SET google_email=? WHERE id=?', (email, eid))
        c.commit()
        c.close()
        return redirect('/?glogin=connected')

    # mode == 'login' — admin is strictly limited to ADMIN_EMAILS
    if email in ADMIN_EMAILS:
        c.close()
        start_session(is_admin=True, master=True)   # master admin: no IP restriction
        return redirect('/?glogin=admin')
    emp = c.execute('SELECT * FROM employees WHERE lower(google_email)=?', (email,)).fetchone()
    c.close()
    if not emp:
        # New Google account → start self-onboarding (name + birth year)
        session['pending_google'] = email
        return redirect('/?glogin=new&name=' + urllib.parse.quote(info.get('name', '')))
    start_session(emp_id=emp['id'], is_admin=(emp['role'] == 'admin'))
    return redirect('/?glogin=admin' if session['is_admin'] else '/?glogin=emp')

@app.route('/api/impersonate', methods=['POST'])
def impersonate():
    # Admin "View as subcontractor": adopt an employee identity while staying admin
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    eid = ((request.json or {}).get('employee_id') or '').strip()
    c = get_db()
    emp = c.execute('SELECT id, name, hourly_rate, regular_hours, daily_hours, allowance, allowance_type, google_email, birth_year FROM employees WHERE id=?', (eid,)).fetchone()
    c.close()
    if not emp:
        return jsonify({'error': 'Employee not found'}), 404
    session['emp_id'] = eid
    return jsonify(dict(emp))

@app.route('/api/impersonate', methods=['DELETE'])
def stop_impersonate():
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    session.pop('emp_id', None)
    return jsonify({'ok': True})

@app.route('/api/employees/<eid>/role', methods=['PUT'])
def set_role(eid):
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    role = ((request.json or {}).get('role') or '').strip()
    if role not in ('admin', 'subcontractor'):
        return jsonify({'error': 'Role must be admin or subcontractor'}), 400
    c = get_db()
    c.execute('UPDATE employees SET role=? WHERE id=?', (role, eid))
    c.commit()
    c.close()
    return jsonify({'ok': True})

@app.route('/api/google-signup', methods=['POST'])
def google_signup():
    email = session.get('pending_google')
    if not email:
        return jsonify({'error': 'Please sign in with Google first'}), 401
    d = request.json or {}
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    try:
        birth_year = int(d.get('birth_year') or 0)
    except (TypeError, ValueError):
        birth_year = 0
    this_year = datetime.now(PH).year
    if birth_year < 1920 or birth_year > this_year - 10:
        return jsonify({'error': 'Please enter a valid year of birth'}), 400

    c = get_db()
    if c.execute('SELECT id FROM employees WHERE lower(google_email)=?', (email,)).fetchone():
        c.close()
        return jsonify({'error': 'This Google account is already registered'}), 400
    eid = uid()
    pw  = secrets.token_urlsafe(8)   # Google-only account; admin can reset if needed
    c.execute('''INSERT INTO employees (id, name, hourly_rate, daily_rate, regular_hours, daily_hours,
                                        allowance, allowance_type, password_hash, plain_password,
                                        google_email, birth_year)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
              (eid, name, 0, 0, 40, 8, 0, 'weekly', hash_pw(pw), pw, email, birth_year))
    c.commit()
    emp = c.execute('SELECT id, name, hourly_rate, regular_hours, daily_hours, allowance, allowance_type, google_email, birth_year FROM employees WHERE id=?', (eid,)).fetchone()
    c.close()
    start_session(emp_id=eid, is_admin=False)
    return jsonify(dict(emp)), 201

@app.route('/api/merge-employees', methods=['POST'])
def merge_employees():
    # Move everything from one profile into another, then delete the source.
    # Used to sync a freshly Google-onboarded profile with an existing one.
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    keep_id   = (d.get('keep_id') or '').strip()
    remove_id = (d.get('remove_id') or '').strip()
    if not keep_id or not remove_id or keep_id == remove_id:
        return jsonify({'error': 'Pick two different profiles'}), 400
    c = get_db()
    keep   = c.execute('SELECT * FROM employees WHERE id=?', (keep_id,)).fetchone()
    remove = c.execute('SELECT * FROM employees WHERE id=?', (remove_id,)).fetchone()
    if not keep or not remove:
        c.close()
        return jsonify({'error': 'Profile not found'}), 404
    # Carry over the Google link and birth year when the kept profile lacks them
    if remove['google_email'] and not keep['google_email']:
        c.execute('UPDATE employees SET google_email=? WHERE id=?', (remove['google_email'], keep_id))
    if remove['birth_year'] and not keep['birth_year']:
        c.execute('UPDATE employees SET birth_year=? WHERE id=?', (remove['birth_year'], keep_id))
    # Move time data; drop duplicate weekly adjustments in favor of the kept profile
    c.execute('''DELETE FROM weekly_adjustments WHERE employee_id=? AND week_start IN
                 (SELECT week_start FROM weekly_adjustments WHERE employee_id=?)''',
              (remove_id, keep_id))
    for table in ('entries', 'breaks', 'weekly_adjustments', 'payments'):
        c.execute(f'UPDATE {table} SET employee_id=? WHERE employee_id=?', (keep_id, remove_id))
    c.execute('DELETE FROM employees WHERE id=?', (remove_id,))
    c.commit()
    c.close()
    return jsonify({'ok': True})

# ── App config / session info ──────────────────────────────────────────────
@app.route('/api/config')
def app_config():
    role = 'admin' if is_admin() else ('employee' if current_emp_id() else None)
    return jsonify({
        'google_enabled': google_enabled(),
        'role': role,
        'my_ip': client_ip(),
        'admin_emails': sorted(ADMIN_EMAILS) if is_admin() else [],
    })

# ── Auth ───────────────────────────────────────────────────────────────────
@app.route('/api/employee-login', methods=['POST'])
def employee_login():
    d = request.json or {}
    emp_id   = d.get('employee_id', '').strip()
    password = d.get('password', '')

    if not emp_id or not password:
        return jsonify({'error': 'Employee and password are required'}), 400

    c = get_db()
    emp = c.execute('SELECT * FROM employees WHERE id=?', (emp_id,)).fetchone()
    c.close()

    if not emp:
        return jsonify({'error': 'Employee not found'}), 401
    if not check_password(emp, password):
        return jsonify({'error': 'Incorrect password'}), 401

    start_session(emp_id=emp['id'], is_admin=False)
    return jsonify({'id': emp['id'], 'name': emp['name'], 'regular_hours': emp['regular_hours'], 'daily_hours': emp['daily_hours'], 'allowance': emp['allowance'], 'allowance_type': emp['allowance_type'], 'google_email': emp['google_email']})

@app.route('/api/employee-logout', methods=['POST'])
def employee_logout():
    keep_bypass = session.get('ip_bypass')
    session.clear()
    if keep_bypass:
        session['ip_bypass'] = True
    return jsonify({'ok': True})

@app.route('/api/me')
def get_me():
    eid = current_emp_id()
    if not eid:
        return jsonify({'error': 'Not logged in'}), 401
    c = get_db()
    emp = c.execute('SELECT id, name, hourly_rate, regular_hours, daily_hours, allowance, allowance_type, google_email, birth_year FROM employees WHERE id=?', (eid,)).fetchone()
    c.close()
    if not emp:
        return jsonify({'error': 'Employee not found'}), 404
    return jsonify(dict(emp))

@app.route('/api/change-password', methods=['POST'])
def change_my_password():
    eid = current_emp_id()
    if not eid:
        return jsonify({'error': 'Not logged in'}), 401
    d = request.json or {}
    cur = d.get('current', '')
    nw  = d.get('new', '')
    if not cur or not nw:
        return jsonify({'error': 'Current and new password required'}), 400
    c = get_db()
    emp = c.execute('SELECT * FROM employees WHERE id=?', (eid,)).fetchone()
    if not emp or not check_password(emp, cur):
        c.close()
        return jsonify({'error': 'Current password is incorrect'}), 401
    c.execute('UPDATE employees SET password_hash=?, plain_password=? WHERE id=?', (hash_pw(nw), nw, eid))
    c.commit()
    c.close()
    return jsonify({'ok': True})

# ── Employee names (public — for login dropdown) ───────────────────────────
@app.route('/api/employee-names')
def employee_names():
    c = get_db()
    rows = c.execute('SELECT id, name FROM employees ORDER BY name').fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ── Employees (admin only) ─────────────────────────────────────────────────
@app.route('/api/employees')
def list_employees():
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    c = get_db()
    rows = c.execute('SELECT id, name, hourly_rate, daily_rate, regular_hours, daily_hours, allowance, allowance_type, plain_password, google_email, birth_year, role, (password_hash IS NOT NULL) as has_password FROM employees ORDER BY name').fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/employees', methods=['POST'])
def add_employee():
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    if not d.get('name', '').strip():
        return jsonify({'error': 'Name is required'}), 400
    eid = uid()
    pw  = d.get('password', DEFAULT_PW)
    c = get_db()
    c.execute('''INSERT INTO employees (id, name, hourly_rate, daily_rate, regular_hours, daily_hours, allowance, allowance_type, password_hash, plain_password)
                 VALUES (?,?,?,?,?,?,?,?,?,?)''',
              (eid, d['name'].strip(), float(d.get('hourly_rate', 0)),
               float(d.get('daily_rate', 0)),
               int(d.get('regular_hours', 40)),
               float(d.get('daily_hours', 8)),
               float(d.get('allowance', 0)),
               d.get('allowance_type', 'weekly'),
               hash_pw(pw), pw))
    c.commit()
    row = c.execute('SELECT id, name, hourly_rate, daily_rate, regular_hours, daily_hours, allowance, allowance_type FROM employees WHERE id=?', (eid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

@app.route('/api/employees/<eid>', methods=['PUT'])
def update_employee(eid):
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    if not d.get('name', '').strip():
        return jsonify({'error': 'Name is required'}), 400
    c = get_db()
    c.execute('UPDATE employees SET name=?, hourly_rate=?, daily_rate=?, regular_hours=?, daily_hours=?, allowance=?, allowance_type=? WHERE id=?',
              (d['name'].strip(), float(d.get('hourly_rate', 0)),
               float(d.get('daily_rate', 0)),
               int(d.get('regular_hours', 40)),
               float(d.get('daily_hours', 8)),
               float(d.get('allowance', 0)),
               d.get('allowance_type', 'weekly'),
               eid))
    c.commit()
    row = c.execute('SELECT id, name, hourly_rate, daily_rate, regular_hours, daily_hours, allowance, allowance_type FROM employees WHERE id=?', (eid,)).fetchone()
    c.close()
    return jsonify(dict(row))

@app.route('/api/employees/<eid>/reset-password', methods=['POST'])
def reset_password(eid):
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    new_pw = d.get('password', DEFAULT_PW)
    c = get_db()
    c.execute('UPDATE employees SET password_hash=?, plain_password=? WHERE id=?', (hash_pw(new_pw), new_pw, eid))
    c.commit()
    c.close()
    return jsonify({'ok': True})

@app.route('/api/employees/<eid>', methods=['DELETE'])
def delete_employee(eid):
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    c = get_db()
    c.execute('DELETE FROM entries   WHERE employee_id=?', (eid,))
    c.execute('DELETE FROM employees WHERE id=?',          (eid,))
    c.commit()
    c.close()
    return jsonify({'ok': True})

# ── Clock in / out ─────────────────────────────────────────────────────────
@app.route('/api/clock-in/<emp_id>', methods=['POST'])
def clock_in(emp_id):
    # Employee can only clock in as themselves
    if not is_admin() and current_emp_id() != emp_id:
        return jsonify({'error': 'Unauthorized'}), 403
    c = get_db()
    # Guard against stale sessions (e.g. profile was merged/deleted) — otherwise
    # the entry would be recorded against a profile that no longer exists
    if not c.execute('SELECT 1 AS x FROM employees WHERE id=?', (emp_id,)).fetchone():
        c.close()
        session.clear()
        return jsonify({'error': 'Your account was updated. Please sign in again.'}), 401
    active = c.execute('SELECT id FROM entries WHERE employee_id=? AND clock_out IS NULL', (emp_id,)).fetchone()
    if active:
        c.close()
        return jsonify({'error': 'Already clocked in'}), 400
    eid = uid()
    c.execute('INSERT INTO entries VALUES (?,?,?,NULL)', (eid, emp_id, now_str()))
    c.commit()
    row = c.execute('SELECT * FROM entries WHERE id=?', (eid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

@app.route('/api/clock-out/<emp_id>', methods=['POST'])
def clock_out(emp_id):
    if not is_admin() and current_emp_id() != emp_id:
        return jsonify({'error': 'Unauthorized'}), 403
    c = get_db()
    active = c.execute('SELECT * FROM entries WHERE employee_id=? AND clock_out IS NULL', (emp_id,)).fetchone()
    if not active:
        c.close()
        return jsonify({'error': 'Not clocked in'}), 400
    c.execute('UPDATE entries SET clock_out=? WHERE id=?', (now_str(), active['id']))
    c.commit()
    row = c.execute('SELECT * FROM entries WHERE id=?', (active['id'],)).fetchone()
    c.close()
    return jsonify(dict(row))

# ── Entries ────────────────────────────────────────────────────────────────
@app.route('/api/entries')
def list_entries():
    # Employees can only see their own entries
    if not is_admin():
        eid = current_emp_id()
        if not eid:
            return jsonify({'error': 'Not logged in'}), 401
        # Force filter to their own entries
        emp_filter = eid
    else:
        emp_filter = request.args.get('employee_id', '')

    start = request.args.get('start', '')
    end   = request.args.get('end',   '')
    date  = request.args.get('date',  '')

    c = get_db()
    q, p = 'SELECT * FROM entries WHERE 1=1', []
    if emp_filter:
        q += ' AND employee_id=?'; p.append(emp_filter)
    if date:
        q += ' AND substr(clock_in,1,10)=?';  p.append(date)
    if start:
        q += ' AND substr(clock_in,1,10)>=?'; p.append(start)
    if end:
        q += ' AND substr(clock_in,1,10)<=?'; p.append(end)
    q += ' ORDER BY clock_in DESC'
    rows = c.execute(q, p).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/entries', methods=['POST'])
def add_entry():
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    eid = uid()
    c = get_db()
    c.execute('INSERT INTO entries VALUES (?,?,?,?)',
              (eid, d['employee_id'], d['clock_in'], d.get('clock_out')))
    c.commit()
    row = c.execute('SELECT * FROM entries WHERE id=?', (eid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

@app.route('/api/entries/<eid>', methods=['PUT'])
def update_entry(eid):
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    c = get_db()
    c.execute('UPDATE entries SET employee_id=?, clock_in=?, clock_out=? WHERE id=?',
              (d['employee_id'], d['clock_in'], d.get('clock_out'), eid))
    c.commit()
    row = c.execute('SELECT * FROM entries WHERE id=?', (eid,)).fetchone()
    c.close()
    return jsonify(dict(row))

@app.route('/api/entries/<eid>', methods=['DELETE'])
def delete_entry(eid):
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    c = get_db()
    c.execute('DELETE FROM entries WHERE id=?', (eid,))
    c.commit()
    c.close()
    return jsonify({'ok': True})

# ── Active status ──────────────────────────────────────────────────────────
@app.route('/api/server-time')
def server_time():
    now = datetime.now(PH)
    return jsonify({'now': now.strftime('%Y-%m-%dT%H:%M:%S'), 'ms': int(now.timestamp() * 1000)})

@app.route('/api/active')
def get_active():
    if not is_admin():
        # Employees can check their own active status only
        eid = current_emp_id()
        if not eid:
            return jsonify({'error': 'Not logged in'}), 401
        c = get_db()
        rows = c.execute('''
            SELECT en.id as entry_id, en.employee_id, en.clock_in, e.name
            FROM entries en JOIN employees e ON e.id = en.employee_id
            WHERE en.clock_out IS NULL AND en.employee_id=?
        ''', (eid,)).fetchall()
        c.close()
        result = [dict(r) for r in rows]
        for r in result:
            r['clock_in_ms'] = ph_ms(r['clock_in'])
        return jsonify(result)

    c = get_db()
    rows = c.execute('''
        SELECT en.id as entry_id, en.employee_id, en.clock_in, e.name
        FROM entries en JOIN employees e ON e.id = en.employee_id
        WHERE en.clock_out IS NULL
    ''').fetchall()
    c.close()
    result = [dict(r) for r in rows]
    for r in result:
        r['clock_in_ms'] = ph_ms(r['clock_in'])
    return jsonify(result)

# ── Settings ───────────────────────────────────────────────────────────────
@app.route('/api/settings')
def get_settings():
    c = get_db()
    rows = c.execute('SELECT * FROM settings').fetchall()
    c.close()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['PUT'])
def save_settings():
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    c = get_db()
    for k, v in d.items():
        c.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, str(v)))
    c.commit()
    c.close()
    return jsonify({'ok': True})

# ── Breaks ────────────────────────────────────────────────────────────────
@app.route('/api/break-start/<emp_id>', methods=['POST'])
def break_start(emp_id):
    if not is_admin() and current_emp_id() != emp_id:
        return jsonify({'error': 'Unauthorized'}), 403
    c = get_db()
    if not c.execute('SELECT 1 AS x FROM employees WHERE id=?', (emp_id,)).fetchone():
        c.close()
        session.clear()
        return jsonify({'error': 'Your account was updated. Please sign in again.'}), 401
    active = c.execute(
        'SELECT id FROM entries WHERE employee_id=? AND clock_out IS NULL', (emp_id,)
    ).fetchone()
    if not active:
        c.close()
        return jsonify({'error': 'Not clocked in'}), 400
    on_break = c.execute(
        'SELECT id FROM breaks WHERE employee_id=? AND break_end IS NULL', (emp_id,)
    ).fetchone()
    if on_break:
        c.close()
        return jsonify({'error': 'Already on break'}), 400
    bid = uid()
    c.execute('INSERT INTO breaks VALUES (?,?,?,?,NULL)', (bid, active['id'], emp_id, now_str()))
    c.commit()
    row = c.execute('SELECT * FROM breaks WHERE id=?', (bid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

@app.route('/api/break-end/<emp_id>', methods=['POST'])
def break_end(emp_id):
    if not is_admin() and current_emp_id() != emp_id:
        return jsonify({'error': 'Unauthorized'}), 403
    c = get_db()
    on_break = c.execute(
        'SELECT * FROM breaks WHERE employee_id=? AND break_end IS NULL', (emp_id,)
    ).fetchone()
    if not on_break:
        c.close()
        return jsonify({'error': 'Not on break'}), 400
    c.execute('UPDATE breaks SET break_end=? WHERE id=?', (now_str(), on_break['id']))
    c.commit()
    row = c.execute('SELECT * FROM breaks WHERE id=?', (on_break['id'],)).fetchone()
    c.close()
    return jsonify(dict(row))

@app.route('/api/breaks')
def list_breaks():
    emp_id   = request.args.get('employee_id', '')
    entry_id = request.args.get('entry_id', '')
    # Employees can only see their own breaks
    if not is_admin():
        eid = current_emp_id()
        if not eid:
            return jsonify({'error': 'Not logged in'}), 401
        emp_id = eid
    c = get_db()
    q, p = 'SELECT * FROM breaks WHERE 1=1', []
    if emp_id:   q += ' AND employee_id=?'; p.append(emp_id)
    if entry_id: q += ' AND entry_id=?';    p.append(entry_id)
    q += ' ORDER BY break_start DESC'
    rows = c.execute(q, p).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/active-break/<emp_id>')
def active_break(emp_id):
    if not is_admin() and current_emp_id() != emp_id:
        return jsonify({'error': 'Unauthorized'}), 403
    c = get_db()
    row = c.execute(
        'SELECT * FROM breaks WHERE employee_id=? AND break_end IS NULL', (emp_id,)
    ).fetchone()
    c.close()
    return jsonify(dict(row) if row else {})

# ── Weekly Adjustments (Bonus / Notes) ────────────────────────────────────
@app.route('/api/adjustments')
def get_adjustments():
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    week_start = request.args.get('week_start', '')
    emp_id     = request.args.get('employee_id', '')
    c = get_db()
    q, p = 'SELECT * FROM weekly_adjustments WHERE 1=1', []
    if week_start: q += ' AND week_start=?'; p.append(week_start)
    if emp_id:     q += ' AND employee_id=?'; p.append(emp_id)
    rows = c.execute(q, p).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/adjustments', methods=['POST'])
def save_adjustment():
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d          = request.json or {}
    emp_id     = d.get('employee_id', '').strip()
    week_start = d.get('week_start', '').strip()
    bonus      = float(d.get('bonus', 0))
    notes      = d.get('notes', '').strip()
    if not emp_id or not week_start:
        return jsonify({'error': 'employee_id and week_start required'}), 400
    c = get_db()
    existing = c.execute(
        'SELECT id FROM weekly_adjustments WHERE employee_id=? AND week_start=?',
        (emp_id, week_start)
    ).fetchone()
    if existing:
        c.execute(
            'UPDATE weekly_adjustments SET bonus=?, notes=? WHERE employee_id=? AND week_start=?',
            (bonus, notes, emp_id, week_start)
        )
    else:
        c.execute(
            'INSERT INTO weekly_adjustments VALUES (?,?,?,?,?)',
            (uid(), emp_id, week_start, bonus, notes)
        )
    c.commit()
    row = c.execute(
        'SELECT * FROM weekly_adjustments WHERE employee_id=? AND week_start=?',
        (emp_id, week_start)
    ).fetchone()
    c.close()
    return jsonify(dict(row)), 201

# ── Payments ──────────────────────────────────────────────────────────────
@app.route('/api/payments')
def list_payments():
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    emp_id = request.args.get('employee_id', '')
    month  = request.args.get('month', '')   # e.g. '2026-04'
    c = get_db()
    q, p = 'SELECT * FROM payments WHERE 1=1', []
    if emp_id: q += ' AND employee_id=?'; p.append(emp_id)
    if month:  q += ' AND substr(week_start,1,7)=?'; p.append(month)
    q += ' ORDER BY week_start DESC, employee_id'
    rows = c.execute(q, p).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/payments', methods=['POST'])
def add_payment():
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    pid = uid()
    c = get_db()
    c.execute('''INSERT INTO payments (id,employee_id,week_label,week_start,week_end,amount,status,paid_date,notes)
                 VALUES (?,?,?,?,?,?,?,?,?)''',
              (pid, d['employee_id'], d.get('week_label',''), d['week_start'], d['week_end'],
               float(d.get('amount', 0)), d.get('status','pending'),
               d.get('paid_date') or None, d.get('notes','')))
    c.commit()
    row = c.execute('SELECT * FROM payments WHERE id=?', (pid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201

@app.route('/api/payments/<pid>', methods=['PUT'])
def update_payment(pid):
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    d = request.json or {}
    c = get_db()
    c.execute('''UPDATE payments SET employee_id=?,week_label=?,week_start=?,week_end=?,
                 amount=?,status=?,paid_date=?,notes=? WHERE id=?''',
              (d['employee_id'], d.get('week_label',''), d['week_start'], d['week_end'],
               float(d.get('amount', 0)), d.get('status','pending'),
               d.get('paid_date') or None, d.get('notes',''), pid))
    c.commit()
    row = c.execute('SELECT * FROM payments WHERE id=?', (pid,)).fetchone()
    c.close()
    return jsonify(dict(row))

@app.route('/api/payments/<pid>', methods=['DELETE'])
def delete_payment(pid):
    if not is_admin():
        return jsonify({'error': 'Admin only'}), 403
    c = get_db()
    c.execute('DELETE FROM payments WHERE id=?', (pid,))
    c.commit()
    c.close()
    return jsonify({'ok': True})

# ── Admin session ──────────────────────────────────────────────────────────
@app.route('/api/admin-login', methods=['POST'])
def admin_login():
    if google_enabled():
        # Admin access is locked to the Google accounts in ADMIN_EMAILS
        return jsonify({'error': 'PIN login is disabled — use “Sign in with Google”'}), 403
    d = request.json or {}
    pin = d.get('pin', '')
    c = get_db()
    row = c.execute("SELECT value FROM settings WHERE key='pin'").fetchone()
    c.close()
    stored_pin = row['value'] if row else '1234'
    if pin != stored_pin:
        return jsonify({'error': 'Incorrect PIN'}), 401
    start_session(is_admin=True)
    return jsonify({'ok': True})

@app.route('/api/admin-logout', methods=['POST'])
def admin_logout():
    keep_bypass = session.get('ip_bypass')
    session.clear()
    if keep_bypass:
        session['ip_bypass'] = True
    return jsonify({'ok': True})

# ── Serve frontend ─────────────────────────────────────────────────────────
SETUP_PAGE = '''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TrackPH — Setup</title>
<style>body{font-family:system-ui,sans-serif;background:#f8fafc;margin:0;padding:40px 20px;color:#0f172a}
.card{background:#fff;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.08);padding:32px;max-width:640px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}p{color:#64748b;font-size:14px;line-height:1.6}
ol{padding-left:20px}li{margin:10px 0;font-size:14px;line-height:1.6}
code{background:#f1f5f9;padding:2px 6px;border-radius:6px;font-size:13px}</style></head><body>
<div class="card"><h1>⏱ TrackPH — almost ready!</h1>
<p>The app is deployed, but it still needs a database. One-time setup:</p>
<ol>
<li>In your <b>Vercel dashboard</b> open this project → <b>Storage</b> tab → <b>Create Database</b> → choose <b>Neon (Postgres)</b> (free plan) → Connect. This automatically adds the <code>DATABASE_URL</code> environment variable.</li>
<li>(Optional, for Google sign-in) add env vars <code>GOOGLE_CLIENT_ID</code> and <code>GOOGLE_CLIENT_SECRET</code> from Google Cloud Console.</li>
<li>Go to <b>Deployments</b> → ⋯ menu on the latest deployment → <b>Redeploy</b>.</li>
</ol>
<p>After redeploying, reload this page — the app will create its tables automatically.</p></div></body></html>'''

@app.route('/')
def index():
    if not db_configured():
        return SETUP_PAGE
    return render_template('index.html')

# ── Start ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()

    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = '127.0.0.1'

    print()
    print('=' * 52)
    print('   TimeTrack Server is RUNNING')
    print('=' * 52)
    print(f'   This computer :  http://localhost:5000')
    print(f'   Other computers: http://{local_ip}:5000')
    print('=' * 52)
    print('   Keep this window open while using the app.')
    print('   Press Ctrl+C to stop.')
    print('=' * 52)
    print()

    app.run(host='0.0.0.0', port=5000, debug=False)
