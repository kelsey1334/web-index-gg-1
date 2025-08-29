import logging, os, json, re, requests, xml.etree.ElementTree as ET
from fastapi import FastAPI, Request, WebSocket, Form, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
from datetime import datetime, timedelta, timezone

app = FastAPI()
templates = Jinja2Templates(directory="templates")

logging.basicConfig(level=logging.INFO)

SCOPES = ["https://www.googleapis.com/auth/indexing"]
INDEXING_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"
DAILY_LIMIT = 200

API_CREDENTIALS = [
    {"name": "API1", "json": os.getenv("API1_JSON")},
    {"name": "API2", "json": os.getenv("API2_JSON")},
    {"name": "API3", "json": os.getenv("API3_JSON")},
    {"name": "API4", "json": os.getenv("API4_JSON")},
    {"name": "API5", "json": os.getenv("API5_JSON")},
]

APIs = []
for api in API_CREDENTIALS:
    if api["json"]:
        creds_json = json.loads(api["json"])
        creds = service_account.Credentials.from_service_account_info(
            creds_json, scopes=SCOPES
        )
        APIs.append({
            "name": api["name"],
            "session": AuthorizedSession(creds),
            "email": creds_json["client_email"],
            "used": 0,
            "day": datetime.utcnow().date()
        })

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

def extract_domain(text):
    text = text.strip()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"/.*$", "", text)
    return text

def index_with_api(api, url):
    body = {"url": url, "type": "URL_UPDATED"}
    response = api["session"].post(INDEXING_ENDPOINT, json=body)
    add_quota(api, 1)
    return response.json()

def parse_sitemap(url):
    urls = []
    r = requests.get(url)
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

def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

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

@app.websocket("/ws/{api_name}/{domain}")
async def ws_index(websocket: WebSocket, api_name: str, domain: str):
    try:
        await websocket.accept()
        api = next(a for a in APIs if a["name"] == api_name)
        await websocket.send_text(f"🔗 Connected. Bắt đầu index domain {domain} bằng {api['name']} ({api['email']})")

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
        await websocket.send_text(f"🔍 Tìm thấy {total} URL.")

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
                await websocket.send_text(f"[{i}/{total}] ❌ {url} (Lỗi: {err_msg})")
            else:
                success += 1
                await websocket.send_text(f"[{i}/{total}] ✅ {url}")

        await websocket.send_text(f"🎯 Hoàn tất. Thành công: {success}, Thất bại: {fail}\n{quota_message(api)}")
        await websocket.close()
    except WebSocketDisconnect:
        logging.info("WebSocket client disconnected")
    except Exception as e:
        logging.error(f"WebSocket error: {e}")
        try:
            await websocket.send_text(f"❌ Server error: {e}")
            await websocket.close()
        except:
            pass
