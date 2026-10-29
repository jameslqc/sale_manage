"""生成演示数据 (会清空现有数据库!): python seed_demo.py

演示账号 (密码均为 demo1234):
  admin      系统管理员
  zhangjl    张经理 (销售主管)
  lixm       李晓明 (销售员, 归张经理)
  wangf      王芳   (销售员, 归张经理)
"""
import os
import random
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

import app as A

random.seed(7)
if os.path.exists(A.DB_PATH):
    os.remove(A.DB_PATH)
A.init_db()
db = A.connect()
pw = generate_password_hash("demo1234")
db.execute("UPDATE users SET password_hash=?, must_change_pw=0 WHERE username='admin'", (pw,))


def add_user(username, name, role, manager=None, region=""):
    return db.execute("INSERT INTO users (username, password_hash, name, role, manager_id, region, must_change_pw) "
                      "VALUES (?,?,?,?,?,?,0)", (username, pw, name, role, manager, region)).lastrowid


mgr = add_user("zhangjl", "张经理", "manager", region="华东区")
li = add_user("lixm", "李晓明", "sales", mgr, "江苏")
wang = add_user("wangf", "王芳", "sales", mgr, "浙江")

customers = [
    ("苏州汇川电机有限公司", "A", "电机", "江苏苏州", li), ("无锡宏达电器有限公司", "A", "家电", "江苏无锡", li),
    ("常州精工自动化有限公司", "B", "自动化", "江苏常州", li), ("南京瑞丰科技有限公司", "B", "电源", "江苏南京", li),
    ("南通华盛机电有限公司", "C", "机电", "江苏南通", li),
    ("杭州中控电子有限公司", "A", "工控", "浙江杭州", wang), ("宁波方太电器有限公司", "A", "家电", "浙江宁波", wang),
    ("温州正泰配电有限公司", "B", "低压电器", "浙江温州", wang), ("嘉兴博远新能源有限公司", "B", "新能源", "浙江嘉兴", wang),
    ("绍兴永利电动工具厂", "C", "电动工具", "浙江绍兴", wang),
]
cids = {}
for name, lv, ind, region, owner in customers:
    cids[name] = (db.execute("INSERT INTO customers (name, level, industry, region, owner_id, created_by) "
                             "VALUES (?,?,?,?,?,?)", (name, lv, ind, region, owner, mgr)).lastrowid, owner)
db.execute("INSERT INTO customers (name, level, region, owner_id, status, created_by) VALUES (?,?,?,?,?,?)",
           ("扬州新科电子有限公司", "C", "江苏扬州", li, "pending", li))

contacts = ["陈工 采购经理", "刘总 总经理", "赵工 研发主管", "孙经理 采购主管", "周工 技术总监"]
contents = [
    "介绍公司新款 MOS 管及驱动方案, 客户对 600V 系列感兴趣, 要求提供规格书和样品。",
    "跟进上次送样测试情况, 客户反馈温升正常, 待小批量验证; 讨论价格和交期。",
    "与研发讨论 FOC 电机控制方案, 客户希望我方提供参考设计和技术支持。",
    "年度框架协议商务谈判, 客户要求降价 5%, 已答复需内部评估。",
    "处理上批次来料不良问题, 现场分析后确认为客户焊接工艺问题, 已出具报告。",
    "拜访新客户, 了解其产品线和年用量, 对方目前使用竞品, 有替代意向。",
]
types = ["回访", "技术交流", "商务谈判", "售后服务", "首次拜访"]
stages = ["初步接触", "需求确认", "方案/报价", "商务谈判"]

today = date.today()
first_this = today.replace(day=1)
months = []
d = first_this
for _ in range(3):
    d = (d - timedelta(days=1)).replace(day=1)
    months.append(d)
months = sorted(months) + [first_this]

for uid in (li, wang):
    mine = [n for n, (_, o) in cids.items() if o == uid]
    for m0 in months:
        end_of_month = (m0.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        end_of_month = min(end_of_month, today)
        # 每月 1-2 次出差
        for k in range(random.randint(1, 2)):
            sd = m0 + timedelta(days=random.randint(1, 10) + k * 14)
            if sd > end_of_month - timedelta(days=2):
                continue
            ed = sd + timedelta(days=random.randint(1, 3))
            dest = "、".join(sorted({c.split("有限")[0][:2] for c in random.sample(mine, 2)}))
            status = "approved" if m0 < first_this else "draft"
            tid = db.execute(
                "INSERT INTO trips (user_id, start_date, end_date, destination, purpose, transport, cost_transport, "
                "cost_hotel, cost_meal, cost_other, summary, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, sd.isoformat(), ed.isoformat(), dest, "重点客户回访及新品推广", random.choice(["高铁", "自驾"]),
                 random.randint(200, 800), 380 * (ed - sd).days, 100 * ((ed - sd).days + 1), random.randint(0, 200),
                 "本次出差完成计划客户拜访, 新品推广反馈较好, 有两家客户进入送样阶段。", status)).lastrowid
            if status == "approved":
                db.execute("INSERT INTO approvals (target_type, target_id, approver_id, decision, comment) "
                           "VALUES ('trip',?,?,'approved','同意, 注意跟进送样客户的测试结果。')", (tid, mgr))
            day = sd
            while day <= ed:
                for _ in range(random.randint(1, 2)):
                    cname = random.choice(mine)
                    cn, ct = random.choice(contacts).split()
                    db.execute(
                        "INSERT INTO visits (user_id, trip_id, customer_id, visit_date, start_time, end_time, visit_type, "
                        "contact_name, contact_title, attendees, content, result, stage, amount, next_step, next_date) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uid, tid, cids[cname][0], day.isoformat(), "09:30", "11:00", random.choice(types), cn, ct,
                         "", random.choice(contents), "客户同意进入下一步评估", random.choice(stages),
                         random.choice([0, 5, 10, 20, 50]), "寄送样品并跟进测试", (day + timedelta(days=10)).isoformat()))
                    db.execute("INSERT OR IGNORE INTO contacts (customer_id, name, title) VALUES (?,?,?)",
                               (cids[cname][0], cn, ct))
                day += timedelta(days=1)
        # 本地拜访
        for _ in range(random.randint(2, 4)):
            vd = m0 + timedelta(days=random.randint(0, 27))
            if vd > today:
                continue
            cname = random.choice(mine)
            cn, ct = random.choice(contacts).split()
            db.execute("INSERT INTO visits (user_id, customer_id, visit_date, visit_type, contact_name, contact_title, content) "
                       "VALUES (?,?,?,?,?,?,?)", (uid, cids[cname][0], vd.isoformat(), "回访", cn, ct, random.choice(contents)))
        # 周报
        wk = A.week_monday(m0)
        while wk <= min(end_of_month, today):
            if wk >= m0 - timedelta(days=6) and wk < A.week_monday(today):
                db.execute("INSERT OR IGNORE INTO weekly_reports (user_id, week_start, summary, next_plan, status) "
                           "VALUES (?,?,?,?,?)",
                           (uid, wk.isoformat(), "本周完成重点客户拜访, 推进送样测试, 整理客户需求。",
                            "继续跟进样品测试结果, 安排新客户拜访。", random.choice(["approved", "submitted"])))
            wk += timedelta(days=7)
db.commit()

# 生成历史月报
for uid in (li, wang):
    for m0 in months[:-1]:
        A.generate_monthly(db, uid, m0.strftime("%Y-%m"))
lastm = months[-2].strftime("%Y-%m")
db.execute("UPDATE monthly_reports SET status='submitted', self_summary='本月重点推进新品送样, 两家 A 类客户进入小批量验证。' "
           "WHERE user_id=? AND month=?", (li, lastm))
db.commit()
db.close()
print("演示数据已生成:", A.DB_PATH)
print("账号: admin / zhangjl(主管) / lixm, wangf(销售员)  密码均为 demo1234")
