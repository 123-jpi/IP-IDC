import os
import re
import socket
import random
import requests
from concurrent.futures import ThreadPoolExecutor

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

# Cloudflare 允许的反代端口
CF_ALLOWED_PORTS = {80, 443, 8080, 8443, 2052, 2053, 2082, 2083, 2086, 2087, 8880}

# -------------------------------------------------------------------
# Cloudflare 区域核心机房 (Colo) 代码 -> 城市名称全量映射字典
# -------------------------------------------------------------------
COLO_CITY_MAP = {
    # === 中国香港 & 中国台湾 ===
    "HKG": "Hong Kong", "TPE": "Taipei", "KHH": "Kaohsiung",

    # === 日本全境机房 ===
    "NRT": "Tokyo Narita", "HND": "Tokyo Haneda", "KIX": "Osaka", 
    "FUK": "Fukuoka", "NGO": "Nagoya", "CTS": "Sapporo", 
    "OKA": "Okinawa", "SDJ": "Sendai",

    # === 韩国机房 ===
    "ICN": "Seoul Incheon", "GMP": "Seoul Gimpo", "PUS": "Busan",

    # === 亚太：新加坡 & 东南亚 ===
    "SIN": "Singapore", "BKK": "Bangkok", "KUL": "Kuala Lumpur", 
    "MNL": "Manila", "CGK": "Jakarta", "SGN": "Ho Chi Minh", 
    "HAN": "Hanoi", "PNH": "Phnom Penh", "VTE": "Vientiane",

    # === 亚太：印度全境 ===
    "BOM": "Mumbai", "DEL": "New Delhi", "MAA": "Chennai", 
    "BLR": "Bengaluru", "CCU": "Kolkata", "HYD": "Hyderabad",

    # === 美西地区核心机房 ===
    "LAX": "Los Angeles", "SFO": "San Francisco", "SJC": "San Jose", 
    "SEA": "Seattle", "PDX": "Portland", "SAN": "San Diego", 
    "SMF": "Sacramento", "PHX": "Phoenix", "LAS": "Las Vegas", 
    "SLC": "Salt Lake City", "DEN": "Denver",

    # === 美东 & 美中主要机房 ===
    "ORD": "Chicago", "DFW": "Dallas", "JFK": "New York", 
    "EWR": "Newark", "IAD": "Washington DC", "ATL": "Atlanta", 
    "MIA": "Miami", "IAH": "Houston", "BOS": "Boston",

    # === 加拿大 (北美) ===
    "YVR": "Vancouver", "YYZ": "Toronto", "YUL": "Montreal", "YYC": "Calgary",

    # === 欧洲核心及主要机房 ===
    "LHR": "London", "MAN": "Manchester", "EDI": "Edinburgh",
    "CDG": "Paris", "MRS": "Marseille", "FRA": "Frankfurt", 
    "MUC": "Munich", "DUS": "Dusseldorf", "HAM": "Hamburg",
    "AMS": "Amsterdam", "BRU": "Brussels", "ZRH": "Zurich", 
    "VIE": "Vienna", "MXP": "Milan", "FCO": "Rome", 
    "MAD": "Madrid", "BCN": "Barcelona", "LIS": "Lisbon",
    "HEL": "Helsinki", "ARN": "Stockholm", "OSL": "Oslo", 
    "CPH": "Copenhagen", "WAW": "Warsaw", "PRG": "Prague", 
    "BUD": "Budapest", "ATH": "Athens", "IST": "Istanbul",

    # === 澳洲及新西兰 ===
    "SYD": "Sydney", "MEL": "Melbourne", "BNE": "Brisbane", 
    "PER": "Perth", "ADL": "Adelaide", "CBR": "Canberra", 
    "AKL": "Auckland", "CHC": "Christchurch"
}

PROXY_SOURCES = [
    "https://zip.cm.edu.kg/all.txt",
]

def clean_and_extract_ip(raw_text):
    """清洗源数据，提取标准的 IP:PORT"""
    candidates = set()
    ip_pattern = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+|\[[0-9a-fA-F:]+\]:\d+)')
    
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#")[0].strip()
            
        match = ip_pattern.search(line)
        if match:
            ip_port = match.group(1)
            try:
                port = int(ip_port.rsplit(":", 1)[-1])
                if port in CF_ALLOWED_PORTS:
                    candidates.add(ip_port)
            except ValueError:
                pass
                
    return candidates

def fetch_candidate_ips():
    all_candidates = set()
    for url in PROXY_SOURCES:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                extracted = clean_and_extract_ip(res.text)
                all_candidates.update(extracted)
                print(f"从 {url} 抓取并清理出 {len(extracted)} 个有效端口 IP")
        except Exception as e:
            print(f"抓取源失败 {url}: {e}")
            
    total_count = len(all_candidates)
    print(f"全网池去重完成，共搜集到 {total_count} 个干净候选 IP！")
    
    # 随机抽样 500 个 IP 进行深度测试
    test_size = min(total_count, 500)
    sampled_list = random.sample(list(all_candidates), test_size)
    print(f"🎲 随机抽取 {test_size} 个 IP 进行深度保活与机房辨识测试...")
    return sampled_list

def check_and_get_colo(ip_port):
    """通过 Socket 建立 Keep-Alive 连接，精确提取 Cloudflare 的 colo 机房代码"""
    try:
        if ip_port.startswith("["):
            ip = ip_port.rsplit("]:", 1)[0].replace("[", "")
            port = int(ip_port.rsplit("]:", 1)[1])
            family = socket.AF_INET6
        else:
            ip, port = ip_port.rsplit(":", 1)
            port = int(port)
            family = socket.AF_INET

        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect((ip, port))

        # 向 Cloudflare 边缘发送请求，触发输出 colo 数据
        probe = (
            f"GET /cdn-cgi/trace HTTP/1.1\r\n"
            f"Host: 1.1.1.1\r\n"
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
            f"Accept: */*\r\n"
            f"Connection: Keep-Alive\r\n\r\n"
        )
        
        sock.sendall(probe.encode("utf-8"))
        response = sock.recv(1024).decode("utf-8", errors="ignore")
        sock.close()

        if "colo=" in response:
            match = re.search(r'colo=([A-Z]{3})', response)
            colo = match.group(1) if match else "UNK"
            return {"ip_port": ip_port, "ip": ip, "colo": colo}
        elif response.startswith("HTTP/"):
            return {"ip_port": ip_port, "ip": ip, "colo": "UNK"}

    except Exception:
        pass
    return None

def get_ip_geo_info(ip):
    """
    智能查询 IP 对应的 ISO 国家代码以及真实城市名称（降级备用）
    """
    try:
        res = requests.get(f"http://ip-api.com/json/{ip}?fields=countryCode,city", timeout=2)
        if res.status_code == 200:
            data = res.json()
            country = data.get("countryCode", "UN").upper()
            city = data.get("city", "")
            return country, city
    except Exception:
        pass
    return "UN", ""

def process_ip(ip_port):
    result = check_and_get_colo(ip_port)
    if result:
        ip = result["ip"]
        colo = result["colo"]
        country, fallback_city = get_ip_geo_info(ip)
        
        # 1. 优先从字典获取标准的城市英文名
        city = COLO_CITY_MAP.get(colo)
        
        # 2. 如果是未记录的小机房或 UNK，用 IP-API 查到的真实城市补充
        if not city:
            city = fallback_city if fallback_city else "Unknown"
        
        # 拼装最终要求的格式：IP:PORT#国家-机房-城市
        formatted_entry = f"{ip_port}#{country}-{colo}-{city}"
        return formatted_entry
    return None

def delete_old_kv():
    kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    try:
        requests.delete(kv_url, headers=headers)
        print("🧹 已成功擦除上一轮旧的 KV 数据！")
    except Exception as e:
        print(f"⚠️ 清除旧 KV 状态: {e}")

def update_cloudflare_kv(valid_entries):
    delete_old_kv()
    if valid_entries:
        kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
        headers = {
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "text/plain; charset=utf-8"
        }
        content = "\n".join(valid_entries)
        res = requests.put(kv_url, data=content.encode('utf-8'), headers=headers)
        if res.status_code == 200:
            print(f"🎉 成功将格式化的 {len(valid_entries)} 个落地 IP 写入 Cloudflare KV！")
        else:
            print(f"❌ 写入 KV 失败: {res.status_code} - {res.text}")
    else:
        print("⚠️ 本轮未测试出活 IP，KV 保持清空。")

def main():
    raw_ips = fetch_candidate_ips()
    valid_entries = []
    
    print("🚀 开始进行【Socket 握手 + 核心边缘机房精确识别】...")
    with ThreadPoolExecutor(max_workers=50) as executor:
        results = executor.map(process_ip, raw_ips)
        for r in results:
            if r:
                valid_entries.append(r)
                
    print(f"测试完成！共筛选并标注了 {len(valid_entries)} 个节点")
    
    if CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN:
        update_cloudflare_kv(valid_entries)

if __name__ == "__main__":
    main()
