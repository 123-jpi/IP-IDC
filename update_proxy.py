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
PROXY_SOURCES = ["https://zip.cm.edu.kg/all.txt"]

# ----------------- 大洲与国家代码映射表 -----------------
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

# ----------------- 调整后的配额结构 (重质不重量 + GB提升) -----------------
PRIMARY_LIMITS = {
    "JP": 15,  # 宁缺毋滥，精选 15 个高质日本 IP
    "SG": 15,  # 亚太主力
    "HK": 10,  # 亚太主力
    "US": 10,  # 美洲主力
    "GB": 10,  # 欧洲主力（已提升）
}
MAX_TOTAL_RESULTS = 100
REMAINING_POOL_LIMIT = MAX_TOTAL_RESULTS - sum(PRIMARY_LIMITS.values())  # 34 个名额给大洲保底与池子动态填充

def get_continent(country):
    return COUNTRY_TO_CONTINENT.get(country, "UNKNOWN")

def create_http_session():
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50)
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

    print(f"🔍 [阶段 1: 清洗] 原始数据 {total_raw_count} 行 | 有效 IP {clean_count} 个 | 涵盖 {len(raw_by_country)} 个地区")

    sampled_pool = []
    for country, nodes in raw_by_country.items():
        node_list = list(nodes)
        random.shuffle(node_list)

        cont = get_continent(country)
        # 大基数抽样：JP 虽只取 15 个，但仍从 180 个样本中挑选，实现更高筛选比
        if country == "JP":
            sample_size = 180
        elif country in {"SG", "HK", "US", "GB"}:
            sample_size = 80
        elif cont in {"AF", "SA", "OC"}:
            sample_size = 20
        else:
            sample_size = 15

        sampled_pool.extend(node_list[:sample_size])

    random.shuffle(sampled_pool)
    print(f"🎯 [阶段 2: 精细抽样] 提取 {len(sampled_pool)} 个样本进入连通性与质量测试")
    return sampled_pool

def check_keepalive(item):
    """
    针对 JP 进行 3 轮严格探针测试，剔除高丢包与虚假低延迟节点
    """
    ip_port, raw_country = item.rsplit("#", 1)
    
    headers = {
        "Host": "www.cloudflare.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    port = int(ip_port.rsplit(":", 1)[-1])
    protocol = "https" if port in {443, 8443, 2053, 2083, 2087, 2096} else "http"
    url = f"{protocol}://{ip_port}/cdn-cgi/trace"

    latencies = []
    real_country = raw_country
    
    # JP 节点开启 3 轮严格测试；其他地区 2 轮测试
    rounds = 3 if raw_country == "JP" else 2
    timeout_val = 1.3 if raw_country == "JP" else 1.6

    try:
        for _ in range(rounds):
            t_start = time.time()
            res = http_session.get(
                url, 
                headers=headers, 
                timeout=timeout_val, 
                verify=False, 
                allow_redirects=False
            )
            t_cost = (time.time() - t_start) * 1000

            if res.status_code == 200 and "h=" in res.text and "colo=" in res.text:
                latencies.append(t_cost)
                loc_match = re.search(r'loc=([A-Z]{2})', res.text)
                if loc_match:
                    detected_loc = loc_match.group(1)
                    if detected_loc != "UN":
                        real_country = "GB" if detected_loc == "UK" else detected_loc
            else:
                return None  # 任何一次丢包/超时直接淘汰

        avg_latency = round(sum(latencies) / len(latencies), 2)

        # 评分机制：极低延迟与 JP/亚太节点获得优先权
        score = avg_latency
        if real_country == "JP":
            score -= 20
        elif real_country in {"HK", "SG"}:
            score -= 10

        return {
            "entry": f"{ip_port}#{real_country}",
            "country": real_country,
            "continent": get_continent(real_country),
            "latency": avg_latency,
            "score": score
        }

    except Exception:
        return None

def test_and_trim(sampled_pool, max_total=MAX_TOTAL_RESULTS):
    start_time = time.time()
    
    print(f"🚀 [阶段 3: 并发测试与质量评估] 启动检测 (线程数: 40)...")
    
    valid_results = []
    with ThreadPoolExecutor(max_workers=40) as executor:
        futures = [executor.submit(check_keepalive, item) for item in sampled_pool]
        for future in futures:
            res = future.result()
            if res:
                valid_results.append(res)

    # 按照综合质量得分（Score）升序排序
    valid_results.sort(key=lambda x: x["score"])

    # 分组存储
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

    # ------------ 核心大洲覆盖与池子动态分配算法 ------------
    other_selected = []
    all_continents = ["AS", "EU", "NA", "SA", "AF", "OC"]

    # 1. 强制在各大洲的非主要国家中抽取 1 个节点保底（保障全球覆盖）
    for cont in all_continents:
        if other_by_continent[cont]:
            chosen = random.choice(other_by_continent[cont])
            other_selected.append(chosen)
            other_by_continent[cont].remove(chosen)

    # 2. 剩余 28 个名额从池子中所有非主要地区节点里打散填充
    remaining_pool = []
    for cont, nodes in other_by_continent.items():
        remaining_pool.extend(nodes)

    random.shuffle(remaining_pool)
    slots_left = REMAINING_POOL_LIMIT - len(other_selected)
    if slots_left > 0:
        other_selected.extend(remaining_pool[:slots_left])

    # 汇总最终结果
    final_entries = []
    for c in ["JP", "SG", "HK", "GB", "US"]:
        final_entries.extend(primary_buckets[c])
    final_entries.extend(other_selected)

    final_entries = final_entries[:max_total]
    duration = round(time.time() - start_time, 2)

    # 统计各大洲分布
    continent_counts = defaultdict(int)
    for entry in final_entries:
        ct = entry.rsplit("#", 1)[-1]
        continent_counts[get_continent(ct)] += 1

    print(f"✨ [阶段 4: 完成] 耗时 {duration}s | 写入节点: {len(final_entries)} 个")
    print(f"📊 主要地区 -> JP: {len(primary_buckets['JP'])}/15 | SG: {len(primary_buckets['SG'])}/15 | HK: {len(primary_buckets['HK'])}/10 | GB: {len(primary_buckets['GB'])}/10 | US: {len(primary_buckets['US'])}/10")
    print(f"🌍 6大洲分布 -> 亚洲(AS): {continent_counts['AS']} | 欧洲(EU): {continent_counts['EU']} | 北美(NA): {continent_counts['NA']} | 南美(SA): {continent_counts['SA']} | 非洲(AF): {continent_counts['AF']} | 大洋洲(OC): {continent_counts['OC']}")

    return final_entries

def safe_update_cloudflare_kv(valid_entries, min_required=30):
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
            print(f"🎉 成功同步更新 Cloudflare KV！已更新 15 个精选 JP 及 85 个全球优质 IP。")
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
    safe_update_cloudflare_kv(valid_entries, min_required=30)

if __name__ == "__main__":
    main()
