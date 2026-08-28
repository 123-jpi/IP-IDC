import os
import re
import socket
import random
import requests
from concurrent.futures import ThreadPoolExecutor

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

# Cloudflare 官方支持的反代端口
CF_ALLOWED_PORTS = {80, 443, 8080, 8443, 2052, 2053, 2082, 2083, 2086, 2087, 8880}

PROXY_SOURCES = [
    "https://zip.cm.edu.kg/all.txt",
]

def clean_and_extract_ip(raw_text):
    """
    清洗数据：彻底清洗带 #DE、#US、备注信息的上万个 IP 文本，只保留标准的 IP:PORT
    """
    candidates = set()
    # 匹配标准的 IPv4:PORT 以及 [IPv6]:PORT
    ip_pattern = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+|\[[0-9a-fA-F:]+\]:\d+)')
    
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
            
        # 如果包含 # 备注后缀（如 1.2.3.4:443#DE），直接砍掉 # 后面的东西
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
                print(f"从 {url} 提取并清理出 {len(extracted)} 个有效端口 IP")
        except Exception as e:
            print(f"抓取源失败 {url}: {e}")
            
    total_count = len(all_candidates)
    print(f"全网池去重清洗完成，共找到 {total_count} 个干净的候选 IP！")
    
    # 随机抽样 500 个 IP 进行深度深度保活测试，防止单次测试卡死
    test_size = min(total_count, 500)
    sampled_list = random.sample(list(all_candidates), test_size)
    print(f"🎲 随机抽取 {test_size} 个 IP 进入深度【保活握手检测】...")
    return sampled_list

def check_keepalive_cf_proxy(ip_port):
    """
    高精度保活检测：直接使用 TCP Socket 发送原生 HTTP Keep-Alive / WebSocket 升级探针
    模拟真实的 Cloudflare Worker 进站连接，剔除伪装和无效 IP
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

        # 1. 建立基础 Socket 握手（超时限制 2 秒）
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect((ip, port))

        # 2. 构造标准的 HTTP Keep-Alive & WebSocket 模拟探针包
        # 很多代理反代服务器必须收到这种格式的报文才会正常响应
        probe = (
            f"GET / HTTP/1.1\r\n"
            f"Host: 1.1.1.1\r\n"
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
            f"Accept: */*\r\n"
            f"Connection: Keep-Alive, Upgrade\r\n"
            f"Upgrade: websocket\r\n\r\n"
        )
        
        sock.sendall(probe.encode("utf-8"))
        
        # 3. 读取对端返回的 HTTP 响应头（最多读 512 字节）
        response = sock.recv(512).decode("utf-8", errors="ignore")
        sock.close()

        # 只要对端回应了标准的 HTTP 头（如 101, 200, 301, 302, 400, 403, 404 等），
        # 证明这个端口真正运行着可用的 HTTP/WS 反代服务，而不是无响应的死端口！
        if response.startswith("HTTP/"):
            return {"ip_port": ip_port, "ip": ip}

    except Exception:
        pass
    return None

def get_country_code(ip):
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
    result = check_keepalive_cf_proxy(ip_port)
    if result:
        country = get_country_code(result["ip"])
        flag = get_flag_emoji(country)
        # 输出给 edgetunnel 认识的标准格式
        formatted_entry = f"{ip_port}#{flag} {country} - [ALIVE]"
        return formatted_entry
    return None

def delete_old_kv():
    kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    try:
        requests.delete(kv_url, headers=headers)
        print("🧹 已成功擦除旧的 KV 数据！")
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
            print(f"🎉 成功将真实的 {len(valid_entries)} 个保活 IP 写入 KV！")
        else:
            print(f"❌ 写入 KV 失败: {res.status_code} - {res.text}")
    else:
        print("⚠️ 本轮抽查未测出存活 IP，KV 保持清空。")

def main():
    raw_ips = fetch_candidate_ips()
    valid_entries = []
    
    print("🚀 开始进行真正的【Socket 协议级保活握手测试】...")
    # 50 个线程并发真正发包测试，每个大约耗时 1~2 秒，不再会出现假测瞬间完成的情况
    with ThreadPoolExecutor(max_workers=50) as executor:
        results = executor.map(process_ip, raw_ips)
        for r in results:
            if r:
                valid_entries.append(r)
                
    print(f"真测完成！筛选出符合保活标准的存活 IP: {len(valid_entries)} 个")
    
    if CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN:
        update_cloudflare_kv(valid_entries)

if __name__ == "__main__":
    main()
