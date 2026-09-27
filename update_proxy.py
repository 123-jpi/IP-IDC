import os
import re
import time
import random
import requests
from collections import defaultdict
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 环境变量
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

# Cloudflare 支持的反代端口
CF_ALLOWED_PORTS = {80, 443, 8080, 8443, 2052, 2053, 2082, 2083, 2086, 2087, 8880}

# 源地址
PROXY_SOURCES = ["https://zip.cm.edu.kg/all.txt"]

CONTINENT_MAP = {
    "AS": {"JP", "HK", "SG", "IN", "KR", "TW", "TH", "VN", "MY", "PH", "ID", "AE", "KH", "PK", "TR"},
    "EU": {"GB", "DE", "FR", "NL", "FI", "IT", "ES", "SE", "PL", "RO", "CH", "AT", "CZ", "UA", "RU", "NO", "DK"},
    "NA": {"US", "CA", "MX", "PR", "CR"},
    "SA": {"BR", "AR", "CL", "CO", "PE"},
    "AF": {"ZA", "EG", "NG", "KE", "MA"},
    "OC": {"AU", "NZ", "FJ"}
}

COUNTRY_TO_CONTINENT = {}
for cont, countries in CONTINENT_MAP.items():
    for c in countries:
        COUNTRY_TO_CONTINENT[c] = cont

PRIMARY_LIMITS = {
    "JP": 15,
    "SG": 15,
    "HK": 15,
    "US": 10,
    "GB": 10,
}
MAX_TOTAL_RESULTS = 100
REMAINING_POOL_LIMIT = MAX_TOTAL_RESULTS - sum(PRIMARY_LIMITS.values())

def get_continent(country):
    return COUNTRY_TO_CONTINENT.get(country, "UNKNOWN")

def create_http_session():
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
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
            res = http_session.get(url, timeout=15)
            if res.status_code != 200:
                continue

            for line in res.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                
                total_raw_count += 1

                country = "UN"
                if "#" in line:
                    parts = line.split("#")
                    line = parts[0].strip()
                    if len(parts) > 1 and parts[1].strip():
                        country = parts[1].strip().upper()
                        if country == "UK":
                            country = "GB"

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

    print(f"🔍 [阶段 1: 解析抽取] 原始总行数 {total_raw_count} | 匹配有效端口 IP {clean_count} 个")

    sampled_pool = []
    for country, nodes in raw_by_country.items():
        node_list = list(nodes)
        random.shuffle(node_list)

        if country in {"JP", "SG", "HK", "US", "GB"}:
            sample_size = 150
        else:
            sample_size = 30

        sampled_pool.extend(node_list[:sample_size])

    random.shuffle(sampled_pool)
    print(f"🎯 [阶段 2: 样本分配] 已抽取 {len(sampled_pool)} 个样本进入连通性 & Google 重定向测试")
    return sampled_pool

def check_keepalive_and_google_redirect(item):
    ip_port, country = item.rsplit("#", 1)
    
    port = int(ip_port.rsplit(":", 1)[-1])
    protocol = "https" if port in {443, 8443, 2053, 2083, 2087, 2096} else "http"
    
    cf_headers = {
        "Host": "www.cloudflare.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    trace_url = f"{protocol}://{ip_port}/cdn-cgi/trace"

    try:
        # 1. 基础连通性测试
        t_start = time.time()
        res = http_session.get(trace_url, headers=cf_headers, timeout=1.5, verify=False, allow_redirects=False)
        t_cost = (time.time() - t_start) * 1000

        if not (res.status_code == 200 and "h=" in res.text):
            return None

        # 2. 针对非 HK 节点的精准 Google 漂移检测
        # 通过 Host 探测 Cloudflare 边缘节点对 Google 请求的处理逻辑
        if country in {"JP", "SG", "US", "GB"}:
            try:
                g_res = http_session.get(
                    f"http://{ip_port}/",
                    headers={"Host": "www.google.com", "User-Agent": cf_headers["User-Agent"]},
                    timeout=1.5,
                    allow_redirects=False,
                    verify=False
                )
                location = g_res.headers.get("Location", "")
                
                # 精准拦截：如果明确重定向到了 google.com.hk 或 google.cn，说明该节点会导致 Google 识别回 HK，直接踢掉！
                if "google.com.hk" in location or "google.cn" in location:
                    return None
            except Exception:
                pass

        avg_latency = round(t_cost, 2)
        score = avg_latency
        
        if country == "JP":
            score -= 15
        elif country in {"SG", "US"}:
            score -= 10

        return {
            "entry": f"{ip_port}#{country}",
            "country": country,
            "continent": get_continent(country),
            "latency": avg_latency,
            "score": score
        }
    except Exception:
        pass

    return None

def test_and_trim(sampled_pool, max_total=MAX_TOTAL_RESULTS):
    start_time = time.time()
    
    print(f"🚀 [阶段 3: 并发筛选] 筛选响应快且无 Google HK 漂移的真正纯净节点...")
    
    valid_results = []
    with ThreadPoolExecutor(max_workers=60) as executor:
        futures = [executor.submit(check_keepalive_and_google_redirect, item) for item in sampled_pool]
        for future in futures:
            res = future.result()
            if res:
                valid_results.append(res)

    print(f"🧪 测试完成：筛出 {len(valid_results)} 个连通且防漂移的高响应节点。")

    valid_results.sort(key=lambda x: x["score"])

    primary_buckets = defaultdict(list)
    other_by_continent = defaultdict(list)

    for res in valid_results:
        c = res["country"]
        cont = res["continent"]
        entry = res["entry"]

        if c in PRIMARY_LIMITS:
            if len(primary_buckets[c]) < PRIMARY_LIMITS[c]:
                primary_buckets[c].append(entry)
        else:
            other_by_continent[cont].append(entry)

    other_selected = []
    all_continents = ["AS", "EU", "NA", "SA", "AF", "OC"]

    for cont in all_continents:
        if other_by_continent[cont]:
            chosen = random.choice(other_by_continent[cont])
            other_selected.append(chosen)
            other_by_continent[cont].remove(chosen)

    remaining_pool = []
    for cont, nodes in other_by_continent.items():
        remaining_pool.extend(nodes)

    random.shuffle(remaining_pool)
    slots_left = REMAINING_POOL_LIMIT - len(other_selected)
    if slots_left > 0:
        other_selected.extend(remaining_pool[:slots_left])

    final_entries = []
    for c in ["JP", "SG", "HK", "GB", "US"]:
        final_entries.extend(primary_buckets[c])
    final_entries.extend(other_selected)

    final_entries = final_entries[:max_total]
    duration = round(time.time() - start_time, 2)

    print(f"✨ [阶段 4: 完成] 耗时 {duration}s | 最终入库节点: {len(final_entries)} 个")
    print(f"📊 主要地区 -> JP: {len(primary_buckets['JP'])}/15 | SG: {len(primary_buckets['SG'])}/15 | HK: {len(primary_buckets['HK'])}/15 | US: {len(primary_buckets['US'])}/10 | GB: {len(primary_buckets['GB'])}/10")

    return final_entries

def safe_update_cloudflare_kv(valid_entries, min_required=15):
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
            print(f"🎉 成功同步更新 Cloudflare KV！已精准排除漂移节点，确保选出的 JP/SG/US 节点解锁 Gemini！")
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
    safe_update_cloudflare_kv(valid_entries, min_required=15)

if __name__ == "__main__":
    main()
