#!/usr/bin/env python3
"""
虚拟员工平台 · 全团队智能体服务器 v2
每个员工 = 独立 Agent：独立记忆 + 独立工具链 + 浏览器自动化

支持团队：
  智播咨询(8人) | 天猫电商(6人) | Amazon跨境(4人)
  Shopify(2人) | TikTok(4人) | 通用系统(48人)

使用：
  pip3 install playwright --break-system-packages && python3 -m playwright install chromium
  python3 agent-server.py
  HTML 页面 API 设 "自定义" → http://localhost:9100/chat
"""

import http.server, json, sqlite3, os, sys, time, subprocess, threading, shutil, tempfile, traceback, urllib.request, urllib.error, urllib.parse, re
from foreign_trade_sdr import dispatch_foreign_trade_task, run_foreign_trade_sop, start_foreign_trade_workflow, validate_input

PORT = int(os.environ.get("PORT", "9100"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "agents.db")
OUTPUT_DIR = os.path.join(BASE_DIR, "agent_outputs")

def env_enabled(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off")

ENABLE_SHELL = env_enabled("AGENT_ENABLE_SHELL", True)
ENABLE_BROWSER = env_enabled("AGENT_ENABLE_BROWSER", True)
ENABLE_COMPUTER = env_enabled("AGENT_ENABLE_COMPUTER", True)

AGENT_SYSTEM_PREFIX = """你是一个可以执行任务的本地 Agent，而不是只能聊天的角色。

你拥有独立记忆、角色身份和工具调用能力。你可以按需使用文件、网页、浏览器、Shell 和电脑操作工具完成任务。

工作规则：
- 先判断任务目标，再选择最少必要工具。
- 需要读取网页时优先用 web_fetch_page；需要打开给用户看的页面时用 browser_open_url。
- 需要操作本机应用、浏览器输入、快捷键或点击时，用 computer_* 工具；这可能需要 macOS 辅助功能权限。
- 执行 Shell 或 AppleScript 前要谨慎，避免删除文件、泄露密钥、付款、发布内容或发送隐私信息。
- 工具执行失败时，说明失败原因并给出下一步修复建议。
- 最终用 produce_output 提交可执行、可交付的结果。"""

# ═══════════ SQLite 持久记忆 ═══════════
def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS agent_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT NOT NULL,
        project_id TEXT DEFAULT 'default', role TEXT, content TEXT,
        created_at REAL DEFAULT (julianday('now')))""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ag ON agent_memory(agent_name, project_id)")
    db.commit(); return db

def save_memory(agent, project, role, content):
    db = sqlite3.connect(DB_PATH)
    db.execute("INSERT INTO agent_memory (agent_name,project_id,role,content) VALUES (?,?,?,?)",(agent,project,role,content))
    db.commit(); db.close()

def load_memory(agent, project='default', limit=20):
    db = sqlite3.connect(DB_PATH)
    rows = db.execute("SELECT role,content FROM agent_memory WHERE agent_name=? AND project_id=? ORDER BY id DESC LIMIT ?",(agent,project,limit)).fetchall()
    db.close(); return [{"role":r,"content":c} for r,c in reversed(rows)]

def clear_memory(agent, project='default'):
    db = sqlite3.connect(DB_PATH)
    db.execute("DELETE FROM agent_memory WHERE agent_name=? AND project_id=?",(agent,project))
    db.commit(); db.close()

# ═══════════ DeepSeek API ═══════════
PROXY = urllib.request.ProxyHandler({})
OPENER = urllib.request.build_opener(PROXY)

def call_llm(messages, api_key, model="deepseek-chat", max_tokens=1500, tools=None):
    body = {"model":model,"messages":messages,"temperature":0.7,"max_tokens":max_tokens}
    if tools: body["tools"] = tools; body["tool_choice"] = "auto"
    req = urllib.request.Request("https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json","Authorization":f"Bearer {api_key}"})
    resp = OPENER.open(req, timeout=90)
    return json.loads(resp.read().decode())

# ═══════════ 本地执行工具辅助函数 ═══════════
def _safe_workspace_path(path):
    """文件工具默认限制在项目目录，避免 Agent 误读/误写整台电脑。"""
    path = path or "."
    path = os.path.expanduser(str(path))
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    real = os.path.realpath(path)
    base = os.path.realpath(BASE_DIR)
    if real != base and not real.startswith(base + os.sep):
        raise ValueError(f"文件工具只允许访问项目目录: {BASE_DIR}")
    return real

def _truncate(text, limit=6000):
    text = "" if text is None else str(text)
    return text[:limit] + ("\n...(截断)" if len(text) > limit else "")

def _normalize_url(url):
    url = str(url or "").strip()
    if not url:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    return url

def _run_command(argv, timeout=30):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=BASE_DIR)
        out = _truncate(r.stdout, 4000)
        err = _truncate(r.stderr, 1200) if r.stderr else ""
        return f"退出码: {r.returncode}\n\n标准输出:\n{out}" + (f"\n\n标准错误:\n{err}" if err else "")
    except subprocess.TimeoutExpired:
        return f"错误：命令超时（{timeout}秒）"
    except Exception as e:
        return f"错误：{e}"

def _osascript(script, timeout=20):
    if not ENABLE_COMPUTER:
        return "错误：电脑操作工具已关闭。设置 AGENT_ENABLE_COMPUTER=1 后重启。"
    return _run_command(["osascript", "-e", script], timeout=timeout)

def _apple_string(text):
    return json.dumps(str(text or ""), ensure_ascii=False)

def _apple_modifiers(modifiers):
    allowed = {"command":"command down", "cmd":"command down", "shift":"shift down", "option":"option down", "alt":"option down", "control":"control down", "ctrl":"control down"}
    items = [allowed.get(str(m).lower()) for m in (modifiers or [])]
    items = [m for m in items if m]
    return " using {" + ", ".join(items) + "}" if items else ""

def _file_list(args):
    path = _safe_workspace_path(args.get("path", "."))
    limit = int(args.get("limit", 80) or 80)
    if os.path.isfile(path):
        return path
    rows = []
    for name in sorted(os.listdir(path))[:limit]:
        fp = os.path.join(path, name)
        kind = "dir" if os.path.isdir(fp) else "file"
        size = os.path.getsize(fp) if os.path.isfile(fp) else 0
        rows.append(f"{kind}\t{size}\t{name}")
    return "\n".join(rows) if rows else "空目录"

def _file_read(args):
    path = _safe_workspace_path(args.get("path", ""))
    limit = int(args.get("limit", 12000) or 12000)
    if not os.path.isfile(path):
        return f"错误：不是文件: {path}"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return _truncate(f.read(), limit)

# ═══════════ 工具定义 ═══════════
def get_tools(agent_role):
    """根据 Agent 角色返回可用的工具集"""
    base_tools = [
        {"type":"function","function":{"name":"think","description":"内部推理——规划下一步、分析问题","parameters":{"type":"object","properties":{"reasoning":{"type":"string","description":"推理过程"}},"required":["reasoning"]}}},
        {"type":"function","function":{"name":"produce_output","description":"提交最终工作成果","parameters":{"type":"object","properties":{"content":{"type":"string","description":"完整工作成果"}},"required":["content"]}}},
        {"type":"function","function":{"name":"list_files","description":"列出项目目录内的文件。默认只允许访问当前项目目录，适合先盘点代码或输出文件。","parameters":{"type":"object","properties":{"path":{"type":"string","description":"相对项目目录的路径，默认 ."},"limit":{"type":"integer","description":"最多返回多少项，默认80"}},"required":[]}}},
        {"type":"function","function":{"name":"read_file","description":"读取项目目录内的文本文件内容。默认只允许访问当前项目目录。","parameters":{"type":"object","properties":{"path":{"type":"string","description":"相对项目目录的文件路径"},"limit":{"type":"integer","description":"最多返回字符数，默认12000"}},"required":["path"]}}},
        {"type":"function","function":{"name":"web_search_live","description":"搜索实时信息","parameters":{"type":"object","properties":{"query":{"type":"string","description":"搜索关键词"},"num_results":{"type":"integer","description":"返回结果数，默认5"}},"required":["query"]}}},
        {"type":"function","function":{"name":"web_fetch_page","description":"抓取网页内容","parameters":{"type":"object","properties":{"url":{"type":"string","description":"完整URL"}},"required":["url"]}}},
        {"type":"function","function":{"name":"save_file","description":"保存文件到本地","parameters":{"type":"object","properties":{"filename":{"type":"string","description":"文件名"},"content":{"type":"string","description":"文件内容"}},"required":["filename","content"]}}},
    ]
    if ENABLE_SHELL:
        base_tools.append({"type":"function","function":{"name":"run_shell","description":"执行本地 shell 命令，工作目录为项目目录。适合安装依赖、运行脚本、检查文件、生成产物。危险操作前必须谨慎。","parameters":{"type":"object","properties":{"command":{"type":"string","description":"要执行的命令"},"timeout":{"type":"integer","description":"超时时间秒数，默认30，最大120"}},"required":["command"]}}})
    if ENABLE_BROWSER:
        base_tools.extend([
            {"type":"function","function":{"name":"browser_open_url","description":"在本机浏览器中打开 URL 给用户查看。不会读取页面内容；读取内容请用 web_fetch_page。","parameters":{"type":"object","properties":{"url":{"type":"string","description":"要打开的网址"},"app":{"type":"string","description":"可选浏览器应用名，如 Google Chrome、Safari、Arc"}},"required":["url"]}}},
            {"type":"function","function":{"name":"browser_search_web","description":"在本机浏览器中打开搜索结果页。","parameters":{"type":"object","properties":{"query":{"type":"string","description":"搜索关键词"},"app":{"type":"string","description":"可选浏览器应用名，如 Google Chrome、Safari、Arc"}},"required":["query"]}}},
        ])
    if ENABLE_COMPUTER:
        base_tools.extend([
            {"type":"function","function":{"name":"computer_open_app","description":"打开或激活 macOS 应用。","parameters":{"type":"object","properties":{"app":{"type":"string","description":"应用名，如 Finder、Google Chrome、Safari、Notes"}},"required":["app"]}}},
            {"type":"function","function":{"name":"computer_type_text","description":"向当前前台应用输入文字，可选先激活某个应用。需要 macOS 辅助功能权限。","parameters":{"type":"object","properties":{"text":{"type":"string","description":"要输入的文字"},"app":{"type":"string","description":"可选，先激活的应用名"}},"required":["text"]}}},
            {"type":"function","function":{"name":"computer_press_key","description":"向当前前台应用发送按键或快捷键。需要 macOS 辅助功能权限。","parameters":{"type":"object","properties":{"key":{"type":"string","description":"按键，如 return、tab、escape、delete、left、right、up、down，或单个字符如 l"},"modifiers":{"type":"array","items":{"type":"string"},"description":"修饰键数组，如 [\"command\"]、[\"command\",\"shift\"]"},"app":{"type":"string","description":"可选，先激活的应用名"}},"required":["key"]}}},
            {"type":"function","function":{"name":"computer_click","description":"按屏幕坐标点击。需要 macOS 辅助功能权限。","parameters":{"type":"object","properties":{"x":{"type":"integer","description":"屏幕 X 坐标"},"y":{"type":"integer","description":"屏幕 Y 坐标"}},"required":["x","y"]}}},
            {"type":"function","function":{"name":"computer_run_applescript","description":"执行 AppleScript 操作本机应用、窗口、菜单、键盘或浏览器。只在必要时使用。","parameters":{"type":"object","properties":{"script":{"type":"string","description":"AppleScript 源码"},"timeout":{"type":"integer","description":"超时时间秒数，默认20，最大60"}},"required":["script"]}}},
        ])
    # Shopify Agent 专属工具 —— 直接读写 Shopify Admin API
    if "Shopify" in str(agent_role) or "品牌增长" in str(agent_role) or "运营自动化" in str(agent_role):
        base_tools.extend([
            {"type":"function","function":{"name":"shopify_get_orders","description":"获取 Shopify 店铺订单列表（今天/昨天/指定日期范围）","parameters":{"type":"object","properties":{"status":{"type":"string","description":"订单状态: any/open/closed/cancelled，默认 any"},"limit":{"type":"integer","description":"返回条数，默认10，最大50"},"created_at_min":{"type":"string","description":"起始日期 ISO格式，如 2026-01-01"}},"required":[]}}},
            {"type":"function","function":{"name":"shopify_get_products","description":"获取 Shopify 店铺产品列表","parameters":{"type":"object","properties":{"limit":{"type":"integer","description":"返回条数，默认10"},"status":{"type":"string","description":"active/archived/draft，默认 active"}},"required":[]}}},
            {"type":"function","function":{"name":"shopify_get_inventory","description":"查询指定产品的库存水平","parameters":{"type":"object","properties":{"product_id":{"type":"string","description":"产品ID，从 shopify_get_products 返回的 id 字段"}},"required":["product_id"]}}},
            {"type":"function","function":{"name":"shopify_get_customers","description":"获取 Shopify 店铺客户列表","parameters":{"type":"object","properties":{"limit":{"type":"integer","description":"返回条数，默认10"}},"required":[]}}},
            {"type":"function","function":{"name":"shopify_get_analytics","description":"获取店铺核心经营数据摘要（GMV/订单数/客单价/新客数）","parameters":{"type":"object","properties":{"period":{"type":"string","description":"today/yesterday/last_7_days/last_30_days，默认 today"}},"required":[]}}},
            {"type":"function","function":{"name":"shopify_update_product","description":"更新产品信息（标题、描述、标签、价格等）","parameters":{"type":"object","properties":{"product_id":{"type":"string","description":"产品ID"},"title":{"type":"string","description":"新产品标题（可选）"},"body_html":{"type":"string","description":"新产品描述HTML（可选）"},"tags":{"type":"string","description":"新标签，逗号分隔（可选）"},"vendor":{"type":"string","description":"供应商（可选）"}},"required":["product_id"]}}},
        ])
    return base_tools

# ═══════════ 工具执行器 ═══════════
def execute_tool(name, args_str, agent_name, project_id):
    """执行工具调用并返回结果"""
    try:
        args = json.loads(args_str)
    except:
        args = {}

    if name == "think":
        return f"思考已记录。继续行动。"

    if name == "produce_output":
        return f"最终成果已提交。"

    if name == "list_files":
        try:
            return _file_list(args)
        except Exception as e:
            return f"列文件失败: {e}"

    if name == "read_file":
        try:
            return _file_read(args)
        except Exception as e:
            return f"读文件失败: {e}"

    if name == "save_file":
        fn = args.get("filename","output.txt")
        ct = args.get("content","")
        d = os.path.join(OUTPUT_DIR, agent_name, project_id)
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, fn)
        fp = os.path.realpath(fp)
        if not fp.startswith(os.path.realpath(d) + os.sep) and fp != os.path.realpath(d):
            return "错误：文件名不能跳出 Agent 输出目录"
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f: f.write(ct)
        return f"文件已保存: {fp}\n大小: {len(ct)} 字符"

    if name == "run_shell":
        if not ENABLE_SHELL:
            return "错误：Shell 工具已关闭。设置 AGENT_ENABLE_SHELL=1 后重启。"
        cmd = args.get("command","")
        if not cmd: return "错误：缺少 command 参数"
        try:
            timeout = min(int(args.get("timeout", 30) or 30), 120)
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=BASE_DIR)
            out = _truncate(r.stdout, 4000)
            err = _truncate(r.stderr, 1200) if r.stderr else ""
            return f"退出码: {r.returncode}\n\n标准输出:\n{out}" + (f"\n\n标准错误:\n{err}" if err else "")
        except subprocess.TimeoutExpired:
            return "错误：命令超时"
        except Exception as e:
            return f"错误：{e}"

    if name == "browser_open_url":
        if not ENABLE_BROWSER:
            return "错误：浏览器工具已关闭。设置 AGENT_ENABLE_BROWSER=1 后重启。"
        url = _normalize_url(args.get("url",""))
        if not url: return "错误：缺少 url 参数"
        cmd = ["open"]
        app = str(args.get("app","")).strip()
        if app: cmd.extend(["-a", app])
        cmd.append(url)
        result = _run_command(cmd, timeout=10)
        return f"已请求浏览器打开: {url}\n{result}"

    if name == "browser_search_web":
        if not ENABLE_BROWSER:
            return "错误：浏览器工具已关闭。设置 AGENT_ENABLE_BROWSER=1 后重启。"
        query = args.get("query","")
        if not query: return "错误：缺少 query 参数"
        url = "https://duckduckgo.com/?q=" + urllib.parse.quote(str(query))
        cmd = ["open"]
        app = str(args.get("app","")).strip()
        if app: cmd.extend(["-a", app])
        cmd.append(url)
        result = _run_command(cmd, timeout=10)
        return f"已打开搜索: {query}\nURL: {url}\n{result}"

    if name == "computer_open_app":
        if not ENABLE_COMPUTER:
            return "错误：电脑操作工具已关闭。设置 AGENT_ENABLE_COMPUTER=1 后重启。"
        app = str(args.get("app","")).strip()
        if not app: return "错误：缺少 app 参数"
        return _run_command(["open", "-a", app], timeout=10)

    if name == "computer_type_text":
        text = args.get("text","")
        app = str(args.get("app","")).strip()
        lines = []
        if app:
            lines.append(f'tell application {_apple_string(app)} to activate')
            lines.append("delay 0.2")
        lines.append(f'tell application "System Events" to keystroke {_apple_string(text)}')
        return _osascript("\n".join(lines), timeout=20)

    if name == "computer_press_key":
        key = str(args.get("key","")).strip().lower()
        if not key: return "错误：缺少 key 参数"
        app = str(args.get("app","")).strip()
        modifiers = _apple_modifiers(args.get("modifiers", []))
        key_codes = {"return":36, "enter":36, "tab":48, "space":49, "delete":51, "backspace":51, "escape":53, "esc":53, "left":123, "right":124, "down":125, "up":126}
        lines = []
        if app:
            lines.append(f'tell application {_apple_string(app)} to activate')
            lines.append("delay 0.2")
        if key in key_codes:
            lines.append(f'tell application "System Events" to key code {key_codes[key]}{modifiers}')
        else:
            lines.append(f'tell application "System Events" to keystroke {_apple_string(key[:1])}{modifiers}')
        return _osascript("\n".join(lines), timeout=20)

    if name == "computer_click":
        x = int(args.get("x", 0) or 0)
        y = int(args.get("y", 0) or 0)
        if x <= 0 or y <= 0: return "错误：缺少有效 x/y 坐标"
        return _osascript(f'tell application "System Events" to click at {{{x}, {y}}}', timeout=10)

    if name == "computer_run_applescript":
        script = args.get("script","")
        if not script: return "错误：缺少 script 参数"
        timeout = min(int(args.get("timeout", 20) or 20), 60)
        return _osascript(script, timeout=timeout)

    if name == "web_search_live":
        query = args.get("query","")
        n = min(args.get("num_results",5), 10)
        if not query: return "错误：缺少 query 参数"
        # 尝试用 Python 直接搜索
        try:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            resp = OPENER.open(req, timeout=15)
            html = resp.read().decode()
            # 简单提取结果
            results = []
            for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>', html):
                if len(results) >= n: break
                results.append(f"- [{m.group(2).strip()}]({m.group(1)})")
            return "\n".join(results) if results else f"搜索 '{query}' 未找到结果（可能是网络限制）。请尝试用 web_fetch_page 直接访问已知URL。"
        except Exception as e:
            return f"搜索失败: {e}。可能被网络代理拦截。请使用 web_fetch_page 直接访问已知URL。"

    if name == "web_fetch_page":
        url = args.get("url","")
        if not url: return "错误：缺少 url 参数"
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            resp = OPENER.open(req, timeout=15)
            html = resp.read().decode()
            # 提取文本
            text = re.sub(r'<script[^>]*>.*?</script>','',html,flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>','',text,flags=re.DOTALL)
            text = re.sub(r'<[^>]+>',' ',text)
            text = re.sub(r'\s+',' ',text).strip()
            return text[:5000] + ("...(截断)" if len(text)>5000 else "")
        except Exception as e:
            return f"抓取失败: {e}"

    if name.startswith("shopify_"):
        return _exec_shopify(name, args, project_id)

    return f"未知工具: {name}"

# ═══════════ Shopify Admin API ═══════════
SHOPIFY_CREDS = {}

def _shopify_config():
    """读取 Shopify 凭证（从环境变量或配置文件）"""
    global SHOPIFY_CREDS
    if SHOPIFY_CREDS: return SHOPIFY_CREDS
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shopify_config.json")
    try:
        with open(config_path) as f:
            SHOPIFY_CREDS = json.load(f)
    except:
        SHOPIFY_CREDS = {
            "store": os.environ.get("SHOPIFY_STORE", ""),
            "token": os.environ.get("SHOPIFY_ADMIN_TOKEN", ""),
        }
    return SHOPIFY_CREDS

def _shopify_api(path, method="GET", data=None, project_id="default"):
    """调用 Shopify Admin API"""
    creds = _shopify_config()
    store = creds.get("store", "").strip()
    token = creds.get("token", "").strip()
    if not store or not token:
        return {"error": "Shopify 未配置。请在 agent-server.py 同目录创建 shopify_config.json，格式: {\"store\":\"your-store.myshopify.com\",\"token\":\"shpat_xxx\"}"}
    url = f"https://{store}/admin/api/2024-01/{path}"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    try:
        req = urllib.request.Request(url, headers=headers, method=method)
        if data:
            req.data = json.dumps(data).encode()
        resp = OPENER.open(req, timeout=30)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"Shopify API {e.code}: {e.reason}", "detail": e.read().decode()[:500]}
    except Exception as e:
        return {"error": str(e)}

def _exec_shopify(name, args, project_id):
    """执行 Shopify API 工具调用"""
    if name == "shopify_get_orders":
        params = []
        if args.get("status"): params.append(f"status={args['status']}")
        if args.get("created_at_min"): params.append(f"created_at_min={args['created_at_min']}")
        limit = min(args.get("limit", 10), 50)
        params.append(f"limit={limit}")
        qs = "&".join(params)
        r = _shopify_api(f"orders.json?{qs}", project_id=project_id)
        if isinstance(r, dict) and r.get("error"):
            return json.dumps(r)
        orders = r.get("orders", [])
        summary = [{"id": o["id"], "name": o["name"], "total": o.get("total_price","?"), "currency": o.get("currency",""), "created": o.get("created_at","")[:10], "financial_status": o.get("financial_status",""), "fulfillment_status": o.get("fulfillment_status","pending")} for o in orders]
        total_gmv = sum(float(o.get("total_price",0) or 0) for o in orders)
        return f"找到 {len(orders)} 个订单，合计 {total_gmv:.2f}:\n"+json.dumps(summary, ensure_ascii=False, indent=2)

    if name == "shopify_get_products":
        limit = min(args.get("limit", 10), 50)
        status = args.get("status", "active")
        r = _shopify_api(f"products.json?limit={limit}&status={status}", project_id=project_id)
        if isinstance(r, dict) and r.get("error"): return json.dumps(r)
        products = r.get("products", [])
        summary = [{"id": p["id"], "title": p["title"], "vendor": p.get("vendor",""), "product_type": p.get("product_type",""), "status": p.get("status",""), "variants_count": len(p.get("variants",[]))} for p in products]
        return json.dumps(summary, ensure_ascii=False, indent=2)

    if name == "shopify_get_inventory":
        pid = args.get("product_id", "")
        if not pid: return json.dumps({"error": "缺少 product_id"})
        # Get inventory levels via inventory_items
        r = _shopify_api(f"products/{pid}.json", project_id=project_id)
        if isinstance(r, dict) and r.get("error"): return json.dumps(r)
        product = r.get("product", {})
        variants = product.get("variants", [])
        inv = []
        for v in variants:
            inv.append({"variant_id": v["id"], "title": v.get("title",""), "sku": v.get("sku",""), "price": v.get("price",""), "inventory_quantity": v.get("inventory_quantity",0), "inventory_item_id": v.get("inventory_item_id","")})
        return f"产品「{product.get('title','?')}」库存:\n"+json.dumps(inv, ensure_ascii=False, indent=2)

    if name == "shopify_get_customers":
        limit = min(args.get("limit", 10), 50)
        r = _shopify_api(f"customers.json?limit={limit}", project_id=project_id)
        if isinstance(r, dict) and r.get("error"): return json.dumps(r)
        customers = r.get("customers", [])
        summary = [{"id": c["id"], "email": c.get("email","?"), "first_name": c.get("first_name",""), "last_name": c.get("last_name",""), "orders_count": c.get("orders_count",0), "total_spent": c.get("total_spent","0"), "state": c.get("state","")} for c in customers]
        return json.dumps(summary, ensure_ascii=False, indent=2)

    if name == "shopify_get_analytics":
        period = args.get("period", "today")
        now = time.strftime("%Y-%m-%d")
        created_min = now
        if period == "yesterday":
            created_min = time.strftime("%Y-%m-%d", time.localtime(time.time()-86400))
            now = created_min
        elif period == "last_7_days":
            created_min = time.strftime("%Y-%m-%d", time.localtime(time.time()-7*86400))
        elif period == "last_30_days":
            created_min = time.strftime("%Y-%m-%d", time.localtime(time.time()-30*86400))
        r = _shopify_api(f"orders.json?status=any&created_at_min={created_min}&limit=250&financial_status=paid", project_id=project_id)
        if isinstance(r, dict) and r.get("error"): return json.dumps(r)
        orders = r.get("orders", [])
        total_gmv = sum(float(o.get("total_price",0) or 0) for o in orders)
        unique_customers = len(set(o.get("email","") for o in orders if o.get("email")))
        open_count = sum(1 for o in orders if o.get("fulfillment_status") != "fulfilled")
        avg_value = total_gmv / len(orders) if orders else 0
        return json.dumps({
            "period": period,
            "date_range": f"{created_min} ~ {now}",
            "total_orders": len(orders),
            "total_gmv": f"{total_gmv:.2f}",
            "avg_order_value": f"{avg_value:.2f}",
            "unique_customers": unique_customers,
            "pending_fulfillment": open_count
        }, ensure_ascii=False, indent=2)

    if name == "shopify_update_product":
        pid = args.get("product_id", "")
        if not pid: return json.dumps({"error": "缺少 product_id"})
        data = {"product": {"id": int(pid)}}
        for key in ["title","body_html","tags","vendor"]:
            if args.get(key): data["product"][key] = args[key]
        r = _shopify_api(f"products/{pid}.json", method="PUT", data=data, project_id=project_id)
        return json.dumps(r, ensure_ascii=False, indent=2)

    return f"未知 Shopify 工具: {name}"

# ═══════════ Agent 角色定义 ═══════════
AGENTS = {}

def _a(name, system, role, team="通用"):
    AGENTS[name] = {"system": system, "role": role, "team": team}

# ── 智播营销咨询 (8人) ──
_a("黄安琪","你是「黄安琪」，智播营销咨询CEO。10年线上增长实战。务实但不急，听完各方再拍板。","CEO","智播咨询")
_a("徐佳","你是「徐佳」，智播首席直播操盘手。投放消耗1亿+。数据驱动，不对创意妥协。说数字，不绕弯。","直播操盘手","智播咨询")
_a("农历卷","你是「农历卷」，智播短视频/IP操盘手。10年内容营销。讲案例讲情绪，相信好创意需要时间。","短视频操盘手","智播咨询")
_a("刘鑫梅","你是「刘鑫梅」，智播直播团队孵化教练。擅长从0搭建直播团队。接地气说人话，带实操细节。","团队孵化教练","智播咨询")
_a("余富强","你是「余富强」，智播品牌战略顾问。18年品牌管理。做增长不能伤品牌。语速慢用词重。","品牌战略顾问","智播咨询")
_a("Amber","你是「Amber」，智播组织能力诊断师。13年组织建设。流程-权责-激励是你的框架。结构化输出。","组织诊断师","智播咨询")
_a("Irene","你是「Irene」，智播品牌整合营销专家。前4A创意总监。非常polished，谈Brand Love。","品牌营销专家","智播咨询")
_a("Jamie","你是「Jamie」，智播内容工业化专家。管理100+账号矩阵，AI自动化提效200%。效率至上。","内容效率专家","智播咨询")

# ── 天猫全域电商 (6人) ──
_a("电商运营总监","你是天猫店铺运营总监。90天落地时间表、四阶段爆款打造、先转化后流量。","运营总监","天猫电商")
_a("电商视觉设计师","你是天猫视觉设计师。首页7屏+主图5张+详情页10步。主图点击率≥5%是底线。","视觉设计师","天猫电商")
_a("电商文案策划师","你是天猫文案策划师。五维卖点+关键词策略+客服话术库+短视频脚本。核心词+属性词+场景词。","文案策划师","天猫电商")
_a("电商拍摄剪辑师","你是天猫拍摄剪辑师。每款产品50+张图+10+条视频+3条安装教程。多平台素材复用。","拍摄剪辑师","天猫电商")
_a("电商投流师","你是天猫投流专员。直通车/万相台/引力魔方/淘宝客。四阶段投放，ROI≥3。先测词后放大。","投流师","天猫电商")
_a("电商客服私域运营","你是天猫客服与私域运营。首响≤30秒，用户四层分层，售后问题库反哺详情页。","客服私域运营","天猫电商")

# ── Amazon 跨境 (4人) ──
_a("Amazon运营操盘手","你是Amazon运营操盘手。AI已接管60%执行。你做战略决策：广告预算/竞品应对/促销节奏/Listing方向。","运营操盘手","Amazon跨境")
_a("跨境供应链专员","你是跨境供应链专员。AI预警后做决策。安全库存=日均×(头程+30天)。断货37天=权重归零。","供应链专员","Amazon跨境")
_a("Listing与内容优化师","你是Listing优化师。不是翻译是本地化。竞品差评=你的卖点金矿。A+三件套提高转化10-20%。","内容优化师","Amazon跨境")
_a("合规与经营分析师","你是合规经营分析师。AI不能碰的三条底线：核心信息/批量操作/申诉。每SKU上架前三问。","合规分析师","Amazon跨境")

# ── Shopify 独立站 (2人) ──
_a("品牌与增长负责人","你是Shopify品牌增长负责人。Sidekick已接管80%执行。你只做AI做不了的：品牌定位/创意方向/渠道判断/内容策略。","品牌增长负责人","Shopify")
_a("运营与自动化负责人","你是Shopify运营自动化负责人。用自然语言驾驶Sidekick：建Flow/建App/查报表/设预警。面对机器效率，面对人共情。","运营自动化负责人","Shopify")

# ── TikTok Shop (4人) ──
_a("短视频创作运营","你是TikTok短视频创作运营。每日3-5条+前3秒钩子=一切。15-60秒黄金时长。不要硬广要原生感。","短视频运营","TikTok")
_a("直播运营操盘","你是TikTok直播运营操盘手。AI虚拟主播720小时/月。排品顺序=GMV。Stacked Stream多时区策略。","直播操盘","TikTok")
_a("达人建联管理","你是TikTok达人建联管理。AI搜350万达人+自动DM1000条/天。腰部达人ROI>头部。15天结算周期。","达人建联","TikTok")
_a("投流增长优化","你是TikTok投流增长优化。素材不行投再多也烧。直播间投流≠短视频投流。核算到最终ROAS。","投流增长","TikTok")

# ── AI 外贸获客小队 ──
_a("外贸SOP总控","你是外贸获客主管。你负责把工厂资料转成ICP客户画像、目标市场策略、获客任务、交付节奏和风险边界。你不做空泛报告，只推动客户开发结果：线索、评分、开发信、跟进计划和老板摘要。所有发送动作必须人工确认。","外贸获客主管","AI外贸")
_a("海外线索搜索员","你是海外获客员工。你负责生成搜索词、打开搜索页、寻找海外B端客户、过滤C端零售和平台商品页，只保留进口商、经销商、批发商、品牌商、多门店渠道等候选。你必须保留来源链接，不能编造联系人或邮箱。","海外获客员工","AI外贸")
_a("客户背调员","你是客户研究与评分员工。你负责读取官网和公开资料，整理企业画像、主营品类、业务区域、采购潜力、风险项，并按A/B/C给出跟进优先级。读不到的信息必须写待验证。","客户研究评分员工","AI外贸")
_a("英文开发信专员","你是开发内容员工。你负责根据客户画像生成英文开发信、LinkedIn私信、社媒获客内容和产品切入角度。你拒绝垃圾邮件话术，不夸大认证、价格、交期或效果。","开发内容员工","AI外贸")
_a("外贸跟进SOP专员","你是跟进与交付员工。你负责为A/B类客户生成30天跟进节奏、下一步动作、人工确认提醒、合规边界和Excel交付包。你不做自动群发。","跟进交付员工","AI外贸")

print(f"已加载 {len(AGENTS)} 个 Agent（{len(set(a['team'] for a in AGENTS.values()))} 个团队）")

# ═══════════ HTTP 服务器 ═══════════
class AgentServer(http.server.BaseHTTPRequestHandler):
    api_key = ""

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","POST,GET,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type,Authorization")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/teams":
            teams = {}
            for n,c in AGENTS.items():
                t = c["team"]
                if t not in teams: teams[t] = []
                teams[t].append({"name":n,"role":c["role"]})
            self._json(200, {"teams": teams, "total": len(AGENTS)})
        elif path == "/tools":
            tools = [t["function"]["name"] for t in get_tools("")]
            self._json(200, {
                "tools": tools,
                "enabled": {
                    "shell": ENABLE_SHELL,
                    "browser": ENABLE_BROWSER,
                    "computer": ENABLE_COMPUTER
                },
                "workspace": BASE_DIR,
                "output_dir": OUTPUT_DIR
            })
        elif path.startswith("/foreign-trade/export/"):
            return self._foreign_trade_export(path)
        elif path.startswith("/dashboard"):
            # 返回指定团队的最新巡检报告
            team = path.split("/")[-1] if "/" in path else ""
            if team and team in MONITOR_DATA:
                self._json(200, MONITOR_DATA[team])
            elif team:
                self._json(200, {"report":"尚未巡检，请先 POST /monitor","alerts":[],"team":team})
            else:
                self._json(200, {"teams":list(MONITOR_DATA.keys()),"alerts":MONITOR_ALERTS[-10:]})
        else:
            self._json(200, {"ok":True,"agents":list(AGENTS.keys()),"count":len(AGENTS)})

    def do_DELETE(self):
        parts = self.path.split("/")
        if len(parts) >= 3 and parts[1] == "memory":
            agent = parts[2]; project = parts[3] if len(parts) > 3 else "default"
            clear_memory(agent, project)
            self._json(200, {"cleared":True,"agent":agent})

    def do_POST(self):
        cl = int(self.headers.get("Content-Length",0))
        body = json.loads(self.rfile.read(cl)) if cl > 0 else {}
        auth = self.headers.get("Authorization","")
        if auth.startswith("Bearer "): self.api_key = auth[7:]

        if self.path == "/chat": return self._chat(body)
        if self.path == "/batch": return self._batch(body)
        if self.path == "/memory": return self._memory(body)
        if self.path == "/monitor": return self._monitor(body)
        if self.path == "/foreign-trade/validate": return self._foreign_trade_validate(body)
        if self.path == "/foreign-trade/dispatch": return self._foreign_trade_dispatch(body)
        if self.path == "/foreign-trade/workflow/start": return self._foreign_trade_workflow_start(body)
        if self.path == "/foreign-trade/run": return self._foreign_trade_run(body)
        self._json(404, {"error":"Not found"})

    def _run_agent(self, agent, msg, project="default", max_tokens=1500):
        cfg = AGENTS[agent]
        tools = get_tools(cfg["role"])
        memory = load_memory(agent, project)
        messages = [{"role":"system","content":AGENT_SYSTEM_PREFIX + "\n\n" + cfg["system"]}]
        messages.extend(memory)
        messages.append({"role":"user","content":msg})
        save_memory(agent, project, "user", msg)

        # Agent loop — 最多5轮工具调用
        thinking = []
        final_reply = ""
        for iteration in range(5):
            try:
                result = call_llm(messages, self.api_key, max_tokens=max_tokens, tools=tools)
                cm = result["choices"][0]["message"]

                if "tool_calls" in cm and cm["tool_calls"]:
                    messages.append({"role":"assistant","content":cm.get("content") or None,"tool_calls":cm["tool_calls"]})
                    for tc in cm["tool_calls"]:
                        fn = tc["function"]["name"]
                        args = tc["function"]["arguments"]
                        if fn == "think":
                            try: thinking.append(json.loads(args).get("reasoning",""))
                            except: thinking.append(str(args))
                        if fn == "produce_output":
                            try: final_reply = json.loads(args).get("content","")
                            except: final_reply = str(args)
                            break
                        tr = execute_tool(fn, args, agent, project)
                        messages.append({"role":"tool","tool_call_id":tc["id"],"content":tr})
                        print(f"  🔧 {agent} → {fn}: {tr[:80]}...")
                    if final_reply: break
                    continue
                else:
                    final_reply = cm.get("content","")
                    break
            except Exception as e:
                final_reply = f"❌ API错误: {e}"; break

        if not final_reply:
            final_reply = "未能完成任务。" + ("\n\n思考过程:\n"+"\n".join(f"{i+1}. {t}" for i,t in enumerate(thinking)) if thinking else "")

        save_memory(agent, project, "assistant", final_reply)
        return {"agent":agent,"reply":final_reply,"thinking":thinking,"role":cfg["role"],"team":cfg["team"]}

    def _chat(self, body):
        agent = body.get("agent",""); msg = body.get("message",""); project = body.get("project","default")
        max_tokens = body.get("max_tokens", 1500)

        if not agent or not msg: return self._json(400,{"error":"Missing agent/message"})
        if not self.api_key: return self._json(401,{"error":"API Key required"})
        if agent not in AGENTS: return self._json(404,{"error":f"Unknown: {agent}"})

        self._json(200, self._run_agent(agent, msg, project, max_tokens))

    def _batch(self, body):
        agents = body.get("agents",[]); msg = body.get("message",""); project = body.get("project","default")
        max_tokens = body.get("max_tokens", 1500)
        if not agents: return self._json(400,{"error":"Missing agents"})
        if not self.api_key: return self._json(401,{"error":"API Key required"})
        results = {}
        for agent in agents:
            if agent not in AGENTS: results[agent]={"error":"Unknown"}; continue
            try:
                results[agent] = self._run_agent(agent, msg, project, max_tokens)
            except Exception as e:
                results[agent] = {"error":str(e)}
        self._json(200, {"results":results})

    def _memory(self, body):
        agent = body.get("agent",""); project = body.get("project","default")
        if agent not in AGENTS: return self._json(404,{"error":"Unknown"})
        self._json(200, {"agent":agent,"memory":load_memory(agent,project)})

    def _monitor(self, body):
        """手动触发运营巡检"""
        team = body.get("team","")
        if not team or team not in MONITOR_TASKS:
            return self._json(400, {"error":"Missing/unknown team","available":list(MONITOR_TASKS.keys())})
        if not self.api_key:
            return self._json(401, {"error":"API Key required"})
        result = run_monitor_check(team, self.api_key)
        self._json(200, result)

    def _foreign_trade_validate(self, body):
        missing = validate_input(body)
        self._json(200, {"ok": not missing, "missing": missing})

    def _foreign_trade_dispatch(self, body):
        result = dispatch_foreign_trade_task(
            body,
            BASE_DIR,
            opener=OPENER,
            live_search=env_enabled("FOREIGN_TRADE_LIVE_SEARCH", True),
            agent_tools=self._foreign_trade_agent_tools(body),
        )
        self._json(200 if result.get("ok") or result.get("need_info") else 400, result)

    def _foreign_trade_workflow_start(self, body):
        result = start_foreign_trade_workflow(
            body,
            BASE_DIR,
            opener=OPENER,
            live_search=env_enabled("FOREIGN_TRADE_LIVE_SEARCH", True),
            agent_tools=self._foreign_trade_agent_tools(body),
        )
        self._json(200 if result.get("ok") or result.get("need_info") else 400, result)

    def _foreign_trade_run(self, body):
        # 默认不调用付费 LLM；公开搜索失败时自动生成待验证线索位。
        result = run_foreign_trade_sop(
            body,
            BASE_DIR,
            opener=OPENER,
            live_search=env_enabled("FOREIGN_TRADE_LIVE_SEARCH", True),
            agent_tools=self._foreign_trade_agent_tools(body),
        )
        self._json(200 if result.get("ok") else 400, result)

    def _foreign_trade_agent_tools(self, body):
        tools = body.get("agent_tools") or {}
        return {
            "shell_enabled": ENABLE_SHELL,
            "browser_enabled": ENABLE_BROWSER,
            "computer_enabled": ENABLE_COMPUTER,
            "open_browser": bool(tools.get("open_browser") or tools.get("openBrowser")),
            "browser_app": str(tools.get("browser_app") or tools.get("browserApp") or "").strip(),
        }

    def _foreign_trade_export(self, path):
        run_id = path.rstrip("/").split("/")[-1]
        if not re.match(r"^ft_\d{8}_\d{6}_[a-f0-9]{6}$", run_id):
            return self._json(400, {"error": "Invalid run_id"})
        fp = os.path.join(OUTPUT_DIR, "foreign_trade", run_id, "外贸客户开发SOP数据包.xlsx")
        if not os.path.isfile(fp):
            return self._json(404, {"error": "Export not found"})
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f'attachment; filename="{urllib.parse.quote(os.path.basename(fp))}"')
        self.send_header("Content-Length", str(os.path.getsize(fp)))
        self.end_headers()
        with open(fp, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Content-Type","application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
    def log_message(self, fmt, *args): pass  # 静默模式


# ═══════════ 运营监控引擎 ═══════════
# 电商公司的核心交付物不是报告，而是持续的监测+告警+行动
# 这个引擎模拟了"AI Agent 每天在后台自动巡检"的能力

MONITOR_TASKS = {
    "Amazon": {
        "agents": ["Amazon运营操盘手", "跨境供应链专员", "Listing与内容优化师", "合规与经营分析师"],
        "schedule_hours": 4,  # 每4小时巡检一次
        "checklist": [
            "订单与GMV：昨日订单数/GMV/ACOS/BSR排名变化",
            "广告表现：SP/SB/SD各campaign的ACOS和ROAS",
            "库存安全：FBA可售天数<30天的SKU、在途货件状态",
            "账号健康：ODR/A-to-Z/绩效通知",
            "竞品动态：核心竞品价格变化/BSR波动/差评突增",
            "差评监控：新增差评内容分析、是否需要修改Listing",
            "合规检查：变体违规/侵权风险/认证到期提醒"
        ]
    },
    "Shopify": {
        "agents": ["品牌与增长负责人", "运营与自动化负责人"],
        "schedule_hours": 6,
        "checklist": [
            "GMV与转化：昨日GMV/转化率/客单价/弃购率",
            "流量来源：各渠道（搜索/社媒/直接/邮件）流量占比和转化率",
            "弃购分析：弃购率变化/弃购原因Top3/弃购挽回率",
            "邮件表现：最新Campaign的打开率/点击率/转化率",
            "库存预警：低库存产品、SLOW-MOVING库存",
            "AI Referred Orders：ChatGPT/Copilot渠道订单量和转化率",
            "Sidekick Pulse 告警：未处理的高优先级预警"
        ]
    },
    "TikTok": {
        "agents": ["短视频创作运营", "直播运营操盘", "达人建联管理", "投流增长优化"],
        "schedule_hours": 4,
        "checklist": [
            "短视频数据：昨日发布视频的播放量/完播率/互动率/带货GMV",
            "直播数据：最新一场直播的场观/峰值在线/GMV/转化率",
            "达人合作：本周新建联达人/待审核内容/佣金结算状态",
            "投流ROAS：各campaign的消耗/GMV/ROAS",
            "内容排期：未来3天的短视频选题和直播排品"
        ]
    }
}

# 监控数据存储（模拟 - 实际应接真实API）
MONITOR_DATA = {}
MONITOR_ALERTS = []

def run_monitor_check(team_name, api_key):
    """运行一次运营巡检——让团队的Agent自动分析当前状态并生成告警"""
    if team_name not in MONITOR_TASKS:
        return {"error": f"未知团队: {team_name}"}

    cfg = MONITOR_TASKS[team_name]
    checklist_text = "\n".join(f"- {c}" for c in cfg["checklist"])

    # 让团队中排名第一的Agent做巡检总结
    lead_agent = cfg["agents"][0]
    if lead_agent not in AGENTS:
        return {"error": f"Agent未找到: {lead_agent}"}

    agent_cfg = AGENTS[lead_agent]
    prompt = f"""你是{team_name}团队的{lead_agent}。现在是每日运营巡检时间。

你需要从{team_name}运营的角度，检查以下方面：

{checklist_text}

请按以下格式输出巡检报告：

## 📊 {team_name} 运营巡检报告
**巡检时间**: {time.strftime('%Y-%m-%d %H:%M')}

### 1. 关键指标概览
（用简洁的数据格式列出核心KPI）

### 2. 异常告警
（标记出所有需要立即关注的问题，按优先级排序。用⚠️标记中优先级，🚨标记高优先级）

### 3. 今日行动建议
（针对告警项给出具体行动计划，谁负责、做什么）

### 4. 趋势判断
（基于最近数据，判断接下来24-48小时应该重点关注什么）

注意：你是一个运营操盘手，不是一个报告生成器。你的输出应该让老板/运营负责人一眼看出"今天有什么问题需要我处理"。
"""

    memory = load_memory(lead_agent, f"monitor_{team_name}")
    messages = [{"role":"system","content":AGENT_SYSTEM_PREFIX + "\n\n" + agent_cfg["system"]}]
    messages.extend(memory)
    messages.append({"role":"user","content":prompt})

    try:
        result = call_llm(messages, api_key, max_tokens=2000)
        report = result["choices"][0]["message"]["content"]
        save_memory(lead_agent, f"monitor_{team_name}", "user", prompt)
        save_memory(lead_agent, f"monitor_{team_name}", "assistant", report)

        # 提取告警项
        alerts = []
        for line in report.split('\n'):
            if '🚨' in line or '⚠️' in line:
                alerts.append({"level":"critical" if '🚨' in line else "warning", "text":line.strip(), "time":time.strftime('%H:%M')})

        MONITOR_DATA[team_name] = {
            "report": report,
            "alerts": alerts,
            "last_check": time.strftime('%Y-%m-%d %H:%M:%S'),
            "agent": lead_agent
        }
        MONITOR_ALERTS.extend(alerts)

        # 如果有高优先级告警，让所有团队成员各出一份应对建议
        if any(a["level"]=="critical" for a in alerts):
            for agent_name in cfg["agents"]:
                if agent_name == lead_agent: continue
                if agent_name not in AGENTS: continue
                alert_prompt = f"🚨 {team_name} 运营告警！以下问题需要你从专业角度给出应对建议：\n\n{report}\n\n请从你的专业角度，给出1-2条具体的应对行动。"
                try:
                    am = [{"role":"system","content":AGENT_SYSTEM_PREFIX + "\n\n" + AGENTS[agent_name]["system"]},{"role":"user","content":alert_prompt}]
                    ar = call_llm(am, api_key, max_tokens=600)
                    save_memory(agent_name, f"monitor_{team_name}", "assistant", ar["choices"][0]["message"]["content"])
                except:
                    pass

        return {"report": report, "alerts": alerts, "team": team_name, "last_check": MONITOR_DATA[team_name]["last_check"]}
    except Exception as e:
        return {"error": str(e)}


def monitor_loop(api_key):
    """后台定时巡检线程"""
    while True:
        for team in MONITOR_TASKS:
            try:
                print(f"  🔍 巡检 {team}...")
                run_monitor_check(team, api_key)
            except Exception as e:
                print(f"  ❌ {team} 巡检失败: {e}")
        time.sleep(3600)  # 每小时检查一次是否有到时间的团队


if __name__ == "__main__":
    init_db()
    teams = set(a["team"] for a in AGENTS.values())
    print(f"🧠 虚拟员工全团队智能体服务器 v2")
    print(f"   地址: http://localhost:{PORT}")
    print(f"   {len(AGENTS)} 个 Agent · {len(teams)} 个团队")
    for t in sorted(teams):
        members = [n for n,c in AGENTS.items() if c["team"]==t]
        print(f"     {t}: {', '.join(members)}")
    print(f"\n   🔍 运营监控引擎: Amazon(每4h) Shopify(每6h) TikTok(每4h)")
    cfg = _shopify_config()
    if cfg.get("store") and cfg.get("token") and "your-store" not in cfg.get("store",""):
        print(f"   🏪 Shopify 已连接: {cfg['store']}")
        print(f"      Shopify Agent 可直接读写产品/订单/客户/库存")
    else:
        print(f"   🏪 Shopify 未配置 — 创建 shopify_config.json 后重启即可直连后台")
    print(f"   每个 Agent 拥有: 独立记忆 + 文件/网页/Shell/浏览器/电脑操作工具")
    print(f"   工具开关: shell={ENABLE_SHELL} browser={ENABLE_BROWSER} computer={ENABLE_COMPUTER}")
    print(f"   API: POST /chat | POST /batch | POST /monitor | GET /tools | GET /dashboard | GET /teams")
    print(f"   按 Ctrl+C 停止\n")
    http.server.HTTPServer(("127.0.0.1", PORT), AgentServer).serve_forever()
