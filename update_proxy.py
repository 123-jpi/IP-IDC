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
    print(f"🎲 随机抽取 {test_size} 个 IP 进行深度保活握手测试...")
    return sampled_list

def check_keepalive_cf_proxy(ip_port):
    """协议级保活握手测试，确认 IP 可用性"""
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

        probe = (
            f"GET / HTTP/1.1\r\n"
            f"Host: 1.1.1.1\r\n"
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
            f"Accept: */*\r\n"
            f"Connection: Keep-Alive\r\n\r\n"
        )
        
        sock.sendall(probe.encode("utf-8"))
        response = sock.recv(512).decode("utf-8", errors="ignore")
        sock.close()

        if response.startswith("HTTP/"):
            return {"ip_port": ip_port, "ip": ip}

    except Exception:
        pass
    return None

def get_country_code(ip):
    """只获取大写的 ISO 国家二字代码（如 US, JP, DE, HK）"""
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
    result = check_keepalive_cf_proxy(ip_port)
    if result:
        ip = result["ip"]
        country = get_country_code(ip)
        
        # 仅输出干净的格式：IP:PORT#国家码
        # 示例: 148.251.148.186:443#DE
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
            print(f"🎉 成功将格式化的 {len(valid_entries)} 个落地 IP 写入 Cloudflare KV！")
        else:
            print(f"❌ 写入 KV 失败: {res.status_code} - {res.text}")
    else:
        print("⚠️ 本轮未测试出活 IP，KV 保持清空。")

def main():
    raw_ips = fetch_candidate_ips()
    valid_entries = []
    
    print("🚀 开始进行【保活握手 + 国家码获取】...")
    with ThreadPoolExecutor(max_workers=50) as executor:
        results = executor.map(process_ip, raw_ips)
        for r in results:
            if r:
                valid_entries.append(r)
                
    print(f"测试完成！共生成 {len(valid_entries)} 个精简格式节点")
    
    if CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN:
        update_cloudflare_kv(valid_entries)

if __name__ == "__main__":
    main()
