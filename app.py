"""销售周报 / 客户拜访 / 出差报告 管理系统

运行:  python app.py            (开发)
       python serve.py          (生产, waitress)
"""
import calendar
import io
import json
import os
import secrets
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SALES_DB", os.path.join(BASE_DIR, "data", "sales.db"))

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or "dev-only-change-me",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",  # 启用 HTTPS 后设为 1
)

ROLES = {"sales": "销售员", "manager": "销售主管", "admin": "系统管理员"}
VISIT_TYPES = ["首次拜访", "回访", "技术交流", "商务谈判", "售后服务", "催款", "其他"]
STAGES = ["", "初步接触", "需求确认", "方案/报价", "商务谈判", "签约", "已丢单"]
STATUS = {
    "draft": ("草稿", "gray"),
    "generated": ("已生成", "blue"),
    "submitted": ("待审批", "orange"),
    "approved": ("已通过", "green"),
    "rejected": ("已退回", "red"),
    "active": ("正常", "green"),
    "pending": ("待审核", "orange"),
    "disabled": ("停用", "gray"),
}
TARGETS = {"trip": ("trips", "出差报告"), "weekly": ("weekly_reports", "周报"),
           "monthly": ("monthly_reports", "月度报告")}


# ---------------------------------------------------------------- 数据库

def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = connect()
    with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    if not conn.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        conn.execute(
            "INSERT INTO users (username, password_hash, name, role) VALUES (?,?,?,?)",
            ("admin", generate_password_hash("admin123"), "系统管理员", "admin"))
        print("已创建默认管理员 admin / admin123 (首次登录需修改密码)")
    conn.commit()
    conn.close()


def q(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def audit(action, detail=""):
    uid = g.user["id"] if g.get("user") else None
    execute("INSERT INTO audit_log (user_id, action, detail, ip) VALUES (?,?,?,?)",
            (uid, action, detail, request.remote_addr or ""))


# ---------------------------------------------------------------- 权限

@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        user = q("SELECT * FROM users WHERE id=? AND active=1", (uid,), one=True)
        if user:
            g.user = user
        else:
            session.clear()
    if request.method == "POST":
        token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not token or token != session.get("_csrf"):
            abort(400, "表单已过期, 请刷新页面后重试")
    if g.user and g.user["must_change_pw"] and request.endpoint not in (
            "change_password", "logout", "static"):
        flash("首次登录或密码被重置, 请先修改密码", "warn")
        return redirect(url_for("change_password"))


@app.context_processor
def inject():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    pending = 0
    if g.get("user") and g.user["role"] in ("manager", "admin"):
        pending = count_pending_approvals()
    return dict(csrf=session["_csrf"], ROLES=ROLES, STATUS=STATUS, me=g.get("user"),
                pending_count=pending, today=date.today().isoformat())


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def roles_required(*roles):
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(*a, **kw):
            if g.user["role"] not in roles:
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


def is_manager():
    return g.user["role"] in ("manager", "admin")


def visible_user_ids():
    """当前用户可查看的销售员 id 列表; None 表示全部 (管理员)"""
    u = g.user
    if u["role"] == "admin":
        return None
    if u["role"] == "manager":
        ids = [r["id"] for r in q("SELECT id FROM users WHERE manager_id=?", (u["id"],))]
        return ids + [u["id"]]
    return [u["id"]]


def scope(col):
    ids = visible_user_ids()
    if ids is None:
        return "", []
    return f" AND {col} IN ({','.join('?' * len(ids))})", ids


def visible_users():
    sql, args = scope("id")
    return q(f"SELECT * FROM users WHERE active=1 AND role IN ('sales','manager') {sql} "
             f"ORDER BY role DESC, name", args)


def check_owner(row):
    if row is None:
        abort(404)
    ids = visible_user_ids()
    if ids is not None and row["user_id"] not in ids:
        abort(403)


def can_approve(owner_id):
    """审批人: 该销售员的直属主管, 或管理员; 不能审批自己"""
    if owner_id == g.user["id"]:
        return g.user["role"] == "admin"
    if g.user["role"] == "admin":
        return True
    if g.user["role"] == "manager":
        owner = q("SELECT manager_id FROM users WHERE id=?", (owner_id,), one=True)
        return owner is not None and owner["manager_id"] == g.user["id"]
    return False


def count_pending_approvals():
    total = 0
    for table, _ in TARGETS.values():
        sql, args = scope("t.user_id")
        total += q(f"SELECT COUNT(*) c FROM {table} t WHERE status='submitted' "
                   f"AND t.user_id != ? {sql}", [g.user["id"]] + args, one=True)["c"]
    return total


# ---------------------------------------------------------------- 工具函数

def f_str(name, default=""):
    return (request.form.get(name) or default).strip()


def f_num(name):
    try:
        return round(float(request.form.get(name) or 0), 2)
    except ValueError:
        return 0.0


def parse_date(s, default=None):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def month_range(month):
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def prev_month(month):
    first, _ = month_range(month)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def week_monday(d):
    return d - timedelta(days=d.weekday())


def approvals_for(target_type, target_id):
    return q("SELECT a.*, u.name approver FROM approvals a JOIN users u ON u.id=a.approver_id "
             "WHERE target_type=? AND target_id=? ORDER BY a.id", (target_type, target_id))


def active_customers():
    return q("SELECT c.*, u.name owner_name FROM customers c LEFT JOIN users u ON u.id=c.owner_id "
             "WHERE c.status='active' ORDER BY c.level, c.name")


@app.template_filter("money")
def money(v):
    return f"{(v or 0):,.2f}"


@app.template_filter("nl2br")
def nl2br(s):
    from markupsafe import Markup, escape
    return Markup("<br>".join(escape(s or "").split("\n")))


# ---------------------------------------------------------------- 登录

_login_fail = defaultdict(list)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        now = time.time()
        _login_fail[ip] = [t for t in _login_fail[ip] if now - t < 600]
        if len(_login_fail[ip]) >= 5:
            flash("登录失败次数过多, 请 10 分钟后再试", "error")
            return render_template("login.html")
        user = q("SELECT * FROM users WHERE username=? AND active=1",
                 (f_str("username"),), one=True)
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            _login_fail.pop(ip, None)
            session.clear()
            session.permanent = True
            session["uid"] = user["id"]
            g.user = user
            audit("登录")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        _login_fail[ip].append(now)
        flash("用户名或密码错误", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old, new, new2 = (request.form.get(k, "") for k in ("old", "new", "new2"))
        if not check_password_hash(g.user["password_hash"], old):
            flash("原密码错误", "error")
        elif len(new) < 8 or new.isdigit() or new.isalpha():
            flash("新密码至少 8 位, 且需同时包含字母和数字", "error")
        elif new != new2:
            flash("两次输入的新密码不一致", "error")
        else:
            execute("UPDATE users SET password_hash=?, must_change_pw=0 WHERE id=?",
                    (generate_password_hash(new), g.user["id"]))
            audit("修改密码")
            flash("密码已修改", "ok")
            return redirect(url_for("index"))
    return render_template("password.html")


# ---------------------------------------------------------------- 首页

@app.route("/")
@login_required
def index():
    today = date.today()
    month = today.strftime("%Y-%m")
    first, last = month_range(month)
    sql, args = scope("v.user_id")
    month_visits = q(f"SELECT COUNT(*) c, COUNT(DISTINCT customer_id) cc FROM visits v "
                     f"WHERE visit_date BETWEEN ? AND ? {sql}",
                     [first.isoformat(), last.isoformat()] + args, one=True)
    tsql, targs = scope("t.user_id")
    month_trips = q(f"SELECT COUNT(*) c, COALESCE(SUM(cost_transport+cost_hotel+cost_meal+cost_other),0) cost "
                    f"FROM trips t WHERE start_date <= ? AND end_date >= ? {tsql}",
                    [last.isoformat(), first.isoformat()] + targs, one=True)
    recent = q(f"SELECT v.*, c.name customer, u.name sales FROM visits v "
               f"JOIN customers c ON c.id=v.customer_id JOIN users u ON u.id=v.user_id "
               f"WHERE 1=1 {sql} ORDER BY visit_date DESC, v.id DESC LIMIT 8", args)
    # 待跟进: 下次跟进日期在 7 天内
    followups = q(f"SELECT v.*, c.name customer, u.name sales FROM visits v "
                  f"JOIN customers c ON c.id=v.customer_id JOIN users u ON u.id=v.user_id "
                  f"WHERE next_date != '' AND next_date BETWEEN ? AND ? {sql} ORDER BY next_date LIMIT 10",
                  [(today - timedelta(days=3)).isoformat(), (today + timedelta(days=7)).isoformat()] + args)
    this_week = week_monday(today).isoformat()
    has_weekly = q("SELECT id, status FROM weekly_reports WHERE user_id=? AND week_start=?",
                   (g.user["id"], this_week), one=True)
    rejected = q(f"SELECT 'trip' t, id, start_date d, destination title FROM trips WHERE user_id=? AND status='rejected' "
                 f"UNION ALL SELECT 'weekly', id, week_start, '周报' FROM weekly_reports WHERE user_id=? AND status='rejected' "
                 f"UNION ALL SELECT 'monthly', id, month, '月度报告' FROM monthly_reports WHERE user_id=? AND status='rejected'",
                 (g.user["id"],) * 3)
    links = {"trip": ("出差报告", "trip_detail", "tid"), "weekly": ("周报", "weekly_detail", "wid"),
             "monthly": ("月度报告", "monthly_detail", "mid")}
    rejected = [{"label": links[r["t"]][0], "d": r["d"], "title": r["title"],
                 "url": url_for(links[r["t"]][1], **{links[r["t"]][2]: r["id"]})} for r in rejected]
    return render_template("index.html", month=month, mv=month_visits, mt=month_trips,
                           recent=recent, followups=followups, has_weekly=has_weekly,
                           this_week=this_week, rejected=rejected)


# ---------------------------------------------------------------- 客户拜访

def visit_filters(a=None):
    a = request.args if a is None else a
    where, args = ["1=1"], []
    if a.get("start"):
        where.append("v.visit_date >= ?"); args.append(a["start"])
    if a.get("end"):
        where.append("v.visit_date <= ?"); args.append(a["end"])
    if a.get("user_id"):
        where.append("v.user_id = ?"); args.append(a["user_id"])
    if a.get("customer_id"):
        where.append("v.customer_id = ?"); args.append(a["customer_id"])
    if a.get("visit_type"):
        where.append("v.visit_type = ?"); args.append(a["visit_type"])
    if a.get("kw"):
        where.append("(v.content LIKE ? OR v.contact_name LIKE ? OR v.attendees LIKE ? OR c.name LIKE ?)")
        args += [f"%{a['kw']}%"] * 4
    sql, sargs = scope("v.user_id")
    return " AND ".join(where) + sql, args + sargs


VISIT_SELECT = ("SELECT v.*, c.name customer, c.level, u.name sales, t.destination trip_dest "
                "FROM visits v JOIN customers c ON c.id=v.customer_id JOIN users u ON u.id=v.user_id "
                "LEFT JOIN trips t ON t.id=v.trip_id ")


@app.route("/visits")
@login_required
def visits():
    where, args = visit_filters()
    rows = q(f"{VISIT_SELECT} WHERE {where} ORDER BY v.visit_date DESC, v.id DESC LIMIT 500", args)
    return render_template("visits.html", rows=rows, users=visible_users(),
                           customers=active_customers(), types=VISIT_TYPES, a=request.args)


def visit_editable(v):
    if v["user_id"] != g.user["id"]:
        return False
    if v["trip_id"]:
        t = q("SELECT status FROM trips WHERE id=?", (v["trip_id"],), one=True)
        if t and t["status"] in ("submitted", "approved"):
            return False
    return True


def my_open_trips(include_id=None):
    return q("SELECT * FROM trips WHERE user_id=? AND (status IN ('draft','rejected') OR id=?) "
             "ORDER BY start_date DESC LIMIT 30", (g.user["id"], include_id or 0))


@app.route("/visits/new", methods=["GET", "POST"])
@app.route("/visits/<int:vid>/edit", methods=["GET", "POST"])
@login_required
def visit_form(vid=None):
    v = None
    if vid:
        v = q("SELECT * FROM visits WHERE id=?", (vid,), one=True)
        check_owner(v)
        if not visit_editable(v):
            flash("该记录已随出差报告提交, 不能修改", "error")
            return redirect(url_for("visit_detail", vid=vid))
    if request.method == "POST":
        cid = request.form.get("customer_id", type=int)
        cust = q("SELECT * FROM customers WHERE id=? AND status='active'", (cid,), one=True)
        vdate = parse_date(f_str("visit_date"))
        errors = []
        if not cust:
            errors.append("请从客户名单中选择客户")
        if not vdate:
            errors.append("请填写拜访日期")
        elif vdate > date.today():
            errors.append("拜访日期不能晚于今天")
        if not f_str("contact_name"):
            errors.append("请填写洽谈人员")
        if len(f_str("content")) < 10:
            errors.append("洽谈内容至少 10 个字")
        trip_id = request.form.get("trip_id", type=int)
        if trip_id:
            trip = q("SELECT * FROM trips WHERE id=? AND user_id=?", (trip_id, g.user["id"]), one=True)
            if not trip:
                trip_id = None
            elif vdate and not (trip["start_date"] <= vdate.isoformat() <= trip["end_date"]):
                errors.append(f"拜访日期不在所选出差行程 ({trip['start_date']} ~ {trip['end_date']}) 内")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            data = (cid, trip_id, vdate.isoformat(), f_str("start_time"), f_str("end_time"),
                    f_str("visit_type", "回访"), f_str("contact_name"), f_str("contact_title"),
                    f_str("contact_phone"), f_str("attendees"), f_str("content"), f_str("result"),
                    f_str("stage"), f_num("amount"), f_str("next_step"), f_str("next_date"))
            cols = ("customer_id, trip_id, visit_date, start_time, end_time, visit_type, contact_name, "
                    "contact_title, contact_phone, attendees, content, result, stage, amount, next_step, next_date")
            if v:
                sets = ", ".join(f"{c.strip()}=?" for c in cols.split(","))
                execute(f"UPDATE visits SET {sets} WHERE id=?", data + (vid,))
                audit("修改拜访", f"#{vid}")
            else:
                vid = execute(f"INSERT INTO visits (user_id, {cols}) VALUES (?{',?' * len(data)})",
                              (g.user["id"],) + data)
                audit("新增拜访", f"#{vid} {cust['name']}")
            # 自动沉淀客户联系人
            execute("INSERT INTO contacts (customer_id, name, title, phone) VALUES (?,?,?,?) "
                    "ON CONFLICT(customer_id, name) DO UPDATE SET "
                    "title=COALESCE(NULLIF(excluded.title,''), title), "
                    "phone=COALESCE(NULLIF(excluded.phone,''), phone)",
                    (cid, f_str("contact_name"), f_str("contact_title"), f_str("contact_phone")))
            flash("拜访记录已保存", "ok")
            if request.form.get("again"):
                return redirect(url_for("visit_form", trip_id=trip_id or ""))
            return redirect(url_for("visit_detail", vid=vid))
    form = request.form if request.method == "POST" else (v or {})
    return render_template("visit_form.html", v=v, form=form, customers=active_customers(),
                           trips=my_open_trips(v["trip_id"] if v else None), types=VISIT_TYPES,
                           stages=STAGES, preset_trip=request.args.get("trip_id", type=int))


@app.route("/visits/<int:vid>")
@login_required
def visit_detail(vid):
    v = q(f"{VISIT_SELECT} WHERE v.id=?", (vid,), one=True)
    check_owner(v)
    return render_template("visit_detail.html", v=v, editable=visit_editable(v))


@app.route("/visits/<int:vid>/delete", methods=["POST"])
@login_required
def visit_delete(vid):
    v = q("SELECT * FROM visits WHERE id=?", (vid,), one=True)
    check_owner(v)
    if not visit_editable(v):
        abort(403)
    execute("DELETE FROM visits WHERE id=?", (vid,))
    audit("删除拜访", f"#{vid}")
    flash("已删除", "ok")
    return redirect(url_for("visits"))


@app.route("/api/contacts")
@login_required
def api_contacts():
    cid = request.args.get("customer_id", type=int)
    rows = q("SELECT name, title, phone FROM contacts WHERE customer_id=? ORDER BY name", (cid,))
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------- 出差报告

def trip_cost(t):
    return t["cost_transport"] + t["cost_hotel"] + t["cost_meal"] + t["cost_other"]


app.jinja_env.globals["trip_cost"] = trip_cost


@app.route("/trips")
@login_required
def trips():
    sql, args = scope("t.user_id")
    where, wargs = "", []
    if request.args.get("month"):
        first, last = month_range(request.args["month"])
        where = " AND start_date <= ? AND end_date >= ?"
        wargs = [last.isoformat(), first.isoformat()]
    if request.args.get("user_id"):
        where += " AND t.user_id=?"
        wargs.append(request.args["user_id"])
    if request.args.get("status"):
        where += " AND t.status=?"
        wargs.append(request.args["status"])
    rows = q(f"SELECT t.*, u.name sales, (SELECT COUNT(*) FROM visits v WHERE v.trip_id=t.id) nvisits, "
             f"CAST(julianday(end_date) - julianday(start_date) + 1 AS INTEGER) days "
             f"FROM trips t JOIN users u ON u.id=t.user_id WHERE 1=1 {sql}{where} "
             f"ORDER BY start_date DESC LIMIT 300", args + wargs)
    return render_template("trips.html", rows=rows, users=visible_users(), a=request.args)


@app.route("/trips/new", methods=["GET", "POST"])
@app.route("/trips/<int:tid>/edit", methods=["GET", "POST"])
@login_required
def trip_form(tid=None):
    t = None
    if tid:
        t = q("SELECT * FROM trips WHERE id=?", (tid,), one=True)
        check_owner(t)
        if t["user_id"] != g.user["id"] or t["status"] not in ("draft", "rejected"):
            flash("已提交或已审批的出差报告不能修改", "error")
            return redirect(url_for("trip_detail", tid=tid))
    if request.method == "POST":
        sd, ed = parse_date(f_str("start_date")), parse_date(f_str("end_date"))
        errors = []
        if not sd or not ed:
            errors.append("请填写出差起止日期")
        elif ed < sd:
            errors.append("结束日期不能早于开始日期")
        if not f_str("destination"):
            errors.append("请填写出差地点")
        if sd and ed and not errors:
            overlap = q("SELECT id FROM trips WHERE user_id=? AND id!=? AND start_date<=? AND end_date>=?",
                        (g.user["id"], tid or 0, ed.isoformat(), sd.isoformat()), one=True)
            if overlap:
                errors.append("与已有出差行程时间重叠")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            submit = request.form.get("action") == "submit"
            data = (sd.isoformat(), ed.isoformat(), f_str("destination"), f_str("purpose"),
                    f_str("transport"), f_num("cost_transport"), f_num("cost_hotel"),
                    f_num("cost_meal"), f_num("cost_other"), f_str("summary"),
                    "submitted" if submit else "draft")
            if t:
                execute("UPDATE trips SET start_date=?, end_date=?, destination=?, purpose=?, transport=?, "
                        "cost_transport=?, cost_hotel=?, cost_meal=?, cost_other=?, summary=?, status=?, "
                        "updated_at=datetime('now','localtime') WHERE id=?", data + (tid,))
            else:
                tid = execute("INSERT INTO trips (user_id, start_date, end_date, destination, purpose, transport, "
                              "cost_transport, cost_hotel, cost_meal, cost_other, summary, status) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (g.user["id"],) + data)
            audit("提交出差报告" if submit else "保存出差报告", f"#{tid}")
            flash("出差报告已提交, 等待主管审批" if submit else "已保存草稿, 可继续添加拜访记录", "ok")
            return redirect(url_for("trip_detail", tid=tid))
    form = request.form if request.method == "POST" else (t or {})
    return render_template("trip_form.html", t=t, form=form)


@app.route("/trips/<int:tid>")
@login_required
def trip_detail(tid):
    t = q("SELECT t.*, u.name sales FROM trips t JOIN users u ON u.id=t.user_id WHERE t.id=?", (tid,), one=True)
    check_owner(t)
    vs = q(f"{VISIT_SELECT} WHERE v.trip_id=? ORDER BY visit_date, start_time", (tid,))
    return render_template("trip_detail.html", t=t, visits=vs, approvals=approvals_for("trip", tid),
                           can_approve=can_approve(t["user_id"]) and t["status"] == "submitted",
                           can_comment=can_approve(t["user_id"]),
                           mine=t["user_id"] == g.user["id"])


@app.route("/trips/<int:tid>/submit", methods=["POST"])
@login_required
def trip_submit(tid):
    t = q("SELECT * FROM trips WHERE id=? AND user_id=?", (tid, g.user["id"]), one=True)
    if not t or t["status"] not in ("draft", "rejected"):
        abort(403)
    execute("UPDATE trips SET status='submitted', updated_at=datetime('now','localtime') WHERE id=?", (tid,))
    audit("提交出差报告", f"#{tid}")
    flash("已提交, 等待主管审批", "ok")
    return redirect(url_for("trip_detail", tid=tid))


@app.route("/trips/<int:tid>/delete", methods=["POST"])
@login_required
def trip_delete(tid):
    t = q("SELECT * FROM trips WHERE id=? AND user_id=?", (tid, g.user["id"]), one=True)
    if not t or t["status"] not in ("draft", "rejected"):
        abort(403)
    execute("UPDATE visits SET trip_id=NULL WHERE trip_id=?", (tid,))
    execute("DELETE FROM trips WHERE id=?", (tid,))
    audit("删除出差报告", f"#{tid}")
    flash("出差报告已删除 (其中的拜访记录已保留)", "ok")
    return redirect(url_for("trips"))


# ---------------------------------------------------------------- 周报

@app.route("/weekly")
@login_required
def weekly():
    sql, args = scope("w.user_id")
    extra, eargs = "", []
    if request.args.get("user_id"):
        extra = " AND w.user_id=?"
        eargs = [request.args["user_id"]]
    rows = q(f"SELECT w.*, u.name sales FROM weekly_reports w JOIN users u ON u.id=w.user_id "
             f"WHERE 1=1 {sql}{extra} ORDER BY week_start DESC, u.name LIMIT 300", args + eargs)
    return render_template("weekly.html", rows=rows, users=visible_users(), a=request.args,
                           timedelta=timedelta, parse_date=parse_date)


def week_visits(user_id, monday):
    return q(f"{VISIT_SELECT} WHERE v.user_id=? AND v.visit_date BETWEEN ? AND ? ORDER BY visit_date",
             (user_id, monday.isoformat(), (monday + timedelta(days=6)).isoformat()))


@app.route("/weekly/new", methods=["GET", "POST"])
@app.route("/weekly/<int:wid>/edit", methods=["GET", "POST"])
@login_required
def weekly_form(wid=None):
    w = None
    if wid:
        w = q("SELECT * FROM weekly_reports WHERE id=?", (wid,), one=True)
        check_owner(w)
        if w["user_id"] != g.user["id"] or w["status"] not in ("draft", "rejected"):
            flash("已提交或已审批的周报不能修改", "error")
            return redirect(url_for("weekly_detail", wid=wid))
    monday = week_monday(parse_date(request.values.get("week") or (w["week_start"] if w else ""),
                                    date.today()))
    if request.method == "POST":
        if len(f_str("summary")) < 10:
            flash("本周工作总结至少 10 个字", "error")
        else:
            dup = q("SELECT id FROM weekly_reports WHERE user_id=? AND week_start=? AND id!=?",
                    (g.user["id"], monday.isoformat(), wid or 0), one=True)
            if dup:
                flash("该周的周报已存在, 已为你打开", "warn")
                return redirect(url_for("weekly_detail", wid=dup["id"]))
            status = "submitted" if request.form.get("action") == "submit" else "draft"
            data = (monday.isoformat(), f_str("summary"), f_str("problems"), f_str("next_plan"), status)
            if w:
                execute("UPDATE weekly_reports SET week_start=?, summary=?, problems=?, next_plan=?, status=?, "
                        "updated_at=datetime('now','localtime') WHERE id=?", data + (wid,))
            else:
                wid = execute("INSERT INTO weekly_reports (user_id, week_start, summary, problems, next_plan, status) "
                              "VALUES (?,?,?,?,?,?)", (g.user["id"],) + data)
            audit("提交周报" if status == "submitted" else "保存周报", monday.isoformat())
            flash("周报已提交" if status == "submitted" else "周报草稿已保存", "ok")
            return redirect(url_for("weekly_detail", wid=wid))
    form = request.form if request.method == "POST" else (w or {})
    return render_template("weekly_form.html", w=w, form=form, monday=monday,
                           sunday=monday + timedelta(days=6), visits=week_visits(g.user["id"], monday))


@app.route("/weekly/<int:wid>")
@login_required
def weekly_detail(wid):
    w = q("SELECT w.*, u.name sales FROM weekly_reports w JOIN users u ON u.id=w.user_id WHERE w.id=?",
          (wid,), one=True)
    check_owner(w)
    monday = parse_date(w["week_start"])
    return render_template("weekly_detail.html", w=w, sunday=monday + timedelta(days=6),
                           visits=week_visits(w["user_id"], monday),
                           approvals=approvals_for("weekly", wid),
                           can_approve=can_approve(w["user_id"]) and w["status"] == "submitted",
                           can_comment=can_approve(w["user_id"]), mine=w["user_id"] == g.user["id"])


@app.route("/weekly/<int:wid>/submit", methods=["POST"])
@login_required
def weekly_submit(wid):
    w = q("SELECT * FROM weekly_reports WHERE id=? AND user_id=?", (wid, g.user["id"]), one=True)
    if not w or w["status"] not in ("draft", "rejected"):
        abort(403)
    execute("UPDATE weekly_reports SET status='submitted', updated_at=datetime('now','localtime') WHERE id=?", (wid,))
    flash("周报已提交", "ok")
    return redirect(url_for("weekly_detail", wid=wid))


# ---------------------------------------------------------------- 月度报告 (自动生成 + 分析)

def compute_month_stats(db, user_id, month):
    first, last = month_range(month)
    f, l = first.isoformat(), last.isoformat()
    visits = db.execute(
        "SELECT v.*, c.name customer, c.level FROM visits v JOIN customers c ON c.id=v.customer_id "
        "WHERE v.user_id=? AND visit_date BETWEEN ? AND ? ORDER BY visit_date", (user_id, f, l)).fetchall()
    trips = db.execute("SELECT * FROM trips WHERE user_id=? AND start_date<=? AND end_date>=? ORDER BY start_date",
                       (user_id, l, f)).fetchall()
    trip_days = 0
    trip_list = []
    for t in trips:
        s, e = max(parse_date(t["start_date"]), first), min(parse_date(t["end_date"]), last)
        days = (e - s).days + 1
        trip_days += days
        trip_list.append({"id": t["id"], "start": t["start_date"], "end": t["end_date"],
                          "destination": t["destination"], "days": days, "cost": trip_cost(t),
                          "status": t["status"]})
    cost = {k: round(sum(t[f"cost_{k}"] for t in trips), 2) for k in ("transport", "hotel", "meal", "other")}
    cost_total = round(sum(cost.values()), 2)

    customers = Counter(v["customer"] for v in visits)
    new_customers = sorted({v["customer"] for v in visits if not db.execute(
        "SELECT 1 FROM visits WHERE customer_id=? AND visit_date<? LIMIT 1",
        (v["customer_id"], f)).fetchone()})
    levels = Counter(v["level"] for v in visits)
    # 负责的 A 类客户本月覆盖情况
    owned_a = [r["name"] for r in db.execute(
        "SELECT name FROM customers WHERE owner_id=? AND level='A' AND status='active'", (user_id,))]
    visited_names = set(customers)
    a_missed = [n for n in owned_a if n not in visited_names]
    # 超过 60 天未拜访的负责客户
    stale = [dict(r) for r in db.execute(
        "SELECT c.name, c.level, MAX(v.visit_date) last FROM customers c "
        "LEFT JOIN visits v ON v.customer_id=c.id AND v.visit_date<=? "
        "WHERE c.owner_id=? AND c.status='active' GROUP BY c.id "
        "HAVING last IS NULL OR last < ? ORDER BY c.level, last", (l, user_id, (last - timedelta(days=60)).isoformat()))]
    pm = prev_month(month)
    pf, pl = month_range(pm)
    prev_visits = db.execute("SELECT COUNT(*) FROM visits WHERE user_id=? AND visit_date BETWEEN ? AND ?",
                             (user_id, pf.isoformat(), pl.isoformat())).fetchone()[0]
    prev_cost = db.execute("SELECT COALESCE(SUM(cost_transport+cost_hotel+cost_meal+cost_other),0) FROM trips "
                           "WHERE user_id=? AND start_date BETWEEN ? AND ?",
                           (user_id, pf.isoformat(), pl.isoformat())).fetchone()[0]
    weeks = {week_monday(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)
             if (first + timedelta(days=i)).weekday() == 0}
    weekly_done = db.execute(
        f"SELECT COUNT(*) FROM weekly_reports WHERE user_id=? AND status IN ('submitted','approved') "
        f"AND week_start IN ({','.join('?' * len(weeks))})", (user_id, *weeks)).fetchone()[0] if weeks else 0
    trip_visits = [v for v in visits if v["trip_id"]]
    return {
        "month": month,
        "visit_count": len(visits),
        "customer_count": len(customers),
        "new_customers": new_customers,
        "contact_count": len({(v["customer_id"], v["contact_name"]) for v in visits}),
        "trip_count": len(trips),
        "trip_days": trip_days,
        "trips": trip_list,
        "cost": cost,
        "cost_total": cost_total,
        "cost_per_visit": round(cost_total / len(trip_visits), 2) if trip_visits else 0,
        "trip_visit_count": len(trip_visits),
        "by_type": Counter(v["visit_type"] for v in visits).most_common(),
        "by_level": {k: levels.get(k, 0) for k in "ABC"},
        "by_stage": Counter(v["stage"] for v in visits if v["stage"]).most_common(),
        "top_customers": customers.most_common(10),
        "pipeline": round(sum(v["amount"] for v in visits), 2),
        "owned_a": len(owned_a),
        "a_missed": a_missed,
        "stale": stale[:20],
        "prev_visits": prev_visits,
        "prev_cost": round(prev_cost, 2),
        "weeks": len(weeks),
        "weekly_done": weekly_done,
        "visits": [{"date": v["visit_date"], "customer": v["customer"], "type": v["visit_type"],
                    "contact": f"{v['contact_name']} {v['contact_title']}".strip(),
                    "content": v["content"], "result": v["result"]} for v in visits],
    }


def analyze(s):
    """根据统计数据生成文字分析"""
    out = []
    if s["visit_count"] == 0:
        out.append("本月无客户拜访记录, 请核实是否漏填, 或说明原因。")
    else:
        out.append(f"本月共拜访客户 {s['visit_count']} 次, 覆盖客户 {s['customer_count']} 家, "
                   f"接触客户人员 {s['contact_count']} 人; 出差 {s['trip_count']} 次共 {s['trip_days']} 天, "
                   f"差旅费用合计 {s['cost_total']:,.2f} 元。")
        if s["prev_visits"]:
            diff = (s["visit_count"] - s["prev_visits"]) / s["prev_visits"] * 100
            word = "增加" if diff >= 0 else "减少"
            out.append(f"拜访次数较上月 ({s['prev_visits']} 次) {word} {abs(diff):.0f}%。")
        if s["new_customers"]:
            out.append(f"新开发客户 {len(s['new_customers'])} 家: {'、'.join(s['new_customers'][:8])}。")
        else:
            out.append("本月无新开发客户, 建议适当增加新客户开拓。")
        if s["trip_days"]:
            per_day = s["trip_visit_count"] / s["trip_days"]
            out.append(f"出差期间日均拜访 {per_day:.1f} 次" +
                       ("; 出差效率偏低, 建议出差前提前预约、集中安排拜访路线。" if per_day < 1 else "。"))
        if s["cost_per_visit"]:
            out.append(f"出差单次拜访成本约 {s['cost_per_visit']:,.2f} 元。")
        if s["top_customers"] and s["visit_count"] >= 5:
            name, n = s["top_customers"][0]
            if n / s["visit_count"] >= 0.4:
                out.append(f"拜访集中于「{name}」({n} 次, 占 {n / s['visit_count']:.0%}), 注意客户分布是否均衡。")
        if s["pipeline"]:
            out.append(f"本月拜访中登记的预计商机金额合计 {s['pipeline']:,.2f} 万元。")
    if s["owned_a"]:
        if s["a_missed"]:
            out.append(f"负责的 A 类重点客户 {s['owned_a']} 家, 本月未拜访 {len(s['a_missed'])} 家: "
                       f"{'、'.join(s['a_missed'][:8])}。")
        else:
            out.append(f"负责的 {s['owned_a']} 家 A 类重点客户本月已全部覆盖。")
    if s["stale"]:
        out.append(f"有 {len(s['stale'])} 家负责客户超过 60 天未拜访, 存在流失风险。")
    if s["weeks"]:
        out.append(f"本月应交周报 {s['weeks']} 份, 已提交 {s['weekly_done']} 份。")
    return "\n".join(out)


def generate_monthly(db, user_id, month, force=False):
    """生成/刷新月报; 已提交或已通过的不会被覆盖"""
    row = db.execute("SELECT id, status FROM monthly_reports WHERE user_id=? AND month=?",
                     (user_id, month)).fetchone()
    if row and (not force or row["status"] in ("submitted", "approved")):
        return row["id"]
    stats = compute_month_stats(db, user_id, month)
    data = (json.dumps(stats, ensure_ascii=False), analyze(stats))
    if row:
        db.execute("UPDATE monthly_reports SET stats_json=?, analysis=?, "
                   "generated_at=datetime('now','localtime') WHERE id=?", data + (row["id"],))
        rid = row["id"]
    else:
        rid = db.execute("INSERT OR IGNORE INTO monthly_reports (user_id, month, stats_json, analysis) "
                         "VALUES (?,?,?,?)", (user_id, month) + data).lastrowid
    db.commit()
    return rid


def needs_monthly(db, user, month):
    """销售员每月都生成; 主管只有本人当月有拜访或出差时才生成"""
    if user["role"] == "sales":
        return True
    first, last = month_range(month)
    f, l = first.isoformat(), last.isoformat()
    return bool(db.execute("SELECT 1 FROM visits WHERE user_id=? AND visit_date BETWEEN ? AND ? "
                           "UNION SELECT 1 FROM trips WHERE user_id=? AND start_date<=? AND end_date>=?",
                           (user["id"], f, l, user["id"], l, f)).fetchone())


def auto_generate_last_month():
    """自然月结束后, 为所有在职销售员生成上月报告"""
    month = prev_month(date.today().strftime("%Y-%m"))
    db = connect()
    try:
        for u in db.execute("SELECT id, role FROM users WHERE active=1 AND role IN ('sales','manager')").fetchall():
            if needs_monthly(db, u, month):
                generate_monthly(db, u["id"], month)
    finally:
        db.close()


def scheduler_loop():
    while True:
        try:
            auto_generate_last_month()
        except Exception as e:  # noqa: BLE001  后台任务不能退出
            print("月报自动生成失败:", e)
        time.sleep(3600)


@app.route("/monthly")
@login_required
def monthly():
    month = request.args.get("month") or prev_month(date.today().strftime("%Y-%m"))
    sql, args = scope("m.user_id")
    rows = q(f"SELECT m.*, u.name sales FROM monthly_reports m JOIN users u ON u.id=m.user_id "
             f"WHERE month=? {sql} ORDER BY u.name", [month] + args)
    parsed = [(r, json.loads(r["stats_json"])) for r in rows]
    history = q(f"SELECT m.month, m.status, m.id FROM monthly_reports m WHERE user_id=? ORDER BY month DESC LIMIT 24",
                (g.user["id"],))
    return render_template("monthly.html", month=month, rows=parsed, history=history,
                           current=date.today().strftime("%Y-%m"))


@app.route("/monthly/generate", methods=["POST"])
@login_required
def monthly_generate():
    month = f_str("month")
    try:
        month_range(month)
    except ValueError:
        abort(400)
    db = get_db()
    if is_manager():
        targets = [u["id"] for u in visible_users() if needs_monthly(db, u, month)]
    else:
        targets = [g.user["id"]]
    for uid in targets:
        generate_monthly(db, uid, month, force=True)
    audit("生成月报", month)
    flash(f"{month} 月度报告已生成/刷新 ({len(targets)} 人); 已提交或已通过的报告不会被覆盖", "ok")
    return redirect(url_for("monthly", month=month))


@app.route("/monthly/<int:mid>", methods=["GET", "POST"])
@login_required
def monthly_detail(mid):
    m = q("SELECT m.*, u.name sales, u.region FROM monthly_reports m JOIN users u ON u.id=m.user_id WHERE m.id=?",
          (mid,), one=True)
    check_owner(m)
    mine = m["user_id"] == g.user["id"]
    if request.method == "POST":
        if not mine or m["status"] in ("submitted", "approved"):
            abort(403)
        action = request.form.get("action")
        if action == "refresh":
            generate_monthly(get_db(), m["user_id"], m["month"], force=True)
            flash("统计数据已刷新", "ok")
        else:
            status = "submitted" if action == "submit" else m["status"]
            execute("UPDATE monthly_reports SET self_summary=?, status=? WHERE id=?",
                    (f_str("self_summary"), status, mid))
            flash("月度报告已提交审批" if action == "submit" else "补充说明已保存", "ok")
        return redirect(url_for("monthly_detail", mid=mid))
    return render_template("monthly_detail.html", m=m, s=json.loads(m["stats_json"]),
                           approvals=approvals_for("monthly", mid), mine=mine,
                           can_approve=can_approve(m["user_id"]) and m["status"] == "submitted",
                           can_comment=can_approve(m["user_id"]))


# ---------------------------------------------------------------- 审批

@app.route("/approve/<target>/<int:tid>", methods=["POST"])
@login_required
def approve(target, tid):
    if target not in TARGETS:
        abort(404)
    table, label = TARGETS[target]
    row = q(f"SELECT * FROM {table} WHERE id=?", (tid,), one=True)
    if row is None:
        abort(404)
    if not can_approve(row["user_id"]):
        abort(403)
    decision = request.form.get("decision")
    comment = f_str("comment")
    if decision not in ("approved", "rejected", "comment"):
        abort(400)
    if decision != "comment" and row["status"] != "submitted":
        flash("该报告当前不是待审批状态", "error")
    elif decision in ("rejected", "comment") and not comment:
        flash("退回或批注时请填写意见", "error")
    else:
        execute("INSERT INTO approvals (target_type, target_id, approver_id, decision, comment) VALUES (?,?,?,?,?)",
                (target, tid, g.user["id"], decision, comment))
        if decision != "comment":
            execute(f"UPDATE {table} SET status=? WHERE id=?", (decision, tid))
        audit(f"审批{label}", f"#{tid} {decision}")
        flash({"approved": "已审批通过", "rejected": "已退回修改", "comment": "意见已保存"}[decision], "ok")
    return redirect(request.referrer or url_for("approvals"))


@app.route("/approvals")
@roles_required("manager", "admin")
def approvals():
    items = []
    for key, (table, label) in TARGETS.items():
        sql, args = scope("t.user_id")
        for r in q(f"SELECT t.*, u.name sales FROM {table} t JOIN users u ON u.id=t.user_id "
                   f"WHERE t.status='submitted' {sql} ORDER BY t.updated_at" if key != "monthly" else
                   f"SELECT t.*, u.name sales FROM {table} t JOIN users u ON u.id=t.user_id "
                   f"WHERE t.status='submitted' {sql} ORDER BY t.month", args):
            if not can_approve(r["user_id"]):
                continue
            if key == "trip":
                title, url = f"{r['start_date']} ~ {r['end_date']} {r['destination']}", url_for("trip_detail", tid=r["id"])
            elif key == "weekly":
                title, url = f"{r['week_start']} 当周", url_for("weekly_detail", wid=r["id"])
            else:
                title, url = f"{r['month']} 月度报告", url_for("monthly_detail", mid=r["id"])
            items.append({"type": label, "sales": r["sales"], "title": title, "url": url})
    return render_template("approvals.html", items=items)


# ---------------------------------------------------------------- 客户名单

@app.route("/customers")
@login_required
def customers():
    a = request.args
    where, args = ["1=1"], []
    if a.get("kw"):
        where.append("(c.name LIKE ? OR c.region LIKE ? OR c.industry LIKE ?)")
        args += [f"%{a['kw']}%"] * 3
    if a.get("level"):
        where.append("c.level=?"); args.append(a["level"])
    if a.get("status"):
        where.append("c.status=?"); args.append(a["status"])
    if a.get("owner_id"):
        where.append("c.owner_id=?"); args.append(a["owner_id"])
    rows = q(f"SELECT c.*, u.name owner_name, COUNT(v.id) nvisits, MAX(v.visit_date) last_visit "
             f"FROM customers c LEFT JOIN users u ON u.id=c.owner_id LEFT JOIN visits v ON v.customer_id=c.id "
             f"WHERE {' AND '.join(where)} GROUP BY c.id ORDER BY c.status='pending' DESC, c.level, c.name", args)
    owners = q("SELECT id, name FROM users WHERE role IN ('sales','manager') AND active=1 ORDER BY name")
    return render_template("customers.html", rows=rows, a=a, owners=owners)


@app.route("/customers/new", methods=["GET", "POST"])
@app.route("/customers/<int:cid>/edit", methods=["GET", "POST"])
@login_required
def customer_form(cid=None):
    c = None
    if cid:
        if not is_manager():
            abort(403)
        c = q("SELECT * FROM customers WHERE id=?", (cid,), one=True) or abort(404)
    if request.method == "POST":
        name = f_str("name")
        dup = q("SELECT id FROM customers WHERE name=? AND id!=?", (name, cid or 0), one=True)
        if not name:
            flash("请填写客户名称", "error")
        elif dup:
            flash("客户名称已存在", "error")
        else:
            level = f_str("level", "B") if f_str("level") in ("A", "B", "C") else "B"
            if is_manager():
                owner = request.form.get("owner_id", type=int)
                status = f_str("status", "active")
                status = status if status in ("active", "pending", "disabled") else "active"
            else:
                owner, status = g.user["id"], "pending"   # 销售员新增客户需主管审核
            data = (name, level, f_str("industry"), f_str("region"), f_str("address"), owner, status, f_str("remark"))
            if c:
                execute("UPDATE customers SET name=?, level=?, industry=?, region=?, address=?, owner_id=?, "
                        "status=?, remark=? WHERE id=?", data + (cid,))
            else:
                cid = execute("INSERT INTO customers (name, level, industry, region, address, owner_id, status, "
                              "remark, created_by) VALUES (?,?,?,?,?,?,?,?,?)", data + (g.user["id"],))
            audit("保存客户", name)
            flash("已提交, 主管审核通过后即可在拜访记录中选择" if status == "pending" and not is_manager()
                  else "客户已保存", "ok")
            return redirect(url_for("customer_detail", cid=cid))
    owners = q("SELECT id, name FROM users WHERE role IN ('sales','manager') AND active=1 ORDER BY name")
    form = request.form if request.method == "POST" else (c or {})
    return render_template("customer_form.html", c=c, form=form, owners=owners)


@app.route("/customers/<int:cid>")
@login_required
def customer_detail(cid):
    c = q("SELECT c.*, u.name owner_name FROM customers c LEFT JOIN users u ON u.id=c.owner_id WHERE c.id=?",
          (cid,), one=True) or abort(404)
    sql, args = scope("v.user_id")
    vs = q(f"{VISIT_SELECT} WHERE v.customer_id=? {sql} ORDER BY visit_date DESC", [cid] + args)
    contacts = q("SELECT * FROM contacts WHERE customer_id=? ORDER BY name", (cid,))
    return render_template("customer_detail.html", c=c, visits=vs, contacts=contacts)


@app.route("/customers/<int:cid>/approve", methods=["POST"])
@roles_required("manager", "admin")
def customer_approve(cid):
    status = "active" if request.form.get("ok") else "disabled"
    execute("UPDATE customers SET status=? WHERE id=?", (status, cid))
    audit("审核客户", f"#{cid} {status}")
    flash("客户已审核通过" if status == "active" else "客户申请已驳回", "ok")
    return redirect(request.referrer or url_for("customers"))


@app.route("/customers/import", methods=["POST"])
@roles_required("manager", "admin")
def customer_import():
    from openpyxl import load_workbook
    file = request.files.get("file")
    if not file:
        flash("请选择 Excel 文件", "error")
        return redirect(url_for("customers"))
    try:
        ws = load_workbook(file, read_only=True).active
    except Exception:  # noqa: BLE001
        flash("无法读取文件, 请上传 .xlsx 格式", "error")
        return redirect(url_for("customers"))
    users = {r["username"]: r["id"] for r in q("SELECT id, username FROM users")}
    users.update({r["name"]: r["id"] for r in q("SELECT id, name FROM users")})
    added = skipped = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0 or not row or not row[0]:
            continue
        row = list(row) + [None] * 6
        name = str(row[0]).strip()
        if q("SELECT 1 FROM customers WHERE name=?", (name,), one=True):
            skipped += 1
            continue
        level = str(row[1] or "B").strip().upper()
        execute("INSERT INTO customers (name, level, industry, region, address, owner_id, created_by) "
                "VALUES (?,?,?,?,?,?,?)",
                (name, level if level in ("A", "B", "C") else "B", str(row[2] or ""), str(row[3] or ""),
                 str(row[4] or ""), users.get(str(row[5] or "").strip()), g.user["id"]))
        added += 1
    audit("导入客户", f"新增{added} 跳过{skipped}")
    flash(f"导入完成: 新增 {added} 家, 跳过重名 {skipped} 家", "ok")
    return redirect(url_for("customers"))


# ---------------------------------------------------------------- 后台统计

@app.route("/stats")
@login_required
def stats():
    a = request.args.to_dict()
    today = date.today()
    a.setdefault("start", today.replace(day=1).isoformat())
    a.setdefault("end", today.isoformat())
    where, args = visit_filters(a)
    by_sales = q(f"SELECT u.id, u.name, COUNT(*) n, COUNT(DISTINCT v.customer_id) nc, "
                 f"COUNT(DISTINCT v.visit_date) ndays, SUM(v.visit_type='首次拜访') nfirst, "
                 f"COALESCE(SUM(v.amount),0) amount "
                 f"FROM visits v JOIN users u ON u.id=v.user_id JOIN customers c ON c.id=v.customer_id "
                 f"WHERE {where} GROUP BY u.id ORDER BY n DESC", args)
    tsql, targs = scope("t.user_id")
    trips_by = {r["user_id"]: r for r in q(
        f"SELECT user_id, COUNT(*) n, COALESCE(SUM(julianday(MIN(end_date, ?)) - julianday(MAX(start_date, ?)) + 1),0) days, "
        f"COALESCE(SUM(cost_transport+cost_hotel+cost_meal+cost_other),0) cost "
        f"FROM trips t WHERE start_date<=? AND end_date>=? {tsql} GROUP BY user_id",
        [a["end"], a["start"], a["end"], a["start"]] + targs)}
    by_customer = q(f"SELECT c.id, c.name, c.level, COUNT(*) n, MAX(v.visit_date) last, "
                    f"GROUP_CONCAT(DISTINCT u.name) sales, GROUP_CONCAT(DISTINCT v.contact_name) contacts "
                    f"FROM visits v JOIN customers c ON c.id=v.customer_id JOIN users u ON u.id=v.user_id "
                    f"WHERE {where} GROUP BY c.id ORDER BY n DESC LIMIT 100", args)
    by_type = q(f"SELECT visit_type, COUNT(*) n FROM visits v JOIN customers c ON c.id=v.customer_id "
                f"WHERE {where} GROUP BY visit_type ORDER BY n DESC", args)
    detail = q(f"{VISIT_SELECT} WHERE {where} ORDER BY v.visit_date DESC LIMIT 200", args)
    return render_template("stats.html", a=a, by_sales=by_sales, trips_by=trips_by, by_customer=by_customer,
                           by_type=by_type, detail=detail, users=visible_users(),
                           customers=active_customers(), types=VISIT_TYPES)


@app.route("/stats/export")
@login_required
def stats_export():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    where, args = visit_filters()
    rows = q(f"{VISIT_SELECT} WHERE {where} ORDER BY v.visit_date, u.name", args)
    wb = Workbook()
    ws = wb.active
    ws.title = "拜访明细"
    head = ["日期", "开始", "结束", "销售员", "客户", "等级", "拜访类型", "洽谈人", "职务", "电话", "其他参与人",
            "洽谈内容", "拜访成果", "商机阶段", "预计金额(万)", "下一步", "下次跟进", "所属出差"]
    ws.append(head)
    for v in rows:
        ws.append([v["visit_date"], v["start_time"], v["end_time"], v["sales"], v["customer"], v["level"],
                   v["visit_type"], v["contact_name"], v["contact_title"], v["contact_phone"], v["attendees"],
                   v["content"], v["result"], v["stage"], v["amount"], v["next_step"], v["next_date"],
                   v["trip_dest"] or ""])
    ws2 = wb.create_sheet("按销售员")
    ws2.append(["销售员", "拜访次数", "客户数", "首次拜访", "预计金额(万)"])
    agg = defaultdict(lambda: [0, set(), 0, 0.0])
    for v in rows:
        x = agg[v["sales"]]
        x[0] += 1; x[1].add(v["customer"]); x[2] += v["visit_type"] == "首次拜访"; x[3] += v["amount"]
    for k, x in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        ws2.append([k, x[0], len(x[1]), x[2], round(x[3], 2)])
    ws3 = wb.create_sheet("按客户")
    ws3.append(["客户", "等级", "拜访次数", "最近拜访", "拜访销售"])
    cagg = defaultdict(lambda: [None, 0, "", set()])
    for v in rows:
        x = cagg[v["customer"]]
        x[0] = v["level"]; x[1] += 1; x[2] = max(x[2], v["visit_date"]); x[3].add(v["sales"])
    for k, x in sorted(cagg.items(), key=lambda kv: -kv[1][1]):
        ws3.append([k, x[0], x[1], x[2], "、".join(sorted(x[3]))])
    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="2F5597")
        for col in sheet.columns:
            width = max(len(str(c.value or "")) for c in col)
            sheet.column_dimensions[col[0].column_letter].width = min(max(width * 1.6, 8), 60)
        sheet.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    audit("导出统计")
    name = f"拜访统计_{request.args.get('start', '')}_{request.args.get('end', '')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- 账号管理

@app.route("/users")
@roles_required("admin")
def users():
    rows = q("SELECT u.*, m.name manager_name FROM users u LEFT JOIN users m ON m.id=u.manager_id "
             "ORDER BY u.active DESC, u.role, u.name")
    return render_template("users.html", rows=rows)


@app.route("/users/new", methods=["GET", "POST"])
@app.route("/users/<int:uid>/edit", methods=["GET", "POST"])
@roles_required("admin")
def user_form(uid=None):
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True) if uid else None
    if uid and not u:
        abort(404)
    if request.method == "POST":
        username, role = f_str("username"), f_str("role")
        pw = request.form.get("password", "")
        dup = q("SELECT 1 FROM users WHERE username=? AND id!=?", (username, uid or 0), one=True)
        if not username or not f_str("name") or role not in ROLES:
            flash("请完整填写用户名、姓名和角色", "error")
        elif dup:
            flash("用户名已存在", "error")
        elif not u and len(pw) < 6:
            flash("初始密码至少 6 位", "error")
        elif u and u["id"] == g.user["id"] and (role != "admin" or not request.form.get("active")):
            flash("不能取消自己的管理员权限或停用自己", "error")
        else:
            manager_id = request.form.get("manager_id", type=int) if role == "sales" else None
            data = (username, f_str("name"), role, manager_id, f_str("region"), f_str("phone"),
                    1 if request.form.get("active") else 0)
            if u:
                execute("UPDATE users SET username=?, name=?, role=?, manager_id=?, region=?, phone=?, active=? "
                        "WHERE id=?", data + (uid,))
                if pw:
                    execute("UPDATE users SET password_hash=?, must_change_pw=1 WHERE id=?",
                            (generate_password_hash(pw), uid))
                to_id = request.form.get("transfer_to", type=int)
                if to_id and to_id != uid:
                    n = get_db().execute("UPDATE customers SET owner_id=? WHERE owner_id=?", (to_id, uid)).rowcount
                    get_db().commit()
                    audit("改派客户", f"{username} -> #{to_id} 共{n}家")
                    flash(f"已将 {n} 家客户改派", "ok")
            else:
                execute("INSERT INTO users (username, name, role, manager_id, region, phone, active, password_hash) "
                        "VALUES (?,?,?,?,?,?,?,?)", data + (generate_password_hash(pw),))
            audit("保存账号", username)
            flash("账号已保存" + (", 用户下次登录需修改密码" if pw else ""), "ok")
            return redirect(url_for("users"))
    managers = q("SELECT id, name FROM users WHERE role='manager' AND active=1 ORDER BY name")
    owned = q("SELECT COUNT(*) c FROM customers WHERE owner_id=?", (uid,), one=True)["c"] if u else 0
    others = q("SELECT id, name FROM users WHERE role IN ('sales','manager') AND active=1 AND id!=? ORDER BY name",
               (uid or 0,))
    form = request.form if request.method == "POST" else (u or {"active": 1, "role": "sales"})
    return render_template("user_form.html", u=u, form=form, managers=managers, owned=owned, others=others)


@app.route("/logs")
@roles_required("admin")
def logs():
    rows = q("SELECT l.*, u.name FROM audit_log l LEFT JOIN users u ON u.id=l.user_id ORDER BY l.id DESC LIMIT 300")
    return render_template("logs.html", rows=rows)


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, msg="没有权限访问该页面"), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, msg="页面或记录不存在"), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, msg=e.description or "请求无效"), 400


def start_background():
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()


if __name__ == "__main__":
    start_background()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
