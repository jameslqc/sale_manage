-- 销售周报 / 客户拜访 / 出差报告系统 数据库结构 (SQLite)

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('sales', 'manager', 'admin')),
    manager_id    INTEGER REFERENCES users(id),      -- 销售员所属主管
    region        TEXT DEFAULT '',
    phone         TEXT DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    must_change_pw INTEGER NOT NULL DEFAULT 1,       -- 首次登录强制改密
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    level       TEXT NOT NULL DEFAULT 'B' CHECK (level IN ('A', 'B', 'C')),
    industry    TEXT DEFAULT '',
    region      TEXT DEFAULT '',
    address     TEXT DEFAULT '',
    owner_id    INTEGER REFERENCES users(id),        -- 负责销售
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending', 'disabled')),
    remark      TEXT DEFAULT '',
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    name        TEXT NOT NULL,
    title       TEXT DEFAULT '',
    phone       TEXT DEFAULT '',
    UNIQUE (customer_id, name)
);

-- 出差报告
CREATE TABLE IF NOT EXISTS trips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    destination     TEXT NOT NULL,
    purpose         TEXT DEFAULT '',
    transport       TEXT DEFAULT '',
    cost_transport  REAL NOT NULL DEFAULT 0,
    cost_hotel      REAL NOT NULL DEFAULT 0,
    cost_meal       REAL NOT NULL DEFAULT 0,
    cost_other      REAL NOT NULL DEFAULT 0,
    summary         TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'draft',
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 客户拜访记录
CREATE TABLE IF NOT EXISTS visits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    trip_id        INTEGER REFERENCES trips(id),
    customer_id    INTEGER NOT NULL REFERENCES customers(id),
    visit_date     TEXT NOT NULL,
    start_time     TEXT DEFAULT '',
    end_time       TEXT DEFAULT '',
    visit_type     TEXT NOT NULL DEFAULT '回访',
    contact_name   TEXT NOT NULL,
    contact_title  TEXT DEFAULT '',
    contact_phone  TEXT DEFAULT '',
    attendees      TEXT DEFAULT '',     -- 其他参与人员 (双方)
    content        TEXT NOT NULL,       -- 洽谈内容
    result         TEXT DEFAULT '',     -- 拜访成果
    stage          TEXT DEFAULT '',     -- 商机阶段
    amount         REAL NOT NULL DEFAULT 0, -- 预计商机金额(万元)
    next_step      TEXT DEFAULT '',
    next_date      TEXT DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 销售周报
CREATE TABLE IF NOT EXISTS weekly_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    week_start  TEXT NOT NULL,          -- 周一日期
    summary     TEXT NOT NULL,
    problems    TEXT DEFAULT '',
    next_plan   TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'draft',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (user_id, week_start)
);

-- 月度出差报告 (自然月底自动生成)
CREATE TABLE IF NOT EXISTS monthly_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    month        TEXT NOT NULL,         -- YYYY-MM
    stats_json   TEXT NOT NULL,
    analysis     TEXT NOT NULL,
    self_summary TEXT DEFAULT '',       -- 销售员补充说明
    status       TEXT NOT NULL DEFAULT 'generated',
    generated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (user_id, month)
);

-- 审批记录 (周报/出差/月报通用, 保留多轮意见历史)
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL CHECK (target_type IN ('trip', 'weekly', 'monthly')),
    target_id   INTEGER NOT NULL,
    approver_id INTEGER NOT NULL REFERENCES users(id),
    decision    TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'comment')),
    comment     TEXT DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 操作日志
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT DEFAULT '',
    ip         TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_visits_user_date ON visits(user_id, visit_date);
CREATE INDEX IF NOT EXISTS idx_visits_customer ON visits(customer_id);
CREATE INDEX IF NOT EXISTS idx_trips_user_date ON trips(user_id, start_date);
CREATE INDEX IF NOT EXISTS idx_approvals_target ON approvals(target_type, target_id);
