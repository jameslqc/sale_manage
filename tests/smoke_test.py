"""冒烟测试: python tests/smoke_test.py  (使用临时数据库, 不影响正式数据)"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SALES_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")

import app as A  # noqa: E402

A.DB_PATH = os.environ["SALES_DB"]
import seed_demo  # noqa: E402,F401  生成演示数据到临时库

A.app.config["TESTING"] = True
fails = []


def client(username):
    c = A.app.test_client()
    r = c.get("/login")
    token = re.search(r'name="_csrf" value="(\w+)"', r.text).group(1)
    r = c.post("/login", data={"username": username, "password": "demo1234", "_csrf": token})
    assert r.status_code == 302, f"{username} 登录失败"
    c.token = re.search(r'name="_csrf" value="(\w+)"', c.get("/password").text).group(1)  # 登录后令牌已轮换
    return c


def check(c, url, code=200, who=""):
    r = c.get(url)
    if r.status_code != code:
        fails.append(f"[{who}] GET {url} -> {r.status_code}")
    return r


def post(c, url, data, who=""):
    data = dict(data, _csrf=c.token)
    r = c.post(url, data=data)
    if r.status_code not in (200, 302):
        fails.append(f"[{who}] POST {url} -> {r.status_code}")
    return r


month = A.date.today().strftime("%Y-%m")
last = A.prev_month(month)
pages = ["/", "/visits", "/visits/new", "/trips", "/trips/new", "/weekly", "/weekly/new", f"/monthly?month={last}",
         f"/monthly?month={month}", "/customers", "/customers/new", "/stats", "/stats/export", "/password",
         "/customers/1"]

for who in ("admin", "zhangjl", "lixm", "wangf"):
    c = client(who)
    for p in pages:
        check(c, p, who=who)
    for p in ("/approvals", "/users", "/logs", "/users/new", "/users/2/edit"):
        expected = 200 if who == "admin" or (who == "zhangjl" and p == "/approvals") else 403
        check(c, p, expected, who)

# 数据隔离: 王芳看不到李晓明的拜访
wang = client("wangf")
with A.app.app_context():
    li_visit = A.q("SELECT v.id FROM visits v JOIN users u ON u.id=v.user_id WHERE u.username='lixm' LIMIT 1", one=True)["id"]
    li_trip = A.q("SELECT t.id FROM trips t JOIN users u ON u.id=t.user_id WHERE u.username='lixm' LIMIT 1", one=True)["id"]
    li_month = A.q("SELECT m.id FROM monthly_reports m JOIN users u ON u.id=m.user_id WHERE u.username='lixm' "
                   "AND status='submitted'", one=True)["id"]
check(wang, f"/visits/{li_visit}", 403, "wangf 越权")
check(wang, f"/trips/{li_trip}", 403, "wangf 越权")
r = wang.post(f"/approve/monthly/{li_month}", data={"decision": "approved", "_csrf": wang.token})
if r.status_code != 403:
    fails.append("销售员竟能审批")

# 无 CSRF 令牌的 POST 被拒
if wang.post("/visits/new", data={"customer_id": 1}).status_code != 400:
    fails.append("缺少 CSRF 未被拦截")

# 销售员完整流程: 建出差 -> 添加拜访 -> 提交 -> 主管退回 -> 修改 -> 再提交 -> 通过
li = client("lixm")
with A.app.app_context():
    cust = A.q("SELECT id FROM customers WHERE status='active' AND owner_id=(SELECT id FROM users WHERE username='lixm')",
               one=True)["id"]
far = A.date.today() - A.timedelta(days=400)
r = post(li, "/trips/new", {"start_date": far.isoformat(), "end_date": (far + A.timedelta(days=1)).isoformat(),
                             "destination": "测试城市", "cost_hotel": "300", "action": "save"}, "lixm")
tid = int(r.headers["Location"].rstrip("/").split("/")[-1])
r = post(li, "/visits/new", {"customer_id": cust, "visit_date": far.isoformat(), "visit_type": "回访",
                              "contact_name": "测试联系人", "contact_title": "采购", "trip_id": tid,
                              "content": "这是一条测试洽谈内容, 超过十个字。"}, "lixm")
assert "/visits/" in r.headers.get("Location", ""), "拜访保存失败"
r = post(li, "/visits/new", {"customer_id": cust, "visit_date": "2099-01-01", "contact_name": "x", "content": "太短"}, "lixm")
if "拜访日期不能晚于今天" not in r.text:
    fails.append("未来日期校验失效")
post(li, f"/trips/{tid}/submit", {}, "lixm")
mgr = client("zhangjl")
r = post(mgr, f"/approve/trip/{tid}", {"decision": "rejected", "comment": "请补充出差总结"}, "zhangjl")
with A.app.app_context():
    assert A.q("SELECT status FROM trips WHERE id=?", (tid,), one=True)["status"] == "rejected"
post(li, f"/trips/{tid}/edit", {"start_date": far.isoformat(), "end_date": (far + A.timedelta(days=1)).isoformat(),
                                 "destination": "测试城市", "summary": "已补充", "action": "submit"}, "lixm")
post(mgr, f"/approve/trip/{tid}", {"decision": "approved", "comment": "同意"}, "zhangjl")
with A.app.app_context():
    assert A.q("SELECT status FROM trips WHERE id=?", (tid,), one=True)["status"] == "approved"
    assert A.q("SELECT COUNT(*) c FROM approvals WHERE target_type='trip' AND target_id=?", (tid,), one=True)["c"] == 2
r = li.get(f"/visits/1/edit")  # 已审批出差中的拜访不可修改(或不是本人)
check(li, f"/trips/{tid}", who="lixm")

# 周报 + 月报
post(li, "/weekly/new", {"week": far.isoformat(), "summary": "测试周报总结内容十个字以上", "action": "submit"}, "lixm")
post(mgr, "/monthly/generate", {"month": far.strftime("%Y-%m")}, "zhangjl")
with A.app.app_context():
    m = A.q("SELECT * FROM monthly_reports WHERE month=? AND user_id=(SELECT id FROM users WHERE username='lixm')",
            (far.strftime("%Y-%m"),), one=True)
    assert m and "拜访客户 1 次" in m["analysis"], m and m["analysis"]
check(li, f"/monthly/{m['id']}", who="lixm")
post(li, f"/monthly/{m['id']}", {"self_summary": "补充", "action": "submit"}, "lixm")
post(mgr, f"/approve/monthly/{m['id']}", {"decision": "approved", "comment": "好"}, "zhangjl")

# 自动生成上月报告 (幂等)
A.auto_generate_last_month()
A.auto_generate_last_month()

# 客户申请与审核
post(li, "/customers/new", {"name": "测试新客户", "level": "A"}, "lixm")
with A.app.app_context():
    nc = A.q("SELECT * FROM customers WHERE name='测试新客户'", one=True)
    assert nc["status"] == "pending"
post(mgr, f"/customers/{nc['id']}/approve", {"ok": "1"}, "zhangjl")

# 导出 Excel 内容
r = mgr.get(f"/stats/export?start={far.isoformat()}&end={A.date.today().isoformat()}")
assert r.data[:2] == b"PK", "导出不是 xlsx"

# 离职交接: 停用王芳并把客户改派给李晓明
adm = client("admin")
with A.app.app_context():
    wid = A.q("SELECT id FROM users WHERE username='wangf'", one=True)["id"]
    lid = A.q("SELECT id FROM users WHERE username='lixm'", one=True)["id"]
check(adm, f"/users/{wid}/edit", who="admin")
post(adm, f"/users/{wid}/edit", {"username": "wangf", "name": "王芳", "role": "sales", "transfer_to": lid}, "admin")
with A.app.app_context():
    assert A.q("SELECT COUNT(*) c FROM customers WHERE owner_id=?", (wid,), one=True)["c"] == 0, "改派失败"
    assert A.q("SELECT active FROM users WHERE id=?", (wid,), one=True)["active"] == 0, "停用失败"
    assert A.q("SELECT COUNT(*) c FROM visits WHERE user_id=?", (wid,), one=True)["c"] > 0, "历史拜访丢失"

if fails:
    print("失败:\n  " + "\n  ".join(fails))
    sys.exit(1)
print("全部通过")
