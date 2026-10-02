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

# ==================== 1. 核心环境变量与配置 ====================
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

# Cloudflare 支持的反代端口
CF_ALLOWED_PORTS = {80, 443, 8080, 8443, 2052, 2053, 2082, 2083, 2086, 2087, 8880}

# 【2号、3号源】：专供 JP 地区，拥有最高检测优先级
JP_PRIORITY_SOURCES = ["https://bestcf.pages.dev/tiancheng/jp.txt","https://bestcf.pages.dev/s5gy/jp.txt",
    # 在此填入你的 2号源、3号源 URL
    # "https://example.com/jp_source_2.txt",
    # "https://example.com/jp_source_3.txt",
]

# 【1号源】：混合多国源（节点海量，参差不齐，用于兜底补齐 JP 及其他国家配额）
MIXED_SOURCES = [
    "https://zip.cm.edu.kg/all.txt",
]

# 日本 Cloudflare 边缘机房代码（命中即校准为真实日本节点）
CF_JP_COLOS = {"NRT", "HND", "KIX", "ITM", "NGO", "FUK", "CTS", "OKA"}

# 各地区配额规划
PRIMARY_LIMITS = {
    "JP": 15,
    "SG": 15,
    "HK": 15,
    "US": 10,
    "GB": 10,
}
JP_MIN_TARGET = 10  # JP 保底必须活着的数量
MAX_TOTAL_RESULTS = 100
REMAINING_POOL_LIMIT = MAX_TOTAL_RESULTS - sum(PRIMARY_LIMITS.values())

CONTINENT_MAP = {
    "AS": {"JP", "HK", "SG", "IN", "KR", "TW", "TH", "VN", "MY", "PH", "ID", "AE", "KH", "PK", "TR"},
    "EU": {"GB", "DE", "FR", "NL", "FI", "IT", "ES", "SE", "PL", "RO", "CH", "AT", "CZ", "UA", "RU", "NO", "DK"},
    "NA": {"US", "CA", "MX", "PR", "CR"},
    "SA": {"BR", "AR", "CL", "CO", "PE"},
    "AF": {"ZA", "EG", "NG", "KE", "MA"},
    "OC": {"AU", "NZ", "FJ"}
}
COUNTRY_TO_CONTINENT = {c: cont for cont, countries in CONTINENT_MAP.items() for c in countries}

def get_continent(country):
    return COUNTRY_TO_CONTINENT.get(country, "UNKNOWN")

def create_http_session():
    session = requests.Session()
    # 保持稳健的连接池规模
    adapter = HTTPAdapter(pool_connections=60, pool_maxsize=60, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http_session = create_http_session()

IP_PORT_PATTERN = re.compile(
    r'(?P<ip>(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)|\[[0-9a-fA-F:]+\])'
    r'[:\s]+(?P<port>\d{2,5})'
)

# ==================== 2. 数据源清洗与提取 ====================
def parse_country_hint(line):
    """从源文本中模糊抓取国家特征"""
    upper = line.upper()
    if any(k in upper for k in ["#JP", "JAPAN", "日本", "TOKYO", "OSAKA"]):
        return "JP"
    if any(k in upper for k in ["#HK", "HONG KONG", "HONGKONG", "香港"]):
        return "HK"
    if any(k in upper for k in ["#SG", "SINGAPORE", "新加坡"]):
        return "SG"
    if any(k in upper for k in ["#US", "UNITED STATES", "USA", "美国"]):
        return "US"
    if any(k in upper for k in ["#GB", "#UK", "UNITED KINGDOM", "英国"]):
        return "GB"
    
    if "#" in line:
        candidate = line.rsplit("#", 1)[-1].strip().upper()[:2]
        if candidate.isalpha():
            return "GB" if candidate == "UK" else candidate
    return "UN"

def fetch_and_clean_source(url):
    """抓取源文本，只提取合法端口与合法 IP，返回 [(ip_port, country_hint)]"""
    valid_items = []
    try:
        # 给源站 20 秒下载宽限，防止慢速大文件被截断
        res = http_session.get(url, timeout=20)
        if res.status_code != 200:
            print(f"⚠️ 源响应非 200 ({res.status_code}): {url}")
            return []
        
        for line in res.text.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue

            match = IP_PORT_PATTERN.search(line)
            if not match:
                continue

            ip = match.group("ip")
            try:
                port = int(match.group("port"))
            except ValueError:
                continue

            if port not in CF_ALLOWED_PORTS:
                continue

            valid_items.append((f"{ip}:{port}", parse_country_hint(line)))
    except Exception as e:
        print(f"⚠️ 抓取源失败 {url}: {e}")
    return valid_items

# ==================== 3. 稳健深度探测逻辑 ====================
def verify_node(item):
    """
    单节点双重校验：
    1. /cdn-cgi/trace 深度检测连通性 + 获取真实机房代码 (colo)
    2. 防 Google 漂移检测（剔除回跳到 HK/CN 的节点）
    """
    ip_port, country = item.rsplit("#", 1)
    port = int(ip_port.rsplit(":", 1)[-1])
    protocol = "https" if port in {443, 8443, 2053, 2083, 2087, 2096} else "http"
    
    cf_headers = {
        "Host": "www.cloudflare.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    trace_url = f"{protocol}://{ip_port}/cdn-cgi/trace"

    try:
        t_start = time.time()
        # 宽容超时：连接 4.0s，读取 5.0s，保证跨洋稳定连接不误判
        res = http_session.get(
            trace_url,
            headers=cf_headers,
            timeout=(4.0, 5.0),
            verify=False,
            allow_redirects=False
        )
        t_cost = (time.time() - t_start) * 1000

        if res.status_code != 200 or "h=" not in res.text:
            return None

        # 读取真实 Colo 机房代码
        colo_match = re.search(r'colo=([A-Z]{3})', res.text)
        colo = colo_match.group(1) if colo_match else ""

        # 只要实际落在日本机房，无条件纠偏为 JP
        if colo in CF_JP_COLOS:
            country = "JP"

        # 重点地区防 Google 识别漂移检测
        if country in {"JP", "SG", "US", "GB"}:
            try:
                g_res = http_session.get(
                    f"http://{ip_port}/",
                    headers={"Host": "www.google.com", "User-Agent": cf_headers["User-Agent"]},
                    timeout=4.0,
                    allow_redirects=False,
                    verify=False
                )
                location = g_res.headers.get("Location", "")
                if "google.com.hk" in location or "google.cn" in location:
                    return None
            except Exception:
                pass

        avg_latency = round(t_cost, 2)
        score = avg_latency

        # 日本节点加权积分倾斜
        if country == "JP":
            score -= 25

        # 严格按照规范：45.32.17.67:443#JP 格式输出
        entry_tag = f"{ip_port}#{country}"

        return {
            "entry": entry_tag,
            "country": country,
            "continent": get_continent(country),
            "latency": avg_latency,
            "score": score
        }
    except Exception:
        return None

def batch_test_nodes(items, max_workers=45):
    """采用中等并发深度测活，避免本地/Runner 端口拥塞"""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for res in executor.map(verify_node, items):
            if res:
                results.append(res)
    return results

# ==================== 4. 优先级调度流水线 ====================
def run_selection_process():
    pipeline_start = time.time()
    seen_ip_ports = set()

    # --- 步骤 1: 加载 2号、3号 JP 专属源 ---
    print("📦 [步骤 1] 正在提取 2号 & 3号 JP 专属源...")
    jp_priority_candidates = []
    for url in JP_PRIORITY_SOURCES:
        records = fetch_and_clean_source(url)
        for ip_port, _ in records:
            if ip_port not in seen_ip_ports:
                seen_ip_ports.add(ip_port)
                jp_priority_candidates.append(f"{ip_port}#JP")

    print(f"   -> 2、3号源去重后候选: {len(jp_priority_candidates)} 个")

    # --- 步骤 2: 加载 1号大杂烩源 ---
    print("📦 [步骤 2] 正在加载 1号混合源...")
    source1_by_country = defaultdict(list)
    for url in MIXED_SOURCES:
        records = fetch_and_clean_source(url)
        for ip_port, country in records:
            if ip_port not in seen_ip_ports:
                seen_ip_ports.add(ip_port)
                source1_by_country[country].append(f"{ip_port}#{country}")

    total_s1 = sum(len(v) for v in source1_by_country.values())
    print(f"   -> 1号源解析完毕: 共提取 {total_s1} 个独立节点 (含预标 JP 节点 {len(source1_by_country.get('JP', []))} 个)")

    # --- 步骤 3: 优先测试 2、3号专属 JP ---
    print("\n🎯 [步骤 3] 优先对 2、3 号专属源进行全量稳健测活...")
    valid_jp_nodes = []
    if jp_priority_candidates:
        test_res = batch_test_nodes(jp_priority_candidates)
        valid_jp_nodes = [n for n in test_res if n["country"] == "JP"]
        print(f"   -> 专属源测活产出: {len(valid_jp_nodes)} 个稳定可用 JP 节点")
    else:
        print("   -> 2、3号源暂未配置或无有效 IP，准备转由 1号源提供 JP 候选。")

    # --- 步骤 4: 评估 JP 缺口，按需从 1号源深挖补齐 ---
    needed_jp = PRIMARY_LIMITS["JP"] - len(valid_jp_nodes)
    if needed_jp > 0:
        print(f"⚠️ 专属源可用 JP ({len(valid_jp_nodes)}/15) 未达到满额，正在从 1号源调派候选深挖补齐...")
        s1_jp_pool = source1_by_country.get("JP", [])
        random.shuffle(s1_jp_pool)
        
        # 宽裕抽样 250 个候选，应对 1号源质量参差不齐的问题
        sample_jp_from_s1 = s1_jp_pool[:250]
        if sample_jp_from_s1:
            print(f"   -> 从 1号源抽取 {len(sample_jp_from_s1)} 个 JP 节点进行实测...")
            s1_jp_results = batch_test_nodes(sample_jp_from_s1)
            extra_jp = [n for n in s1_jp_results if n["country"] == "JP"]
            print(f"   -> 1号源成功补入: {len(extra_jp)} 个可用 JP 节点")
            valid_jp_nodes.extend(extra_jp)
    else:
        print("🎉 2、3号源已直接供给满 15 个可用 JP 节点，跳过 1号源的 JP 探测！")

    # 对所有收集到的存活 JP 节点按延迟得分排序，锁定最终 15 个名额
    valid_jp_nodes.sort(key=lambda x: x["score"])
    selected_jp = valid_jp_nodes[:PRIMARY_LIMITS["JP"]]

    # --- 步骤 5: 测试 1号源的多国节点 (SG/HK/US/GB等) ---
    print("\n🌐 [步骤 5] 正在测试 1号源其他地区节点...")
    other_candidates = []
    for c in ["SG", "HK", "US", "GB"]:
        nodes = source1_by_country.get(c, [])
        random.shuffle(nodes)
        other_candidates.extend(nodes[:80])

    # 抽样未标记节点，利用 Cloudflare trace 逆向嗅探
    un_nodes = source1_by_country.get("UN", [])
    random.shuffle(un_nodes)
    other_candidates.extend(un_nodes[:120])

    # 兼顾其它大洲少量节点
    for c, nodes in source1_by_country.items():
        if c not in {"JP", "SG", "HK", "US", "GB", "UN"}:
            random.shuffle(nodes)
            other_candidates.extend(nodes[:20])

    random.shuffle(other_candidates)
    valid_others = batch_test_nodes(other_candidates)

    # --- 步骤 6: 汇总配额，组合最终数据 ---
    primary_buckets = defaultdict(list)
    primary_buckets["JP"] = [n["entry"] for n in selected_jp]

    other_by_continent = defaultdict(list)
    valid_others.sort(key=lambda x: x["score"])

    for res in valid_others:
        c = res["country"]
        cont = res["continent"]
        entry = res["entry"]

        if c in PRIMARY_LIMITS and c != "JP":
            if len(primary_buckets[c]) < PRIMARY_LIMITS[c]:
                primary_buckets[c].append(entry)
        else:
            other_by_continent[cont].append(entry)

    # 平衡各大洲节点
    other_selected = []
    for cont in ["AS", "EU", "NA", "SA", "AF", "OC"]:
        if other_by_continent[cont]:
            chosen = random.choice(other_by_continent[cont])
            other_selected.append(chosen)
            other_by_continent[cont].remove(chosen)

    remaining_pool = [node for nodes in other_by_continent.values() for node in nodes]
    random.shuffle(remaining_pool)
    slots_left = REMAINING_POOL_LIMIT - len(other_selected)
    if slots_left > 0:
        other_selected.extend(remaining_pool[:slots_left])

    final_entries = []
    for c in ["JP", "SG", "HK", "GB", "US"]:
        final_entries.extend(primary_buckets[c])
    final_entries.extend(other_selected)
    final_entries = final_entries[:MAX_TOTAL_RESULTS]

    total_cost = round(time.time() - pipeline_start, 2)
    jp_count = len(primary_buckets["JP"])

    print(f"\n==================== 探测报告 ====================")
    print(f"⏱️ 总耗时: {total_cost} 秒")
    print(f"📊 核心配额锁定状态:")
    print(f"   - JP (日本): {jp_count}/15 (保底要求 >= {JP_MIN_TARGET})")
    print(f"   - SG (新加坡): {len(primary_buckets['SG'])}/15")
    print(f"   - HK (香港): {len(primary_buckets['HK'])}/15")
    print(f"   - US (美国): {len(primary_buckets['US'])}/10")
    print(f"   - GB (英国): {len(primary_buckets['GB'])}/10")
    print(f"   - 其他大洲补充: {len(other_selected)} 个")
    print(f"📌 最终输出总节点数: {len(final_entries)} 个")

    if jp_count >= JP_MIN_TARGET:
        print(f"✅ JP 质量达标: 成功锁定 {jp_count} 个经实测存活的日本节点！")
    else:
        print(f"⚠️ JP 可用数预警: 本轮仅选出 {jp_count} 个可用 JP 节点，未达 {JP_MIN_TARGET} 底线，请检查 2、3号专属源。")

    return final_entries

# ==================== 5. 写入 Cloudflare KV ====================
def safe_update_cloudflare_kv(valid_entries, min_required=15):
    if not (CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN):
        print("\n⚠️ 未配置 Cloudflare KV 环境变量，以下为前 8 个输出样例预览：")
        for item in valid_entries[:8]:
            print(f"   {item}")
        return

    if len(valid_entries) < min_required:
        print(f"⚠️ 存活节点总数 ({len(valid_entries)}) 低于安全阈值 ({min_required})，拒绝写入以保护旧数据。")
        return

    kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "text/plain; charset=utf-8"
    }

    try:
        # KV 数据格式即为带有 #国家 后缀的多行文本
        payload = "\n".join(valid_entries).encode('utf-8')
        res = http_session.put(kv_url, data=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            print("🎉 成功同步写入 Cloudflare KV！数据已刷新。")
        else:
            print(f"❌ 写入 KV 失败: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ 写入 KV 网络异常: {e}")

def main():
    final_entries = run_selection_process()
    if not final_entries:
        print("❌ 未产生有效数据，退出。")
        return
    safe_update_cloudflare_kv(final_entries, min_required=15)

if __name__ == "__main__":
    main()
