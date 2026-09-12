import os
import re
import time
import socket
import random
import requests
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor

# 环境变量
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

# Cloudflare 支持的反代端口
CF_ALLOWED_PORTS = {80, 443, 8080, 8443, 2052, 2053, 2082, 2083, 2086, 2087, 8880}
PROXY_SOURCES = ["https://zip.cm.edu.kg/all.txt"]

# ----------------- 1. 抽样预过滤上限 (控制在 300~800 规模) -----------------
SAMPLE_PRIMARY_LIMITS = {
    "JP": 100,  # 提升抽样上限，给予 JP 更多测试机会
    "HK": 120,
    "US": 120,
    "SG": 100,
    "GB": 80
}

SAMPLE_NICHE_LIMITS = {
    "IN": 20, "FI": 20, "BR": 20, "AU": 20, "CA": 20,
    "DE": 20, "FR": 20, "KR": 20, "TW": 20, "NL": 20
}

DEFAULT_SAMPLE_MAX = 8

# ----------------- 2. 最终写入 KV 的配额限制 (总数上限 100) -----------------
FINAL_KV_LIMITS = {
    "HK": 25, "US": 25, "SG": 20, "JP": 15, "GB": 10,
    "IN": 3, "FI": 2, "BR": 2, "AU": 3, "CA": 3, "DE": 3, "FR": 3, "KR": 3, "TW": 3, "NL": 2
}
DEFAULT_FINAL_MAX = 2
MAX_TOTAL_RESULTS = 100  # 最终结果上限

def create_http_session():
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http_session = create_http_session()

def fetch_extract_clean_and_sample():
    raw_by_country = defaultdict(set)
    ip_pattern = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+|\[[0-9a-fA-F:]+\]:\d+)')
    
    total_raw_count = 0
    clean_count = 0

    for url in PROXY_SOURCES:
        try:
            res = http_session.get(url, timeout=10)
            if res.status_code != 200:
                continue

            for line in res.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                
                total_raw_count += 1

                # 提取后缀国家代码 (如 1.1.1.1:443#JP -> JP)
                country = "UN"
                if "#" in line:
                    parts = line.split("#")
                    line = parts[0].strip()
                    if len(parts) > 1 and parts[1].strip():
                        country = parts[1].strip().upper()

                match = ip_pattern.search(line)
                if match:
                    ip_port = match.group(1)
                    try:
                        port = int(ip_port.rsplit(":", 1)[-1])
                        if port in CF_ALLOWED_PORTS:
                            raw_by_country[country].add(f"{ip_port}#{country}")
                            clean_count += 1
                    except ValueError:
                        pass
        except Exception as e:
            print(f"⚠️ 抓取源失败 {url}: {e}")

    print(f"🔍 [阶段 1: 提取与清洗] 原始数据 {total_raw_count} 行 | 清洗出有效格式 IP {clean_count} 个 | 涵盖 {len(raw_by_country)} 个地区")

    sampled_pool = []
    for country, nodes in raw_by_country.items():
        node_list = list(nodes)
        random.shuffle(node_list)

        if country in SAMPLE_PRIMARY_LIMITS:
            sample_size = SAMPLE_PRIMARY_LIMITS[country]
        elif country in SAMPLE_NICHE_LIMITS:
            sample_size = SAMPLE_NICHE_LIMITS[country]
        else:
            sample_size = DEFAULT_SAMPLE_MAX

        sampled_pool.extend(node_list[:sample_size])

    random.shuffle(sampled_pool)
    print(f"🎯 [阶段 2: 精细抽样] 从清洗结果中提取 {len(sampled_pool)} 个样本进入连通性测试")
    return sampled_pool

def check_keepalive(item):
    """
    针对防误杀优化的连通性检测逻辑：
    1. 增加建连超时至 2.5s，应对跨国高延迟节点
    2. 采用通用型 HTTP 探测报文
    3. 宽松判活机制：只要 TCP 建连成功且返回有效 HTTP 报头即视为活 IP
    """
    ip_port, raw_country = item.rsplit("#", 1)
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
        # 核心修改 1：超时时间从 0.9s 放宽到 2.5s，防止在网络波动时误杀活 IP
        sock.settimeout(2.5)
        sock.connect((ip, port))

        # 核心修改 2：使用极简 HTTP/1.1 请求，减少因严格 Host 检查导致的连接被直接 reset
        probe = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {ip}\r\n"
            f"User-Agent: Mozilla/5.0\r\n"
            f"Connection: close\r\n\r\n"
        )
        sock.sendall(probe.encode("utf-8"))
        
        # 接收数据
        response = sock.recv(1024).decode("utf-8", errors="ignore")

        # 核心修改 3：宽松的响应判定，放宽误杀门槛
        # 只要能响应 HTTP 规范格式（无论是 200, 301, 302, 400, 403, 404 等），或者有 Server/Cloudflare 标记，均视作活节点
        if response.startswith("HTTP/") or "server:" in response.lower() or "cloudflare" in response.lower() or "fl=" in response:
            # 优先检查响应头里是否有 Cloudflare 节点区域 loc=XX
            if "loc=" in response:
                match = re.search(r'loc=([A-Z]{2})', response)
                if match and match.group(1) != "UN":
                    return f"{ip_port}#{match.group(1)}"
            
            # 无法从响应中解析 loc 时，直接沿用源数据解析出的国家后缀
            if raw_country != "UN":
                return f"{ip_port}#{raw_country}"

    except Exception:
        pass
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
    return None

def test_and_trim(sampled_pool, max_total=MAX_TOTAL_RESULTS):
    start_time = time.time()
    country_buckets = defaultdict(list)
    
    print(f"🚀 [阶段 3: 连通保活测试] 启动并发检测 (线程数: 30)...")

    # 核心修改 4：线程池并发度由 60 降至 30，避免在 Actions 环境触发 FD/Socket 排队延时
    with ThreadPoolExecutor(max_workers=30) as executor:
        results = executor.map(check_keepalive, sampled_pool)
        
        for item in results:
            if not item:
                continue
            
            _, country = item.rsplit("#", 1)
            max_allowed = FINAL_KV_LIMITS.get(country, DEFAULT_FINAL_MAX)

            if len(country_buckets[country]) < max_allowed:
                country_buckets[country].append(item)

            total_valid = sum(len(v) for v in country_buckets.values())

            if total_valid >= max_total:
                print(f"⚡ 已经集齐 {max_total} 个高质量有效 IP，提前终止测试！")
                break

    final_entries = []
    for country, nodes in country_buckets.items():
        final_entries.extend(nodes)

    final_entries = final_entries[:max_total]

    duration = round(time.time() - start_time, 2)
    print(f"✨ [阶段 4: 完成] 耗时 {duration}s | 最终提取纯净可用 IP: {len(final_entries)} 个 (含 JP: {len(country_buckets['JP'])} 个)")
    return final_entries

def safe_update_cloudflare_kv(valid_entries, min_required=40):
    """
    核心修改 5：把防空保护底线由 60 适当调低至 40
    若遇网络大波动或源站恶化，只要活着 40 个以上就更新，避免因死守 60 个导致长时间无法更新 KV
    """
    if not (CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN):
        print("⚠️ 未配置 Cloudflare KV 环境变量，跳过写入。")
        return

    if len(valid_entries) < min_required:
        print(f"⚠️ 本轮保活成功 {len(valid_entries)} 个，未达底线阀值 ({min_required} 个)，保留旧 KV 数据不覆盖。")
        return

    kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "text/plain; charset=utf-8"
    }
    
    try:
        res = http_session.put(kv_url, data="\n".join(valid_entries).encode('utf-8'), headers=headers, timeout=10)
        if res.status_code == 200:
            print(f"🎉 成功同步更新 Cloudflare KV！最终写入 {len(valid_entries)} 个纯净 IP。")
        else:
            print(f"❌ 写入 KV 失败: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ 更新 KV 网络异常: {e}")

def main():
    sampled_pool = fetch_extract_clean_and_sample()
    if not sampled_pool:
        print("❌ 抽样池为空，终止流程。")
        return

    valid_entries = test_and_trim(sampled_pool, max_total=MAX_TOTAL_RESULTS)
        
    safe_update_cloudflare_kv(valid_entries, min_required=40)

if __name__ == "__main__":
    main()
