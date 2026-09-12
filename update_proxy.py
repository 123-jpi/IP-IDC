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
    "JP": 80,   # 全量/大比例优先抽 JP
    "HK": 100,
    "US": 100,
    "SG": 100,
    "GB": 60
}

SAMPLE_NICHE_LIMITS = {
    "IN": 15, "FI": 15, "BR": 15, "AU": 15, "CA": 15,
    "DE": 15, "FR": 15, "KR": 15, "TW": 15, "NL": 15
}

DEFAULT_SAMPLE_MAX = 5  # 其他小众国家各抽 5 个

# ----------------- 2. 最终写入 KV 的配额限制 (总数上限 100) -----------------
FINAL_KV_LIMITS = {
    "HK": 25, "US": 25, "SG": 20, "JP": 15, "GB": 10,
    "IN": 3, "FI": 2, "BR": 2, "AU": 3, "CA": 3, "DE": 3, "FR": 3, "KR": 3, "TW": 3, "NL": 2
}
DEFAULT_FINAL_MAX = 2
MAX_TOTAL_RESULTS = 100  # 最终结果上限强制 100 个

def create_http_session():
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http_session = create_http_session()

def fetch_extract_clean_and_sample():
    """
    步骤 1: 抓取源数据
    步骤 2: 提取国家代码后缀 (如 #JP)
    步骤 3: 清洗校验 IP/IPv6 格式及端口合法性
    步骤 4: 按国家配额精细抽样 (300~800 个)
    """
    raw_by_country = defaultdict(set)
    ip_pattern = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+|\[[0-9a-fA-F:]+\]:\d+)')
    
    total_raw_count = 0
    clean_count = 0

    for url in PROXY_SOURCES:
        try:
            res = http_session.get(url, timeout=8)
            if res.status_code != 200:
                continue

            for line in res.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                
                total_raw_count += 1

                # 1. 提取后缀国家代码 (如 1.1.1.1:443#JP -> JP)
                country = "UN"
                if "#" in line:
                    parts = line.split("#")
                    line = parts[0].strip()
                    if len(parts) > 1 and parts[1].strip():
                        country = parts[1].strip().upper()

                # 2. 清洗校验 IP 与端口规范
                match = ip_pattern.search(line)
                if match:
                    ip_port = match.group(1)
                    try:
                        port = int(ip_port.rsplit(":", 1)[-1])
                        # 过滤非 CF 反代端口
                        if port in CF_ALLOWED_PORTS:
                            raw_by_country[country].add(f"{ip_port}#{country}")
                            clean_count += 1
                    except ValueError:
                        pass
        except Exception as e:
            print(f"⚠️ 抓取源失败 {url}: {e}")

    print(f"🔍 [阶段 1: 提取与清洗] 原始数据 {total_raw_count} 行 | 清洗出有效格式 IP {clean_count} 个 | 涵盖 {len(raw_by_country)} 个地区")

    # 3. 抽样预过滤 (300~800 规模)
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
    步骤 5: 连通性测试与响应比对保活
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
        sock.settimeout(0.9)  # 0.9s 超时
        sock.connect((ip, port))

        probe = (
            f"GET /cdn-cgi/trace HTTP/1.1\r\n"
            f"Host: 1.1.1.1\r\n"
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
            f"Connection: close\r\n\r\n"
        )
        sock.sendall(probe.encode("utf-8"))
        response = sock.recv(1536).decode("utf-8", errors="ignore")

        if "HTTP/1.1 200 OK" in response or "HTTP/2 200" in response or "fl=" in response:
            if "loc=" in response:
                match = re.search(r'loc=([A-Z]{2})', response)
                if match and match.group(1) != "UN":
                    return f"{ip_port}#{match.group(1)}"
            
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
    """
    步骤 6: 保活测试并收敛保存最多 100 个有效节点
    """
    start_time = time.time()
    country_buckets = defaultdict(list)
    
    print(f"🚀 [阶段 3: 连通保活测试] 启动并发检测...")

    with ThreadPoolExecutor(max_workers=60) as executor:
        results = executor.map(check_keepalive, sampled_pool)
        
        for item in results:
            if not item:
                continue
            
            _, country = item.rsplit("#", 1)
            max_allowed = FINAL_KV_LIMITS.get(country, DEFAULT_FINAL_MAX)

            # 按国家配额装桶
            if len(country_buckets[country]) < max_allowed:
                country_buckets[country].append(item)

            total_valid = sum(len(v) for v in country_buckets.values())

            # 达到总数硬上限 100 提前截止
            if total_valid >= max_total:
                print(f"⚡ 已经集齐 {max_total} 个高质量有效 IP，提前终止测试！")
                break

    final_entries = []
    for country, nodes in country_buckets.items():
        final_entries.extend(nodes)

    # 再次严格截取前 100 个
    final_entries = final_entries[:max_total]

    duration = round(time.time() - start_time, 2)
    print(f"✨ [阶段 4: 完成] 耗时 {duration}s | 最终提取纯净可用 IP: {len(final_entries)} 个 (含 JP: {len(country_buckets['JP'])} 个)")
    return final_entries

def safe_update_cloudflare_kv(valid_entries, min_required=60):
    if not (CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN):
        print("⚠️ 未配置 Cloudflare KV 环境变量，跳过写入。")
        return

    # 防空/防过少保护
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
    # 提取 -> 清洗 -> 抽样
    sampled_pool = fetch_extract_clean_and_sample()
    if not sampled_pool:
        print("❌ 抽样池为空，终止流程。")
        return

    # 保活测试 -> 截取最多 100 个
    valid_entries = test_and_trim(sampled_pool, max_total=MAX_TOTAL_RESULTS)
        
    # 同步 Cloudflare KV
    safe_update_cloudflare_kv(valid_entries, min_required=60)

if __name__ == "__main__":
    main()
