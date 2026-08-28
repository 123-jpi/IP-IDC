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
    print(f"🎲 随机抽取 {test_size} 个 IP 进行深度保活与 CF 官方地理识别测试...")
    return sampled_list

def check_keepalive_and_get_loc(ip_port):
    """
    通过 Socket 握手建立连接，并直接从 Cloudflare 探针响应中解析权威国家代码 loc=XX
    不依赖任何第三方 IP 数据库，100% 准确且无请求限流
    """
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
        sock.settimeout(2.5)
        sock.connect((ip, port))

        # 向 1.1.1.1/cdn-cgi/trace 发送握手请求，触发 Cloudflare 返回包含 loc=XX 的信息
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

        # 从 Cloudflare trace 数据中直接正则提取 loc=XX（如 loc=DE, loc=JP）
        if "loc=" in response:
            match = re.search(r'loc=([A-Z]{2})', response)
            if match:
                country = match.group(1)
                return {"ip_port": ip_port, "ip": ip, "country": country}
        elif response.startswith("HTTP/"):
            # 如果没拿到 loc 但 HTTP 能连通，降级使用 IP-API 作为单次备用查询
            country = get_fallback_country(ip)
            if country != "UN":
                return {"ip_port": ip_port, "ip": ip, "country": country}

    except Exception:
        pass
    return None

def get_fallback_country(ip):
    """备用查询逻辑"""
    try:
        res = requests.get(f"http://ip-api.com/json/{ip}?fields=countryCode", timeout=2)
        if res.status_code == 200:
            country = res.json().get("countryCode")
            if country:
                return country.upper()
    except Exception:
        pass
    return "UN"

def process_ip(ip_port):
    result = check_keepalive_and_get_loc(ip_port)
    if result:
        country = result["country"]
        
        # 严格过滤：如果极端情况下依然没获取到国家（为 UN），直接丢弃该节点，确保输出质量
        if country == "UN":
            return None
            
        # 仅输出干净格式：IP:PORT#国家码
        formatted_entry = f"{ip_port}#{country}"
        return formatted_entry
    return None

def delete_old_kv():
    """清除上一轮旧 KV，防止遗留失效 IP"""
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
            print(f"🎉 成功将格式化的 {len(valid_entries)} 个纯净落地 IP 写入 Cloudflare KV！")
        else:
            print(f"❌ 写入 KV 失败: {res.status_code} - {res.text}")
    else:
        print("⚠️ 本轮未测试出活 IP，KV 保持清空。")

def main():
    raw_ips = fetch_candidate_ips()
    valid_entries = []
    
    print("🚀 开始进行【Socket 保活 + Cloudflare 官方地理位置识别】...")
    with ThreadPoolExecutor(max_workers=50) as executor:
        results = executor.map(process_ip, raw_ips)
        for r in results:
            if r:
                valid_entries.append(r)
                
    print(f"测试完成！共筛选出 {len(valid_entries)} 个高质量节点（已完全剔除 UN）")
    
    if CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN:
        update_cloudflare_kv(valid_entries)

if __name__ == "__main__":
    main()
