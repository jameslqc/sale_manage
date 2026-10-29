# 销售拜访管理系统

销售员客户拜访记录 / 出差报告 / 周报 / 月度报告 + 主管审批 + 后台统计。
技术栈: Python 3.10+ · Flask · SQLite · 网页端 (手机浏览器可直接使用, 无需安装 App)。

## 功能对照需求

| 需求 | 实现 |
|---|---|
| 1. 每个销售员独立账号密码, 可远程/云端访问 | 管理员在「账号管理」中开户; 密码加密存储; 首次登录强制改密; 登录失败 5 次锁定 10 分钟。部署到百度智能云后, 外网任何地方都可通过浏览器访问 |
| 2. 局域网外填写出差报告 | 出差报告: 起止日期、地点、目的、交通、四类费用、总结, 草稿→提交→审批 |
| 3. 客户名单可选填 | 拜访记录中客户只能从名单中选择 (支持搜索), 保证统计口径一致。销售员可"申请新增客户", 主管审核后生效; 主管可 Excel 批量导入 |
| 4. 后台统计拜访时间、客户、洽谈人员、内容等 | 「统计分析」: 按时间/销售员/客户/类型/关键字筛选; 按销售员、按客户、按类型汇总; 一键导出 Excel (明细 + 汇总三张表) |
| 5. 自然月底自动生成月度报告并分析 | 每月结束后系统自动为每位销售员生成上月报告: 拜访次数、覆盖客户、新客户、出差天数、费用、单次拜访成本、环比、A 类客户覆盖、60 天未拜访预警、周报提交率, 并自动生成文字分析 |
| 6. 审批意见 + 账户分级 | 三级角色: 销售员 / 销售主管 / 系统管理员。主管只能审批自己名下销售员; 出差报告、周报、月报均可"通过 / 退回 / 批注", 多轮意见完整保留 |

## 快速体验 (Windows)

```powershell
cd sales-report-system
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python seed_demo.py      # 生成演示数据 (会清空数据库)
.\.venv\Scripts\python app.py            # 浏览器打开 http://127.0.0.1:5000
```

演示账号 (密码均为 `demo1234`): `admin` 管理员 · `zhangjl` 销售主管 · `lixm` / `wangf` 销售员

正式使用时**不要**运行 `seed_demo.py`, 直接启动即可, 系统会自动建库并创建 `admin / admin123` (首次登录强制改密)。

运行测试: `.\.venv\Scripts\python tests\smoke_test.py`

## 部署到百度智能云 (BCC 云服务器)

1. 购买 BCC 云服务器 (1 核 2G 足够 50 人以内团队), 系统选 Ubuntu 22.04; 在安全组放行 80、443 端口。
2. 上传代码并安装:
   ```bash
   sudo apt update && sudo apt install -y python3-venv nginx
   cd /opt && sudo mkdir sales && sudo chown $USER sales && cd sales   # 把本项目文件上传到这里
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
3. 创建系统服务 `/etc/systemd/system/sales.service`:
   ```ini
   [Unit]
   Description=Sales Visit System
   After=network.target

   [Service]
   WorkingDirectory=/opt/sales
   Environment=SECRET_KEY=请替换为一串随机长字符串
   Environment=COOKIE_SECURE=1
   Environment=PORT=8000
   ExecStart=/opt/sales/.venv/bin/python serve.py
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```
   生成随机密钥: `python3 -c "import secrets;print(secrets.token_hex(32))"`
   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now sales
   ```
4. Nginx 反向代理 (`/etc/nginx/sites-available/default` 中 `location /`):
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8000;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       client_max_body_size 10m;
   }
   ```
5. **必须启用 HTTPS** (密码和客户资料在公网传输): 在百度云申请免费 SSL 证书或使用 `certbot --nginx`, 并绑定域名。
   注意: 使用中国大陆服务器 + 域名需完成 ICP 备案; 未备案前可先用 IP 测试 (此时去掉 `COOKIE_SECURE=1`)。
6. 数据备份: 数据全部在 `data/sales.db` 一个文件中, 建议每天定时备份到百度对象存储 BOS:
   ```bash
   # crontab -e
   0 2 * * * sqlite3 /opt/sales/data/sales.db ".backup /opt/sales/backup/sales-$(date +\%F).db"
   ```

公司内网也可直接部署 (Windows 服务器运行 `python serve.py`), 但外网访问需要公网 IP/端口映射, 云服务器更省事。

## 目录结构

```
app.py            全部业务逻辑与路由
schema.sql        数据库结构
serve.py          生产启动 (waitress)
seed_demo.py      演示数据
templates/        页面模板
static/style.css  样式 (适配手机)
tests/            冒烟测试
data/sales.db     数据库 (运行后自动生成)
```

## 月报自动生成规则

- 服务运行期间每小时检查一次: 若上个自然月的报告尚未生成, 则为所有在职销售员生成 (主管本人有拜访/出差时也生成)。
- 生成后状态为"已生成", 销售员可补充说明、刷新统计数据, 然后提交审批; 已提交/已通过的报告不会被覆盖。
- 主管可随时在「月度报告」页手动生成/刷新任意月份 (包括当月预览)。
