import os
import re
import socket
import random
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

# Cloudflare 允许的反代端口
CF_ALLOWED_PORTS = {80, 443, 8080, 8443, 2052, 2053, 2082, 2083, 2086, 2087, 8880}

PROXY_SOURCES = [
    "https://zip.cm.edu.kg/all.txt",
]

def create_http_session():
    """创建复用连接池的 Session"""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http_session = create_http_session()

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
            res = http_session.get(url, timeout=8)
            if res.status_code == 200:
                extracted = clean_and_extract_ip(res.text)
                all_candidates.update(extracted)
                print(f"从 {url} 抓取并清理出 {len(extracted)} 个有效端口 IP")
        except Exception as e:
            print(f"抓取源失败 {url}: {e}")
            
    total_count = len(all_candidates)
    print(f"全网池去重完成，共搜集到 {total_count} 个干净候选 IP！")
    return list(all_candidates)

def check_keepalive_and_get_loc(ip_port):
    """ Socket 极致提速检测（1.2s 超时） """
    sock = None
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
        sock.settimeout(1.2)  # 压缩至 1.2s，丢弃高延迟/卡死的假活节点
        sock.connect((ip, port))

        probe = (
            f"GET /cdn-cgi/trace HTTP/1.1\r\n"
            f"Host: 1.1.1.1\r\n"
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
            f"Accept: */*\r\n"
            f"Connection: Keep-Alive\r\n\r\n"
        )
        
        sock.sendall(probe.encode("utf-8"))
        response = sock.recv(1024).decode("utf-8", errors="ignore")

        if "loc=" in response:
            match = re.search(r'loc=([A-Z]{2})', response)
            if match:
                country = match.group(1)
                return {"ip_port": ip_port, "ip": ip, "country": country}
        elif response.startswith("HTTP/"):
            country = get_fallback_country(ip)
            if country != "UN":
                return {"ip_port": ip_port, "ip": ip, "country": country}

    except Exception:
        pass
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
    return None

def get_fallback_country(ip):
    """备用查询逻辑"""
    try:
        res = http_session.get(f"http://ip-api.com/json/{ip}?fields=countryCode", timeout=1.5)
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
        if country == "UN":
            return None
        return f"{ip_port}#{country}"
    return None

def run_fast_target_testing(ip_pool, min_total_target=80, max_total_target=100, min_jp_target=3, batch_size=40):
    """
    极速目标驱动逻辑：
    只要 80 <= 有效节点 <= 100 且 JP >= 3，立刻终止，绝不浪费时间！
    """
    random.shuffle(ip_pool)
    
    tested_ips = set()
    valid_entries = []
    jp_count = 0

    print(f"🚀 开始极速测试：目标搜集 {min_total_target}~{max_total_target} 个有效节点，且其中包含 JP >= {min_jp_target} 个...")

    idx = 0
    pool_len = len(ip_pool)

    # 40 个并发搭配 1.2s 超时，能在几秒内消化完一个 Batch
    with ThreadPoolExecutor(max_workers=40) as executor:
        while idx < pool_len:
            current_valid_count = len(valid_entries)
            
            # 退出条件：达到了最少目标量(80+) 并且 JP 节点已凑满 3 个；或者总数已达上限(100)
            if (current_valid_count >= min_total_target and jp_count >= min_jp_target) or current_valid_count >= max_total_target:
                print(f"\n🎯 目标达成，立即停止！共检测 {len(tested_ips)} 个 IP，成功捕获有效节点 {current_valid_count} 个，其中 JP 数量为 {jp_count} 个。")
                break

            batch = ip_pool[idx : idx + batch_size]
            idx += batch_size
            
            for ip in batch:
                tested_ips.add(ip)

            results = executor.map(process_ip, batch)
            
            for r in results:
                if r:
                    valid_entries.append(r)
                    if r.endswith("#JP"):
                        jp_count += 1

            print(f"📊 进度: 已测试 {len(tested_ips)} 个 IP | 成功捕获有效节点 {len(valid_entries)} 个，其中 JP 数量为 {jp_count} 个 (目标 ≥ {min_jp_target})")

            if idx >= pool_len:
                print(f"⚠️ 整个 IP 池已全部测试完毕！共筛选出有效节点 {len(valid_entries)} 个，其中 JP 数量为 {jp_count} 个。")

    return valid_entries

def delete_old_kv():
    """清除上一轮旧 KV"""
    kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    try:
        http_session.delete(kv_url, headers=headers, timeout=8)
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
        res = http_session.put(kv_url, data=content.encode('utf-8'), headers=headers, timeout=10)
        if res.status_code == 200:
            print(f"🎉 成功将格式化的 {len(valid_entries)} 个纯净落地 IP 写入 Cloudflare KV！")
        else:
            print(f"❌ 写入 KV 失败: {res.status_code} - {res.text}")
    else:
        print("⚠️ 本轮未测试出活 IP，KV 保持清空。")

def main():
    raw_ip_pool = fetch_candidate_ips()
    if not raw_ip_pool:
        print("❌ 未抓取到任何有效 IP，程序退出。")
        return

    # 调高效率逻辑，达到目标直接 Exit
    valid_entries = run_fast_target_testing(
        ip_pool=raw_ip_pool,
        min_total_target=80,        # 搜集到 80 个活节点即可准备退出
        max_total_target=100,       # 上限 100 个
        min_jp_target=3,            # 保底 3 个 JP 节点
        batch_size=40               # 小批次快速响应退出条件
    )
        
    print(f"\n✅ 全部流程完成！最终输出 {len(valid_entries)} 个高质量节点。")
    
    if CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN:
        update_cloudflare_kv(valid_entries)

if __name__ == "__main__":
    main()
