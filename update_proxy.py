import os
import re
import socket
import requests
from concurrent.futures import ThreadPoolExecutor

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

# 支持的 Cloudflare 代理端口列表
CF_ALLOWED_PORTS = {80, 443, 8080, 8443, 2052, 2053, 2082, 2083, 2086, 2087, 8880}

PROXY_SOURCES = [
    "https://raw.githubusercontent.com/cmliu/CF-Workers-SUB/main/proxyip.txt",
]

def fetch_candidate_ips():
    candidates = set()
    ip_pattern = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+|\[[0-9a-fA-F:]+\]:\d+)')
    for url in PROXY_SOURCES:
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                matches = ip_pattern.findall(res.text)
                for item in matches:
                    # 过滤出符合 CF 规则的端口
                    port = int(item.rsplit(":", 1)[-1])
                    if port in CF_ALLOWED_PORTS:
                        candidates.add(item)
        except Exception as e:
            print(f"抓取源失败 {url}: {e}")
            
    raw_list = list(candidates)[:200]  # 限制数量，保障几秒内测试完成
    print(f"筛选出符合 CF 端口规则的候选 IP: {len(raw_list)} 个")
    return raw_list

def check_cf_proxy_compatibility(ip_port):
    """
    通过模拟 Cloudflare 握手请求，检测该 IP 是否可作为 CF 落地代理
    """
    try:
        proxies = {
            "http": f"http://{ip_port}",
            "https": f"http://{ip_port}"
        }
        # 向公网 CDN 发起带 Cloudflare 标识的请求，测试代理中转能力
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Host": "1.1.1.1"
        }
        
        # 建立代理连接，超时限制 2.5 秒
        res = requests.get(
            "http://1.1.1.1/cdn-cgi/trace", 
            proxies=proxies, 
            headers=headers, 
            timeout=2.5
        )
        
        # 成功响应且包含 CF 追踪特征，说明该 IP 具备良好的反代连通性
        if res.status_code == 200 and "h=" in res.text:
            ip = ip_port.rsplit(":", 1)[0].replace("[", "").replace("]", "")
            return {"ip_port": ip_port, "ip": ip}
    except Exception:
        pass
    return None

def get_country_code(ip):
    """获取 IP 地理位置信息"""
    try:
        res = requests.get(f"http://ip-api.com/json/{ip}?fields=countryCode", timeout=2)
        if res.status_code == 200:
            return res.json().get("countryCode", "🌐")
    except Exception:
        pass
    return "🌐"

def get_flag_emoji(country_code):
    if not country_code or len(country_code) != 2:
        return "🌐"
    return "".join(chr(127397 + ord(c)) for c in country_code.upper())

def process_ip(ip_port):
    result = check_cf_proxy_compatibility(ip_port)
    if result:
        country = get_country_code(result["ip"])
        flag = get_flag_emoji(country)
        formatted_entry = f"{ip_port}#{flag} {country} - [CF OK]"
        return formatted_entry
    return None

def update_cloudflare_kv(valid_entries):
    kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "text/plain; charset=utf-8"
    }
    content = "\n".join(valid_entries)
    res = requests.put(kv_url, data=content.encode('utf-8'), headers=headers)
    if res.status_code == 200:
        print("🎉 成功存入 Cloudflare 兼容的高效落地 IP 库！")
    else:
        print(f"写入 KV 失败: {res.status_code} - {res.text}")

def main():
    raw_ips = fetch_candidate_ips()
    valid_entries = []
    
    print("🚀 开始在 GitHub 上模拟 Cloudflare 连通性测试...")
    # 40 线程并发
    with ThreadPoolExecutor(max_workers=40) as executor:
        results = executor.map(process_ip, raw_ips)
        for r in results:
            if r:
                valid_entries.append(r)
                
    print(f"测试完成！筛选出通过 CF 连通校验的 IP: {len(valid_entries)} 个")
    
    if valid_entries and CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN:
        update_cloudflare_kv(valid_entries)

if __name__ == "__main__":
    main()
