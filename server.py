from flask import Flask, jsonify, request, render_template, session
import sqlite3, os, uuid, hashlib
from datetime import datetime, timezone, timedelta

# Always use Philippine Time (UTC+8) regardless of server OS timezone
PH = timezone(timedelta(hours=8))

app = Flask(__name__)
app.secret_key = 'timetrack-local-secret-2024'   # used for session cookies
app.config['TEMPLATES_AUTO_RELOAD'] = True        # always serve latest template

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'timetracker.db')

# ── Database ──────────────────────────────────────────────────────────────
def get_db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = get_db()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS employees (
            id               TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            hourly_rate      REAL NOT NULL DEFAULT 0,
            regular_hours    INTEGER NOT NULL DEFAULT 40,
            allowance        REAL NOT NULL DEFAULT 0,
            allowance_type   TEXT NOT NULL DEFAULT 'weekly',
            password_hash    TEXT
        );
        CREATE TABLE IF NOT EXISTS entries (
            id          TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL,
            clock_in    TEXT NOT NULL,
            clock_out   TEXT,
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS weekly_adjustments (
            id          TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL,
            week_start  TEXT NOT NULL,
            bonus       REAL NOT NULL DEFAULT 0,
            notes       TEXT NOT NULL DEFAULT '',
            UNIQUE(employee_id, week_start),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );
        CREATE TABLE IF NOT EXISTS breaks (
            id          TEXT PRIMARY KEY,
            entry_id    TEXT NOT NULL,
            employee_id TEXT NOT NULL,
            break_start TEXT NOT NULL,
            break_end   TEXT,
            FOREIGN KEY (entry_id)    REFERENCES entries(id),
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );
        CREATE TABLE IF NOT EXISTS payments (
            id          TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL,
            week_label  TEXT NOT NULL,
            week_start  TEXT NOT NULL,
            week_end    TEXT NOT NULL,
            amount      REAL NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT 'pending',
            paid_date   TEXT,
            notes       TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );
        INSERT OR IGNORE INTO settings VALUES ('pin',    '1234');
        INSERT OR IGNORE INTO settings VALUES ('ot_mult','1.5');
    ''')
    # Migrate existing DBs — add columns if missing
    for col, definition in [
        ('password_hash',  'TEXT'),
        ('allowance',      'REAL NOT NULL DEFAULT 0'),
        ('allowance_type', "TEXT NOT NULL DEFAULT 'weekly'"),
        ('daily_hours',     'REAL NOT NULL DEFAULT 8'),
        ('plain_password',  "TEXT NOT NULL DEFAULT '1234'"),
        ('daily_rate',      'REAL NOT NULL DEFAULT 0'),
    ]:
        try:
            c.execute(f'ALTER TABLE employees ADD COLUMN {col} {definition}')
            c.commit()
        except Exception:
            pass
    c.commit()
    c.close()

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

    session.clear()
    session['emp_id']   = emp['id']
    session['is_admin'] = False
    return jsonify({'id': emp['id'], 'name': emp['name'], 'regular_hours': emp['regular_hours'], 'daily_hours': emp['daily_hours'], 'allowance': emp['allowance'], 'allowance_type': emp['allowance_type']})

@app.route('/api/employee-logout', methods=['POST'])
def employee_logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/me')
def get_me():
    eid = current_emp_id()
    if not eid:
        return jsonify({'error': 'Not logged in'}), 401
    c = get_db()
    emp = c.execute('SELECT id, name, hourly_rate, regular_hours, daily_hours, allowance FROM employees WHERE id=?', (eid,)).fetchone()
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
    rows = c.execute('SELECT id, name, hourly_rate, daily_rate, regular_hours, daily_hours, allowance, allowance_type, plain_password, (password_hash IS NOT NULL) as has_password FROM employees ORDER BY name').fetchall()
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
        c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (k, str(v)))
    c.commit()
    c.close()
    return jsonify({'ok': True})

# ── Breaks ────────────────────────────────────────────────────────────────
@app.route('/api/break-start/<emp_id>', methods=['POST'])
def break_start(emp_id):
    if not is_admin() and current_emp_id() != emp_id:
        return jsonify({'error': 'Unauthorized'}), 403
    c = get_db()
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
    d = request.json or {}
    pin = d.get('pin', '')
    c = get_db()
    row = c.execute("SELECT value FROM settings WHERE key='pin'").fetchone()
    c.close()
    stored_pin = row['value'] if row else '1234'
    if pin != stored_pin:
        return jsonify({'error': 'Incorrect PIN'}), 401
    session.clear()
    session['is_admin'] = True
    return jsonify({'ok': True})

@app.route('/api/admin-logout', methods=['POST'])
def admin_logout():
    session.clear()
    return jsonify({'ok': True})

# ── Serve frontend ─────────────────────────────────────────────────────────
@app.route('/')
def index():
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
