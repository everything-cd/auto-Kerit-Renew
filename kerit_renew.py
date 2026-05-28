import os
import time
import json
import socket
import signal
import imaplib
import email
import re
import subprocess
import urllib.request
import urllib.parse
import requests
from urllib.parse import unquote, urlparse, parse_qs
from seleniumbase import SB

# ============================================================
# 工具函数
# ============================================================

def mask_email(email_str: str) -> str:
    """掩码邮箱：保留第一个字符和@前最后一个字符，其他用*替代"""
    parts = email_str.split("@")
    local = parts[0]
    domain = parts[1]
    
    if len(local) > 2:
        return local[0] + "*" * (len(local) - 2) + local[-1] + "@" + domain
    else:
        return local[0] + "*" * max(0, len(local) - 1) + ("" if len(local) == 1 else local[-1]) + "@" + domain


# ============================================================
# 配置（从环境变量读取）
# ============================================================

_account = os.environ["KERIT_ACCOUNT"].split(",")
KERIT_EMAIL    = _account[0].strip()
GMAIL_PASSWORD = _account[1].strip()

# 代理配置
HY2_PROXY_URL = os.getenv('HY2_PROXY_URL', "")
SOCKS_PORT = int(os.getenv('SOCKS_PORT', '51080'))

# 邮箱掩码
MASKED_EMAIL = mask_email(KERIT_EMAIL)

LOGIN_URL      = "https://billing.kerit.cloud/"
FREE_PANEL_URL = "https://billing.kerit.cloud/free_panel"

_tg_raw = os.environ.get("TG_BOT", "")
if _tg_raw and "," in _tg_raw:
    _tg = _tg_raw.split(",")
    TG_CHAT_ID = _tg[0].strip()
    TG_TOKEN   = _tg[1].strip()
else:
    TG_CHAT_ID = ""
    TG_TOKEN   = ""


# ============================================================
# Hy2 代理管理
# ============================================================

class Hy2Proxy:
    """Hysteria2 代理管理器"""
    def __init__(self, url: str):
        self.url = url
        self.proc = None

    def start(self) -> bool:
        print("📡 启动 Hysteria2…")

        u = self.url.replace("hysteria2://", "").replace("hy2://", "")
        parsed = urlparse("scheme://" + u)
        params = parse_qs(parsed.query)

        # 处理 insecure 参数（支持 insecure 和 allowInsecure）
        insecure_val = params.get("insecure", params.get("allowInsecure", ["0"]))[0]
        insecure = insecure_val == "1"

        cfg = {
            "server": f"{parsed.hostname}:{parsed.port}",
            "auth": unquote(parsed.username),
            "tls": {
                "sni": params.get("sni", [parsed.hostname])[0],
                "insecure": insecure,
                "alpn": params.get("alpn", ["h3"]),
            },
            "socks5": {"listen": f"127.0.0.1:{SOCKS_PORT}"}
        }

        cfg_path = "/tmp/hy2.json"
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)

        try:
            self.proc = subprocess.Popen(
                ["hysteria", "client", "-c", cfg_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        except FileNotFoundError:
            print("❌ hysteria 命令未找到，请先安装 Hysteria2")
            return False

        for _ in range(12):
            time.sleep(1)
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", SOCKS_PORT)) == 0:
                    print("✅ Hy2 SOCKS5 已就绪")
                    return True
        return False

    def stop(self):
        if self.proc:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            print("🛑 Hy2 已停止")

    @property
    def proxy(self):
        return f"socks5://127.0.0.1:{SOCKS_PORT}"


def get_proxy_manager():
    """根据环境变量判断是否需要使用代理"""
    if HY2_PROXY_URL:
        return Hy2Proxy(HY2_PROXY_URL)
    return None


def mask_ip(ip: str) -> str:
    """脱敏 IP 地址"""
    return ip.rsplit(".", 1)[0] + ".***"


def check_ip(proxy: str = None) -> str:
    """检查落地 IP，明确指出是否使用了代理"""
    try:
        proxies = None
        if proxy:
            proxies = {"http": proxy, "https": proxy}
        r = requests.get(
            "http://ip-api.com/json/?fields=status,query,countryCode",
            proxies=proxies,
            timeout=30
        ).json()
        if r.get("status") == "success":
            ip_str = f"{mask_ip(r['query'])} ({r['countryCode']})"
            mode = "✅ 代理" if proxy else "⚠️ 直连"
            return f"{ip_str} [{mode}]"
    except Exception:
        pass
    mode = "✅ 代理" if proxy else "⚠️ 直连"
    return f"未知 IP [{mode}]"


def start_proxy_with_retry(max_retries=3):
    """启动代理，失败时重试"""
    if not HY2_PROXY_URL:
        print("⚠️ 未配置代理 URL，使用直连模式")
        return None, None
    
    proxy_manager = get_proxy_manager()
    proxy_url = None
    
    if not proxy_manager:
        print("⚠️ 代理管理器初始化失败，使用直连模式")
        return None, None
    
    for attempt in range(1, max_retries + 1):
        print(f"🔄 尝试启动代理 ({attempt}/{max_retries})...")
        if proxy_manager.start():
            proxy_url = proxy_manager.proxy
            print(f"✅ 代理已启动：{proxy_url}")
            return proxy_manager, proxy_url
        else:
            if attempt < max_retries:
                print(f"⏳ 等待 5 秒后重试...")
                time.sleep(5)
            else:
                print("⚠️ 代理启动失败，继续使用直连模式")
    
    return None, None


# ============================================================
# TG 推送
# ============================================================

def now_str():
    import datetime
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def send_tg(result, server_id=None, remaining=None, ip_info=None, email=None):
    lines = [
        f"🎮 Kerit 服务器续期通知",
        f"🕐 运行时间: {now_str()}",
    ]
    if email:
        tg_user_id = TG_CHAT_ID if TG_CHAT_ID else "0000"
        tg_user_link = f'<a href="tg://user?id={tg_user_id}">{email}</a>'
        lines.append(f"📮 邮箱: {tg_user_link}")

    lines.append(f"📊 续期结果: {result}")
    if server_id is not None:
        lines.append(f"🖥 服务器ID: {server_id}")
    if remaining is not None:
        lines.append(f"⏱️ 剩余天数: {remaining}天")
    if ip_info:
        lines.append(f"🌐 IP信息: {ip_info}")
    msg = "\n".join(lines)
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ TG未配置，跳过推送")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"📨 TG推送成功")
    except Exception as e:
        print(f"⚠️ TG推送失败：{e}")


# ============================================================
# IMAP 读取 Gmail OTP
# ============================================================

def fetch_otp_from_gmail(wait_seconds=60) -> str:
    print(f"📬 连接Gmail，等待{wait_seconds}s...")
    deadline = time.time() + wait_seconds

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(KERIT_EMAIL, GMAIL_PASSWORD)
    except imaplib.IMAP4.error as e:
        print(f"❌ Gmail 认证失败: {e}")
        print("💡 请检查:")
        print("   1. KERIT_ACCOUNT 环境变量是否正确")
        print("   2. Gmail 是否启用了 IMAP 访问")
        print("   3. 是否需要使用应用专用密码而不是账户密码")
        raise TimeoutError(f"Gmail 认证失败: {e}")

    spam_folder = None
    _, folder_list = mail.list()
    for f in folder_list:
        decoded = f.decode("utf-8", errors="ignore")
        if any(k in decoded for k in ["Spam", "Junk", "垃圾", "spam", "junk"]):
            match = re.search(r'"([^"]+)"\s*$', decoded)
            if not match:
                match = re.search(r'(\S+)\s*$', decoded)
            if match:
                spam_folder = match.group(1).strip('"')
                print(f"🗑️ 检查Gmail垃圾邮箱")
                break

    folders_to_check = ["INBOX"]
    if spam_folder:
        folders_to_check.append(spam_folder)
    else:
        print("⚠️ 未找到垃圾邮箱")

    seen_uids = {}
    for folder in folders_to_check:
        try:
            status, _ = mail.select(folder)
            if status != "OK":
                raise Exception(f"select失败: {status}")
            _, data = mail.uid("search", None, "ALL")
            seen_uids[folder] = set(data[0].split())
        except Exception as e:
            print(f"⚠️ 文件夹异常 {folder}: {e}")
            seen_uids[folder] = set()

    while time.time() < deadline:
        time.sleep(5)

        for folder in folders_to_check:
            try:
                status, _ = mail.select(folder)
                if status != "OK":
                    continue
                _, data = mail.uid("search", None, 'FROM "kerit"')
                all_uids = set(data[0].split())
                new_uids = all_uids - seen_uids[folder]

                for uid in new_uids:
                    seen_uids[folder].add(uid)
                    _, msg_data = mail.uid("fetch", uid, "(RFC822)")
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                break
                        if not body:
                            for part in msg.walk():
                                if part.get_content_type() == "text/html":
                                    html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                    body = re.sub(r'<[^>]+>', ' ', html)
                                    break
                    else:
                        body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                    otp = re.search(r'\b(\d{4})\b', body)
                    if otp:
                        code = otp.group(1)
                        print(f"✅ Gmail OTP: {code}")
                        mail.logout()
                        return code

            except Exception as e:
                print(f"⚠️ 检查{folder}出错: {e}")
                continue

    mail.logout()
    raise TimeoutError("❌ Gmail超时")


# ============================================================
# 过 CF 盾（登录前）—— 完全按照参考代码逻辑
# ============================================================

def _kerit_cf_bypass(sb) -> bool:
    """
    执行过 CF 盾逻辑，返回是否看到登录表单（#email-input）。
    （与参考代码的 _cf_bypass 一模一样，仅 URL 和检测元素不同）
    """
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=6.0)
    time.sleep(4)

    # 第一层：uc_gui_click_captcha 模拟真实鼠标点击 Turnstile
    try:
        sb.uc_gui_click_captcha()
        time.sleep(3)
    except Exception:
        pass

    # 第二层：若仍有 CF iframe 则自动切入点击
    try:
        if sb.is_element_visible('iframe[src*="challenges.cloudflare"]'):
            print("⚠️ 发现 CF iframe，自动处理...")
            sb.switch_to_frame('iframe[src*="challenges.cloudflare"]')
            sb.uc_click('input[type="checkbox"]', timeout=6)
            sb.switch_to_default_content()
            time.sleep(5)
    except Exception:
        try:
            sb.switch_to_default_content()
        except Exception:
            pass

    # 判断是否通过（看到 #email-input）
    try:
        sb.wait_for_element_visible('#email-input', timeout=15)
        return True
    except Exception:
        return False


# ============================================================
# 续期过程中的 Turnstile（轻量，不重新打开页面）
# ============================================================

def check_token(sb) -> bool:
    """检查 Cloudflare Turnstile 的 token 是否已写入隐藏 input"""
    try:
        return sb.execute_script("""
            (function(){
                var input = document.querySelector('input[name="cf-turnstile-response"]');
                return input && input.value && input.value.length > 20;
            })()
        """)
    except Exception:
        return False


def turnstile_exists(sb) -> bool:
    """判断页面是否存在 Turnstile 验证元素"""
    try:
        return sb.execute_script(
            "(function(){ return document.querySelector('input[name=\"cf-turnstile-response\"]') !== null; })()"
        )
    except Exception:
        return False


def get_token_value(sb) -> str:
    """获取 Turnstile token 字符串"""
    try:
        token = sb.execute_script("""
            (function(){
                var input = document.querySelector('input[name="cf-turnstile-response"]');
                return (input && input.value) ? input.value : '';
            })()
        """)
        if token and len(token) > 20:
            return token
    except Exception:
        pass
    return ''


def solve_turnstile_on_page(sb) -> bool:
    """
    在当前页面解决 Turnstile（不重新加载），用于续期等场景。
    """
    try:
        sb.uc_gui_click_captcha()
        time.sleep(3)
    except Exception:
        pass

    try:
        if sb.is_element_visible('iframe[src*="challenges.cloudflare"]'):
            print("⚠️ 发现 CF iframe，自动处理...")
            sb.switch_to_frame('iframe[src*="challenges.cloudflare"]')
            sb.uc_click('input[type="checkbox"]', timeout=6)
            sb.switch_to_default_content()
            time.sleep(5)
    except Exception:
        try:
            sb.switch_to_default_content()
        except Exception:
            pass

    for _ in range(30):
        if check_token(sb):
            print("✅ Cloudflare Token 验证通过")
            return True
        time.sleep(0.5)

    print("❌ Cloudflare Token 验证超时")
    sb.save_screenshot("turnstile_fail.png")
    return False


# ============================================================
# 续期相关
# ============================================================

def extract_remaining_days(sb) -> int:
    """从 expiry-display 元素读取剩余天数"""
    try:
        return sb.execute_script("""
            (function(){
                var el = document.getElementById('expiry-display');
                return el ? parseInt(el.innerText || "0") : 0;
            })()
        """) or 0
    except Exception:
        return 0


def do_renew(sb, ip_info=None, email=None):
    print("🔄 跳转续期页...")
    sb.open(FREE_PANEL_URL)
    time.sleep(4)
    sb.save_screenshot("free_panel.png")

    server_id = sb.execute_script(
        "(function(){ return typeof serverData !== 'undefined' ? serverData.id : null; })()"
    )
    if not server_id:
        print("❌ serverData.id缺失")
        sb.save_screenshot("no_server_id.png")
        send_tg("❌ serverData.id缺失，续期失败", ip_info=ip_info, email=email)
        return
    print(f"🆔 服务器ID: {server_id}")

    initial_count = sb.execute_script("""
        (function(){
            var el = document.getElementById('renewal-count');
            return el ? parseInt(el.innerText || "0") : 0;
        })()
    """)
    initial_remaining = extract_remaining_days(sb)
    need = 7 - initial_count
    print(f"📊 当前进度: {initial_count}/7，剩余天数: {initial_remaining}天，本次需续期: {need}次")

    if initial_remaining >= 7:
        print("✅ 剩余天数已满7天，无需续期")
        sb.save_screenshot("renew_skip.png")
        send_tg("✅ 无需续期（剩余天数已满）", server_id, initial_remaining, ip_info=ip_info, email=email)
        return

    if need <= 0:
        print("🎉 已达上限7/7，无需续期")
        sb.save_screenshot("renew_full.png")
        remaining = extract_remaining_days(sb)
        send_tg("✅ 无需续期（已达上限 7/7）", server_id, remaining, ip_info=ip_info, email=email)
        return

    for attempt in range(need):
        count = sb.execute_script("""
            (function(){
                var el = document.getElementById('renewal-count');
                return el ? parseInt(el.innerText || "0") : 0;
            })()
        """)
        print(f"📊 续期进度: {count}/7")

        if count >= 7:
            print("🎉 已达上限7/7，提前结束")
            sb.save_screenshot("renew_full.png")
            remaining = extract_remaining_days(sb)
            send_tg("✅ 续期完成", server_id, remaining, ip_info=ip_info, email=email)
            return

        print(f"🔁 第{attempt + 1}/{need}次续期...")

        # 点击 Renew Server 按钮
        renew_clicked = False
        for _ in range(10):
            try:
                btns = sb.find_elements("a, button")
                btn = next((b for b in btns if "Renew Server" in (b.text or "")), None)
                if btn:
                    btn.click()
                    renew_clicked = True
                    print("✅ 已点击「Renew Server」")
                    break
            except Exception:
                pass
            time.sleep(1)

        if not renew_clicked:
            print("❌ 续期按钮缺失")
            sb.save_screenshot("no_renew_btn.png")
            send_tg(f"❌ 续期按钮缺失，第{attempt + 1}次失败", server_id, ip_info=ip_info, email=email)
            return

        time.sleep(2)

        print("⏳ 等待Turnstile...")
        for _ in range(20):
            if turnstile_exists(sb):
                print("🛡️ 检测到Turnstile")
                break
            time.sleep(1)
        else:
            print("❌ Turnstile未出现")
            sb.save_screenshot(f"no_turnstile_{attempt}.png")
            send_tg(f"❌ Turnstile未出现，第{attempt + 1}次失败", server_id, ip_info=ip_info, email=email)
            return

        if not solve_turnstile_on_page(sb):
            sb.save_screenshot(f"turnstile_fail_{attempt}.png")
            send_tg(f"❌ Turnstile验证失败，第{attempt + 1}次", server_id, ip_info=ip_info, email=email)
            return

        token = get_token_value(sb)
        if not token:
            print("❌ Token获取失败")
            send_tg(f"❌ Token获取失败，第{attempt + 1}次", server_id, ip_info=ip_info, email=email)
            return

        print("🎯 提交续期...")
        result = sb.execute_script(f"""
            (async function() {{
                const res = await fetch('/api/renew', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    credentials: 'include',
                    body: JSON.stringify({{ id: '{server_id}', captcha: '{token}' }})
                }});
                const data = await res.json();
                return JSON.stringify(data);
            }})()
        """)
        try:
            import json as _json
            res_obj = _json.loads(result)
            if res_obj.get('success') or res_obj == {}:
                print("✅ 续期成功")
            else:
                print(f"❌ 续期失败: {result}")
        except Exception:
            print(f"✅ 续期成功")

        try:
            sb.execute_script("document.querySelector('[data-bs-dismiss=\"modal\"]')?.click();")
        except Exception:
            pass

        time.sleep(3)
        sb.execute_script("window.location.reload();")
        time.sleep(3)

    sb.save_screenshot("renew_done.png")
    final_count = sb.execute_script("""
        (function(){
            var el = document.getElementById('renewal-count');
            return el ? parseInt(el.innerText || "0") : 0;
        })()
    """)
    final_remaining = extract_remaining_days(sb)
    print(f"📊 最终进度: {final_count}/7")
    if final_count >= 7:
        print("🎉 已达上限7/7")
        send_tg("✅ 续期完成", server_id, final_remaining, ip_info=ip_info, email=email)
    else:
        print(f"⚠️ 续期未达上限，当前{final_count}/7")
        send_tg(f"⚠️ 续期未达上限（{final_count}/7）", server_id, final_remaining, ip_info=ip_info, email=email)


# ============================================================
# 主流程（已修正输入邮箱后的 Turnstile 处理）
# ============================================================

def run_script():
    print("🔧 启动浏览器...")

    # 初始化代理
    proxy_manager, proxy_url = start_proxy_with_retry(max_retries=3)
    ip_info = ""
    
    # 检查 IP 信息
    print(f"🔍 正在检查 IP 信息（使用代理: {bool(proxy_url)})...")
    ip_info = check_ip(proxy_url)
    print(f"🌐 IP 信息：{ip_info}")

    try:
        with SB(uc=True, test=True, proxy=proxy_url) as sb:
            print("🚀 浏览器就绪！")

            # ── IP 验证 ──────────────────────────────────────────
            print("🌐 验证出口IP...")
            try:
                sb.open("https://api.ipify.org/?format=json")
                ip_text = sb.get_text('body')
                ip_text = re.sub(r'(\d+\.\d+\.\d+\.)\d+', r'\1xx', ip_text)
                print(f"✅ 出口IP确认：{ip_text}")
            except Exception:
                print("⚠️ IP验证超时，跳过")

            # ── 登录（过 CF 盾） ────────────────────────────────
            MAX_CF_ATTEMPTS = 5
            login_page_ready = False
            print("🔑 开始过 CF 盾...")
            for cf_try in range(1, MAX_CF_ATTEMPTS + 1):
                print(f"🔄 第 {cf_try}/{MAX_CF_ATTEMPTS} 次尝试...")
                try:
                    if _kerit_cf_bypass(sb):
                        print("✅ CF 盾通过，已看到登录表单")
                        login_page_ready = True
                        break
                    else:
                        print(f"⚠️ 第 {cf_try} 次未通过 CF，重试...")
                        sb.save_screenshot(f"cf_stuck_{cf_try}.png")
                except Exception as e:
                    print(f"⚠️ 过盾异常: {e}")
                    time.sleep(3)

            if not login_page_ready:
                sb.save_screenshot("cf_fatal.png")
                send_tg("❌ 过 CF 盾多次失败", ip_info=ip_info, email=MASKED_EMAIL)
                return

            # ── 填写邮箱 ────────────────────────────────────────
            print("📭 确认邮箱框...")
            try:
                sb.wait_for_element_visible('#email-input', timeout=10)
            except Exception:
                print("❌ 邮箱框加载失败")
                sb.save_screenshot("kerit_no_email_input.png")
                send_tg("❌ 邮箱框加载失败", ip_info=ip_info, email=MASKED_EMAIL)
                return

            sb.type('#email-input', KERIT_EMAIL)
            print(f"✅ 邮箱：{MASKED_EMAIL}")

            # ⚡ 关键新增：等待 3 秒，然后处理可能出现的 Turnstile
            print("⏳ 等待 3 秒，检查动态 Turnstile...")
            time.sleep(3)

            if turnstile_exists(sb):
                print("🛡️ 检测到 Turnstile（输入邮箱后），自动验证...")
                if not solve_turnstile_on_page(sb):
                    sb.save_screenshot("email_cf_fail.png")
                    send_tg("❌ 输入邮箱后 Turnstile 验证失败", ip_info=ip_info, email=MASKED_EMAIL)
                    return
                print("✅ Turnstile 验证通过，按钮即将可用")
            else:
                print("✅ 无 Turnstile，继续")

            # ── 等待按钮变为可用（非 disabled） ────────────────
            print("🖱️ 等待 Continue 按钮变为可用...")
            btn_enabled = False
            for _ in range(30):
                try:
                    disabled = sb.execute_script(
                        "return document.getElementById('continue-btn').disabled"
                    )
                    if not disabled:
                        btn_enabled = True
                        break
                except Exception:
                    pass
                time.sleep(1)

            if not btn_enabled:
                print("❌ 按钮长时间不可用")
                sb.save_screenshot("btn_disabled.png")
                send_tg("❌ Continue 按钮未启用", ip_info=ip_info, email=MASKED_EMAIL)
                return

            # 现在点击按钮
            print("🔘 点击「Continue with Email」")
            try:
                sb.click('#continue-btn')
            except Exception:
                print("⚠️ JS 点击失败，尝试常规点击")
                sb.click('button#continue-btn')
            print("✅ 已点击继续，等待 OTP 发送...")

            # ── 等待 OTP 输入框出现 ────────────────────────────
            print("📨 等待OTP框...")
            try:
                sb.wait_for_element_visible('.otp-input', timeout=30)
            except Exception:
                print("❌ OTP框加载失败")
                sb.save_screenshot("kerit_no_otp.png")
                send_tg("❌ OTP框加载失败", ip_info=ip_info, email=MASKED_EMAIL)
                return

            # ── 获取并填写 OTP ──────────────────────────────────
            try:
                code = fetch_otp_from_gmail(wait_seconds=60)
            except TimeoutError as e:
                print(e)
                sb.save_screenshot("kerit_otp_timeout.png")
                send_tg("❌ Gmail OTP获取超时", ip_info=ip_info, email=MASKED_EMAIL)
                return

            otp_inputs = sb.find_elements('.otp-input')
            if len(otp_inputs) < 4:
                print(f"❌ OTP框不足: {len(otp_inputs)}")
                send_tg(f"❌ OTP框数量不足（{len(otp_inputs)}）", ip_info=ip_info, email=MASKED_EMAIL)
                return

            print(f"⌨️ 填入OTP: {code}")
            for i, char in enumerate(code):
                js = f"""
                    (function() {{
                        var inputs = document.querySelectorAll('.otp-input');
                        var inp = inputs[{i}];
                        if (!inp) return;
                        var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeInputValueSetter.call(inp, '{char}');
                        inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }})();
                """
                sb.execute_script(js)
                time.sleep(0.1)

            print("✅ OTP已填入")
            time.sleep(0.5)

            print("🚀 点击验证...")
            verify_clicked = False
            for sel in [
                '//button[contains(., "Verify Code")]',
                '//span[contains(., "Verify Code")]',
                'button[type="submit"]',
            ]:
                try:
                    if sb.is_element_visible(sel):
                        sb.click(sel)
                        verify_clicked = True
                        break
                except Exception:
                    continue

            if not verify_clicked:
                print("❌ 验证按钮缺失")
                sb.save_screenshot("kerit_no_verify_btn.png")
                send_tg("❌ 验证按钮缺失", ip_info=ip_info, email=MASKED_EMAIL)
                return

            print("⏳ 等待登录跳转...")
            for _ in range(80):
                try:
                    url = sb.get_current_url()
                    if "/session" in url:
                        print("✅ 登录成功！")
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            else:
                print("❌ 登录等待超时")
                sb.save_screenshot("kerit_login_timeout.png")
                send_tg("❌ 登录等待超时", ip_info=ip_info, email=MASKED_EMAIL)
                return

            do_renew(sb, ip_info, MASKED_EMAIL)
    finally:
        if proxy_manager:
            proxy_manager.stop()


if __name__ == "__main__":
    run_script()
