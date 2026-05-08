import os
import re
import json
import urllib.parse
from datetime import datetime
from functools import wraps
import asyncio
import aiohttp
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "meou_scan_secret_key_default_123")

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

USERS = {}
for key, value in os.environ.items():
    if key.startswith("USER_EMAIL_"):
        index_suffix = key.replace("USER_EMAIL_", "")
        password = os.getenv(f"USER_PASS_{index_suffix}")
        if password:
            USERS[value] = password

ECOMMERCE_DOMAINS = r'(?:shopee\.vn|shope\.ee|lazada\.vn|lzd\.co|tiktok\.com|tiki\.vn|ti\.ki|joyme|s\.shopee\.vn)'
LINK_PATTERNS = [
    re.compile(r'https?://[^\s"\'<>\\{}]*' + ECOMMERCE_DOMAINS + r'[^\s"\'<>\\{}]*'),
    re.compile(r'https(?:%3A|:|\\u00253A)[^\s"\'<>\\{}]*' + ECOMMERCE_DOMAINS + r'[^\s"\'<>\\{}]*')
]

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_clean_ecommerce_url(raw_url):
    try:
        decoded = urllib.parse.unquote(urllib.parse.unquote(raw_url))
        decoded = decoded.replace('\\/', '/').replace('\\u0026', '&').replace('\\', '').split('"')[0].split("'")[0]
        
        if 'an_redir' in decoded:
            return None
            
        if 'youtube.com/redirect' in decoded or 'url=' in decoded or 'q=' in decoded:
            if 'event=video_description' in decoded or 'event=comments' in decoded or 'event=channel_description' in decoded:
                return None
                
            parsed = urllib.parse.urlparse(decoded)
            query = urllib.parse.parse_qs(parsed.query)
            if 'q' in query: decoded = query['q'][0]
            elif 'url' in query: decoded = query['url'][0]
            elif 'origin_link' in query: decoded = query['origin_link'][0]

        decoded_lower = decoded.lower()
        
        if 'lazada.vn' in decoded_lower or 'lzd.co' in decoded_lower:
            if '.html' not in decoded_lower and '/products/' not in decoded_lower:
                return None
            if '.html' in decoded_lower:
                decoded = decoded.split('.html')[0] + '.html'
            else:
                decoded = decoded.split('?')[0]
            return {"url": decoded, "platform": "Lazada"}
            
        elif 'shopee.vn' in decoded_lower or 'shope.ee' in decoded_lower or 's.shopee.vn' in decoded_lower:
            if '/product/' not in decoded_lower and '-i.' not in decoded_lower and 'sp_atk' not in decoded_lower:
                return None
            decoded = decoded.split('?')[0]
            return {"url": decoded, "platform": "Shopee"}
            
        elif 'tiktok.com' in decoded_lower:
            if '/product/' not in decoded_lower and '/view/product/' not in decoded_lower:
                return None
            decoded = decoded.split('?')[0]
            return {"url": decoded, "platform": "Other"}
            
        elif 'tiki.vn' in decoded_lower or 'ti.ki' in decoded_lower:
            if '.html' not in decoded_lower and '/p' not in decoded_lower:
                return None
            decoded = decoded.split('?')[0]
            return {"url": decoded, "platform": "Other"}
            
        else:
            decoded = decoded.split('?')[0]
            return {"url": decoded, "platform": "Other"}
            
    except: return None

def extract_video_id(url):
    match = re.search(r'(?:v=|youtu\.be/|shorts/|/embed/)([0-9A-Za-z_-]{11})', url)
    return match.group(1) if match else None

def check_native_shopping(html_content):
    """
    Kiểm tra xem HTML có chứa các cờ (flags) của tính năng Giỏ hàng (Native Shopping) hay không.
    Các cờ này được cập nhật dựa trên cấu trúc ytInitialData mới nhất của YouTube.
    """
    indicators = [
        '"shoppingOverlayRenderer"',        # Thường dùng trên Shorts
        '"shoppingPanelRenderer"',          # Bảng điều khiển mua sắm
        '"productCarouselRenderer"',        # Băng chuyền sản phẩm
        '"shoppingCarouselItemRenderer"',   # Item trong băng chuyền
        '"productListItemRenderer"',        # Danh sách sản phẩm dọc
        '"engagementPanelShopping"',        # Tính năng mua sắm trong khung tương tác
        '"shoppingResources"'               # Resource mua sắm
    ]
    
    for indicator in indicators:
        if indicator in html_content:
            return True
            
    # Dự phòng thêm cách check cũ (nhỡ YT dùng lại)
    if re.search(r'"shoppingId"\s*:\s*"([^"]{5,30})"', html_content):
        return True
        
    return False

async def get_channel_info(session_http, url):
    try:
        if '@' in url:
            handle = url.split('@')[-1].split('/')[0].split('?')[0]
            api_url = f"{YOUTUBE_API_BASE}/channels?part=snippet,contentDetails&forHandle={handle}&key={YOUTUBE_API_KEY}"
        elif '/channel/' in url:
            channel_id = url.split('/channel/')[-1].split('/')[0].split('?')[0]
            api_url = f"{YOUTUBE_API_BASE}/channels?part=snippet,contentDetails&id={channel_id}&key={YOUTUBE_API_KEY}"
        else: return[], "MeoU"

        async with session_http.get(api_url) as resp:
            data = await resp.json()
            if not data.get('items'): return[], "Channel"
            item = data['items'][0]
            channel_name, channel_id = item['snippet']['title'], item['id']
            if channel_id.startswith("UC"):
                base_id = channel_id[2:]
                return["UU" + base_id, "UUSH" + base_id, "UULV" + base_id], channel_name
            uploads_id = item['contentDetails']['relatedPlaylists'].get('uploads')
            return [uploads_id] if uploads_id else[], channel_name
    except: return[], "MeoU"

async def get_playlist_videos(session_http, playlist_id, start_date, max_results=50, max_pages=100):
    video_ids =[]
    next_page_token = None
    pages_fetched = 0
    
    try:
        while pages_fetched < max_pages:
            api_url = f"{YOUTUBE_API_BASE}/playlistItems?part=snippet&maxResults={max_results}&playlistId={playlist_id}&key={YOUTUBE_API_KEY}"
            if next_page_token:
                api_url += f"&pageToken={next_page_token}"
                
            async with session_http.get(api_url) as resp:
                if resp.status != 200: break
                data = await resp.json()
                items = data.get('items',[])
                stop_fetching = False
                
                for item in items:
                    pub_date = item['snippet']['publishedAt'].split('T')[0]
                    if pub_date < start_date:
                        stop_fetching = True
                        break 
                    video_ids.append(item['snippet']['resourceId']['videoId'])
                
                if stop_fetching: break
                next_page_token = data.get('nextPageToken')
                if not next_page_token: break
            pages_fetched += 1
        return video_ids
    except Exception as e: 
        print(f"Lỗi lấy playlist: {e}")
        return video_ids

async def fetch_html_and_extract_links(session_http, video_data, semaphore):
    vid = video_data['vid']
    shorts_url = f"https://www.youtube.com/shorts/{vid}"
    
    async with semaphore:
        try:
            current_type = video_data['type']
            html_content = ""
            
            # Ưu tiên fetch giao diện Shorts (Vì UI giỏ hàng của Shorts dễ bóc tách hơn)
            async with session_http.get(shorts_url, allow_redirects=False, timeout=10) as resp:
                if resp.status == 200:
                    video_data['type'] = 'Short'
                    video_data['url'] = shorts_url
                    html_content = await resp.text()
                else:
                    if current_type != 'Stream':
                        video_data['type'] = 'Video'
                    video_data['url'] = f"https://www.youtube.com/watch?v={vid}"
            
            # Nếu không phải Shorts, fetch giao diện Video thường
            if not html_content:
                async with session_http.get(video_data['url'], timeout=10) as resp:
                    html_content = await resp.text()
            
            if video_data['type'] == 'Stream':
                if re.search(r'"isPremiere"\s*:\s*true', html_content) or 'BADGE_STYLE_TYPE_PREMIERE' in html_content:
                    video_data['type'] = 'Video'
            
            # BƯỚC 1: Kiểm tra xem video CÓ giỏ hàng (Native Shopping) hay không trước
            has_native_shopping = check_native_shopping(html_content)
            
            # Nếu KHÔNG có giỏ hàng, trả về kết quả 0 luôn cho nhanh, không cần regex mất thời gian
            if not has_native_shopping:
                video_data.update({
                    'has_shopping': False,
                    'shopping_links': [],
                    'shopee_count': 0,
                    'lazada_count': 0,
                    'other_count': 0,
                    'status': 'success'
                })
                return video_data

            # BƯỚC 2: Nếu CÓ giỏ hàng native, tiến hành bóc tách link
            clean_html = html_content
            # Lọc bỏ bớt text mô tả/comment để tránh bắt nhầm link rác
            clean_html = re.sub(r'<meta[^>]*>', '', clean_html)
            clean_html = re.sub(r'"text"\s*:\s*"(?:[^"\\]|\\.)*"', '""', clean_html)
            clean_html = re.sub(r'"content"\s*:\s*"(?:[^"\\]|\\.)*"', '""', clean_html)
            clean_html = re.sub(r'"simpleText"\s*:\s*"(?:[^"\\]|\\.)*"', '""', clean_html)

            raw_links = {}
            for p in LINK_PATTERNS:
                for m in p.findall(clean_html):
                    clean_data = get_clean_ecommerce_url(m)
                    if clean_data and clean_data['url'] not in raw_links:
                        raw_links[clean_data['url']] = clean_data['platform']
                    
            ecommerce_items = [{"clean_url": k, "platform": v} for k, v in raw_links.items()]
            
            shopee_c = sum(1 for i in ecommerce_items if i['platform'] == 'Shopee')
            lazada_c = sum(1 for i in ecommerce_items if i['platform'] == 'Lazada')
            other_c = sum(1 for i in ecommerce_items if i['platform'] == 'Other')
            
            # Nếu là Native Shopping nhưng tool không bóc được link Shopee/Lazada (có thể do YT mã hóa sâu quá),
            # chúng ta vẫn set total_other_count tối thiểu là 1 để người dùng biết "Có sản phẩm bên trong".
            total_other_count = other_c if (shopee_c > 0 or lazada_c > 0 or other_c > 0) else 1

            video_data.update({
                'has_shopping': True, # Đã check qua hàm check_native_shopping ở trên
                'shopping_links': ecommerce_items,
                'shopee_count': shopee_c,
                'lazada_count': lazada_c,
                'other_count': total_other_count,
                'status': 'success'
            })
            return video_data
            
        except Exception as e:
            print(f"Lỗi phân tích video {vid}: {e}")
            video_data.update({'has_shopping': False, 'shopping_links':[], 'shopee_count': 0, 'lazada_count': 0, 'other_count': 0, 'status': 'error'})
            return video_data

async def process_all_urls(urls, start_date, end_date):
    candidate_ids =[]
    final_channel_name = "MeoU"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    
    async with aiohttp.ClientSession(headers=headers) as session_http:
        for u in urls:
            if '/@' in u or '/channel/' in u:
                playlist_ids, name = await get_channel_info(session_http, u)
                if playlist_ids:
                    final_channel_name = name
                    for pid in playlist_ids:
                        vids = await get_playlist_videos(session_http, pid, start_date, max_results=50)
                        candidate_ids.extend(vids)
            else:
                vid = extract_video_id(u)
                if vid: 
                    candidate_ids.append(vid)
                
        unique_ids = list(set(candidate_ids))
        valid_videos =[]
        for i in range(0, len(unique_ids), 50):
            chunk = unique_ids[i:i+50]
            api_url = f"{YOUTUBE_API_BASE}/videos?part=snippet,contentDetails,liveStreamingDetails&id={','.join(chunk)}&key={YOUTUBE_API_KEY}"
            try:
                async with session_http.get(api_url) as resp:
                    data = await resp.json()
                    for item in data.get('items',[]):
                        pub_date = item['snippet']['publishedAt'].split('T')[0]
                        if start_date <= pub_date <= end_date:
                            vid = item['id']
                            
                            is_live = 'liveStreamingDetails' in item or item['snippet'].get('liveBroadcastContent') != 'none'
                            v_type = "Stream" if is_live else "Video"
                            
                            valid_videos.append({
                                "vid": vid,
                                "url": "", 
                                "upload_date": pub_date,
                                "display_date": datetime.strptime(pub_date, "%Y-%m-%d").strftime("%d/%m/%Y"),
                                "type": v_type,
                                "channel_name": item['snippet'].get('channelTitle', final_channel_name)
                            })
            except: continue
                
        semaphore = asyncio.Semaphore(15) 
        tasks =[fetch_html_and_extract_links(session_http, v, semaphore) for v in valid_videos]
        scanned_results = await asyncio.gather(*tasks)
    return scanned_results, final_channel_name

@app.route('/')
@login_required
def index(): return render_template('index.html', user=session['user'])

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        email = data.get('email')
        password = data.get('password')
        if email in USERS and USERS[email] == password:
            session['user'] = email
            return jsonify({"status": "success"})
        return jsonify({"status": "error", "message": "Email hoặc mật khẩu sai!"}), 401
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None); return redirect(url_for('login'))

@app.route('/api/scan', methods=['POST'])
@login_required
def scan_links():
    data = request.get_json()
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        scanned_results, final_channel_name = loop.run_until_complete(process_all_urls(data.get('urls',[]), data.get('startDate'), data.get('endDate')))
        loop.close()
    except Exception as e:
        print(f"Lỗi Scan: {e}")
        return jsonify({"results":[], "channel_name": "Lỗi"})
        
    scanned_results.sort(key=lambda x: x['upload_date'], reverse=True)
    return jsonify({"results": scanned_results, "channel_name": final_channel_name})

if __name__ == '__main__':
    app.run(debug=True, port=5000)
