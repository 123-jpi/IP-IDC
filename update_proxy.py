import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor

# 从 GitHub Secrets 获取配置参数
CHECKER_URL = os.environ.get("CHECKER_URL", "").rstrip('/')
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

# 公开 IP 抓取源（可以自己往里面加其他 txt 网址）
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/cmliu/CF-Workers-SUB/main/proxyip.txt",
]

def fetch_candidate_ips():
    """从网络公开源抓取候选 IP:PORT"""
    candidates = set()
    # 正则表达式识别 IPv4:端口 和 [IPv6]:端口
    ip_pattern = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+|\[[0-9a-fA-F:]+\]:\d+)')
    
    for url in PROXY_SOURCES:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                matches = ip_pattern.findall(res.text)
                candidates.update(matches)
                print(f"从 {url} 获取到 {len(matches)} 个潜在 IP")
        except Exception as e:
            print(f"抓取源失败 {url}: {e}")
            
    print(f"合并去重后，共有 {len(candidates)} 个候选 IP 待检测")
    return list(candidates)

def get_flag_emoji(country_code):
    """把国家代码（如 US）转成国旗 Emoji（如 🇺🇸）"""
    if not country_code or len(country_code) != 2:
        return "🌐"
    return "".join(chr(127397 + ord(c)) for c in country_code.upper())

def check_ip(ip_port):
    """调用 cmliu 的 /check API 校验 IP 是否可用，并获取延迟和位置信息"""
    url = f"{CHECKER_URL}/check?proxyip={ip_port}"
    try:
        res = requests.get(url, timeout=6)
        if res.status_code == 200:
            data = res.json()
            # 如果测速节点返回 success 为 true，说明此 IP 可用
            if data.get("success") is True:
                loc = data.get("loc", "🌐").upper()
                colo = data.get("colo", "UNK").upper()
                delay = data.get("responseTime", 999)
                flag = get_flag_emoji(loc)
                
                # 拼接成 edgetunnel 认识的格式：IP:端口#国旗 国家 - 机房 - [延迟ms]
                formatted_tag = f"{ip_port}#{flag} {loc} - {colo} - [{delay}ms]"
                
                return {
                    "entry": formatted_tag,
                    "delay": delay
                }
    except Exception:
        pass
    return None

def update_cloudflare_kv(valid_items):
    """把筛选出来的优质 IP 发送到 Cloudflare KV 存储"""
    kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "text/plain; charset=utf-8"
    }
    
    content = "\n".join([item["entry"] for item in valid_items])
    
    res = requests.put(kv_url, data=content.encode('utf-8'), headers=headers)
    if res.status_code == 200:
        print("成功把优质 IP 存储到 Cloudflare KV！")
    else:
        print(f"写入 KV 失败，错误码: {res.status_code}，原因: {res.text}")

def main():
    raw_ips = fetch_candidate_ips()
    valid_items = []
    
    print("开始多线程检测 IP 中...")
    # 使用 20 个并发线程测试 IP
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = executor.map(check_ip, raw_ips)
        for r in results:
            if r:
                valid_items.append(r)
                
    # 按延迟从小到大排序，只保留延迟最低的前 50 个 IP
    valid_items.sort(key=lambda x: x["delay"])
    best_ips = valid_items[:50]
    
    print(f"检测完成！共找到 {len(valid_items)} 个活 IP，精选出前 {len(best_ips)} 个最佳 IP")
    
    if best_ips and CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN:
        update_cloudflare_kv(best_ips)
    else:
        print("缺少必填的环境变量，或者没有找到有效的 IP，跳过 KV 写入。")

if __name__ == "__main__":
    main()
