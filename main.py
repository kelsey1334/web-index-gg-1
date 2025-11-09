import logging, os, json, re, base64, requests, xml.etree.ElementTree as ET
import asyncio
from fastapi import FastAPI, Request, WebSocket, Form, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
from datetime import datetime, timedelta, timezone
import urllib3

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ===========================
# App & Templates
# ===========================
app = FastAPI()
templates = Jinja2Templates(directory="templates")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("indexbot")

# ===========================
# Config
# ===========================
SCOPES = ["https://www.googleapis.com/auth/indexing"]
INDEXING_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"
DAILY_LIMIT = 200
HPING_API_KEY = os.getenv("HPING_API_KEY")  # 1hping ApiKey header

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

# ===========================
# Load API credentials
# ===========================
API_CREDENTIALS = [
    {"name": "API1", "json": os.getenv("API1_JSON")},
    {"name": "API2", "json": os.getenv("API2_JSON")},
    {"name": "API3", "json": os.getenv("API3_JSON")},
    {"name": "API4", "json": os.getenv("API4_JSON")},
    {"name": "API5", "json": os.getenv("API5_JSON")},
]

def _try_parse_json_string(s: str):
    """Thử json.loads trên chuỗi 's' (đã strip)."""
    return json.loads(s)

def _try_parse_base64_json(s: str):
    """Thử decode Base64 rồi json.loads."""
    raw = base64.b64decode(s)
    return json.loads(raw.decode("utf-8"))

def _try_load_from_path(p: str):
    """Nếu 'p' là đường dẫn file, đọc nội dung JSON."""
    if os.path.exists(p) and os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    raise FileNotFoundError("Path not found")

def load_service_account_info(cred_input: str):
    """
    Hỗ trợ 3 dạng:
    1) JSON thuần (bắt đầu bằng '{')
    2) Base64 của JSON
    3) Đường dẫn file tới JSON
    """
    if not cred_input:
        return None

    s = cred_input.strip()

    # 1) JSON thuần
    if s.startswith("{"):
        try:
            return _try_parse_json_string(s)
        except Exception as e:
            logger.warning("JSON string parse failed: %s", e)

    # 2) Base64 JSON
    try:
        # Heuristic: base64 thường không chứa khoảng trắng, nhưng vẫn thử decode
        return _try_parse_base64_json(s)
    except Exception:
        pass

    # 3) Đường dẫn file
    try:
        return _try_load_from_path(s)
    except Exception:
        pass

    # Không nhận diện được
    raise ValueError("Invalid credential input: not JSON, not Base64 JSON, not a valid file path")

APIs = []
for api in API_CREDENTIALS:
    if not api["json"]:
        logger.info("Skip %s: no env provided", api["name"])
        continue
    try:
        creds_json = load_service_account_info(api["json"])
        creds = service_account.Credentials.from_service_account_info(
            creds_json, scopes=SCOPES
        )
        APIs.append({
            "name": api["name"],
            "session": AuthorizedSession(creds),
            "email": creds_json.get("client_email", "unknown"),
            "used": 0,
            "day": datetime.utcnow().date()
        })
        logger.info("Loaded %s (%s)", api["name"], creds_json.get("client_email"))
    except Exception as e:
        logger.error("Failed to load %s: %s", api["name"], e)

# ===========================
# Quota functions
# ===========================
def check_api_quota(api):
    today = datetime.utcnow().date()
    if today != api["day"]:
        api["day"] = today
        api["used"] = 0
    return DAILY_LIMIT - api["used"]

def add_quota(api, count=1):
    api["used"] += count

def quota_message(api):
    remaining = check_api_quota(api)
    reset_time_vn = (datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                     + timedelta(days=1)).replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=7)))
    return f"{api['name']} ({api['email']}): {api['used']}/{DAILY_LIMIT} used, còn {remaining}. Reset {reset_time_vn.strftime('%H:%M %d-%m-%Y')} VN"

# ===========================
# Helpers
# ===========================
def extract_domain(text):
    text = text.strip()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"/.*$", "", text)
    return text

def index_with_api(api, url):
    body = {"url": url, "type": "URL_UPDATED"}
    response = api["session"].post(INDEXING_ENDPOINT, json=body, timeout=REQUEST_TIMEOUT)
    add_quota(api, 1)
    # Uploader note: Google có thể trả về 200/OK nhưng body rỗng; đảm bảo .json() không crash
    try:
        return response.json()
    except Exception:
        return {"status": response.status_code}

def parse_sitemap(url):
    urls = []
    r = requests.get(url, verify=False, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    if root.tag.endswith("sitemapindex"):
        for sitemap in root.findall("sm:sitemap", ns):
            loc = sitemap.find("sm:loc", ns).text
            urls.extend(parse_sitemap(loc))
    elif root.tag.endswith("urlset"):
        for url_tag in root.findall("sm:url", ns):
            loc = url_tag.find("sm:loc", ns).text
            urls.append(loc)
    return urls

# ===========================
# 1hping Integration
# ===========================
def submit_to_1hping(urls: list, name: str = "IndexBot"):
    """Gửi danh sách URL lên 1hping"""
    if not HPING_API_KEY:
        return None, "HPING_API_KEY is not set"
    hping_url = "https://app.1hping.com/external/api/campaign/create?culture=vi-VN"
    headers = {
        "ApiKey": HPING_API_KEY,
        "Content-Type": "application/json"
    }
    data = {
        "CampaignName": name,
        "NumberOfDay": 1,
        "Urls": urls
    }
    try:
        resp = requests.post(hping_url, headers=headers, json=data, timeout=REQUEST_TIMEOUT)
        return resp.status_code, resp.text
    except Exception as e:
        return None, str(e)

# ===========================
# Routes
# ===========================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/check", response_class=HTMLResponse)
async def check_domain(request: Request, domain: str = Form(...)):
    domain = extract_domain(domain)
    sitemap_url_https = f"https://{domain}/sitemap_index.xml"
    sitemap_url_http = f"http://{domain}/sitemap_index.xml"
    try:
        try:
            urls = parse_sitemap(sitemap_url_https)
        except Exception:
            urls = parse_sitemap(sitemap_url_http)
    except Exception as e:
        return templates.TemplateResponse("index.html", {"request": request, "error": str(e)})

    total = len(urls)
    candidates = []
    details = []
    for api in APIs:
        remaining = check_api_quota(api)
        details.append(quota_message(api))
        if remaining >= total:
            candidates.append(api)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "domain": domain,
        "total": total,
        "apis": APIs,
        "candidates": candidates,
        "details": details
    })

# ===========================
# WebSocket
# ===========================
@app.websocket("/ws/{api_name}/{domain}")
async def ws_index(websocket: WebSocket, api_name: str, domain: str):
    try:
        await websocket.accept()

        if not APIs:
            await websocket.send_text("❌ Không có API Google Indexing nào được nạp. Kiểm tra biến môi trường APIx_JSON.")
            await websocket.close()
            return

        # Tìm API theo tên
        sel = [a for a in APIs if a["name"] == api_name]
        if not sel:
            await websocket.send_text(f"❌ Không tìm thấy API có name='{api_name}'.")
            await websocket.close()
            return
        api = sel[0]

        await websocket.send_text(f"🚀 Bắt đầu index domain `{domain}` bằng {api['name']} ({api['email']})")
        await asyncio.sleep(0)

        sitemap_url_https = f"https://{domain}/sitemap_index.xml"
        sitemap_url_http = f"http://{domain}/sitemap_index.xml"
        try:
            try:
                urls = parse_sitemap(sitemap_url_https)
            except Exception:
                urls = parse_sitemap(sitemap_url_http)
        except Exception as e:
            await websocket.send_text(f"❌ Lỗi lấy sitemap: {e}")
            await websocket.close()
            return

        total = len(urls)
        await websocket.send_text(f"🔍 Tìm thấy {total} URL, bắt đầu gửi lên Google...")
        await asyncio.sleep(0)

        success, fail = 0, 0
        for i, url in enumerate(urls, start=1):
            remaining = check_api_quota(api)
            if remaining <= 0:
                await websocket.send_text("🚫 Hết quota API này!")
                break

            result = index_with_api(api, url)
            if "error" in result:
                fail += 1
                err_msg = result['error'].get('message', 'Unknown error')
                await websocket.send_text(f"[GSC {i}/{total}] ❌ {url} (Lỗi: {err_msg})")
            else:
                success += 1
                await websocket.send_text(f"[GSC {i}/{total}] ✅ {url}")
            await asyncio.sleep(0)

        await websocket.send_text(f"🎯 GSC hoàn tất. Thành công: {success}, Thất bại: {fail}\n{quota_message(api)}")
        await asyncio.sleep(0)

        # Gửi danh sách lên 1hping
        await websocket.send_text("🌐 Đang gửi toàn bộ danh sách URL lên 1hping...")
        hping_status, hping_resp = submit_to_1hping(urls, name=f"{domain}")
        await asyncio.sleep(0)
        if hping_status == 200:
            await websocket.send_text("✅ Đã gửi toàn bộ URL lên 1hping thành công.")
        else:
            await websocket.send_text(f"❌ Gửi 1hping thất bại: {hping_resp}")

        await websocket.close()
    except WebSocketDisconnect:
        logger.info("🔌 WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.send_text(f"❌ Server error: {e}")
            await websocket.close()
        except:
            pass
