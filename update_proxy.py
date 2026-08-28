import os
import re
import socket
import requests
from concurrent.futures import ThreadPoolExecutor

# 从 GitHub Secrets 获取环境变量配置
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.environ.get("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN")

# Cloudflare 允许的官方 HTTP/HTTPS 代理端口集合
CF_ALLOWED_PORTS = {80, 443, 8080, 8443, 2052, 2053, 2082, 2083, 2086, 2087, 8880}

# 抓取源：可以自行扩展包含 IP:PORT 文本的公开源
PROXY_SOURCES = [
    "https://zip.cm.edu.kg/all.txt",
]

def fetch_candidate_ips():
    """从源网站搜集 IP，并筛选出符合 CF 端口规范的候选 IP"""
    candidates = set()
    # 匹配 IPv4:PORT 以及 [IPv6]:PORT
    ip_pattern = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+|\[[0-9a-fA-F:]+\]:\d+)')
    
    for url in PROXY_SOURCES:
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                matches = ip_pattern.findall(res.text)
                for item in matches:
                    port = int(item.rsplit(":", 1)[-1])
                    if port in CF_ALLOWED_PORTS:
                        candidates.add(item)
                print(f"从 {url} 获取并筛选出 {len(candidates)} 个合规端口候选 IP")
        except Exception as e:
            print(f"抓取源失败 {url}: {e}")
            
    # 限制单次测试前 200 个，确保数秒内完成测试
    raw_list = list(candidates)[:200]
    print(f"搜集完成，准备进行 CF 握手测速的 IP 总数: {len(raw_list)} 个")
    return raw_list

def check_cf_proxy_compatibility(ip_port):
    """
    模拟 Cloudflare 流量发起请求，校验该 IP 是否能为 Cloudflare 提供反代落地
    """
    try:
        proxies = {
            "http": f"http://{ip_port}",
            "https": f"http://{ip_port}"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Host": "1.1.1.1"
        }
        
        # 1.5 秒超时快速淘汰无效 IP
        res = requests.get(
            "http://1.1.1.1/cdn-cgi/trace", 
            proxies=proxies, 
            headers=headers, 
            timeout=1.5
        )
        
        # 响应包含 Cloudflare trace 特征字符 "h=" 证明连通性良好
        if res.status_code == 200 and "h=" in res.text:
            ip = ip_port.rsplit(":", 1)[0].replace("[", "").replace("]", "")
            return {"ip_port": ip_port, "ip": ip}
    except Exception:
        pass
    return None

def get_country_code(ip):
    """获取 IP 所属国家代码（如 US, JP, HK）"""
    try:
        res = requests.get(f"http://ip-api.com/json/{ip}?fields=countryCode", timeout=2)
        if res.status_code == 200:
            return res.json().get("countryCode", "🌐")
    except Exception:
        pass
    return "🌐"

def get_flag_emoji(country_code):
    """根据国家代码转为对应的国旗 Emoji"""
    if not country_code or len(country_code) != 2:
        return "🌐"
    return "".join(chr(127397 + ord(c)) for c in country_code.upper())

def process_ip(ip_port):
    result = check_cf_proxy_compatibility(ip_port)
    if result:
        country = get_country_code(result["ip"])
        flag = get_flag_emoji(country)
        # 拼装为 edgetunnel 自动读取节点命名的格式：IP:PORT#国旗 国家 - [CF OK]
        formatted_entry = f"{ip_port}#{flag} {country} - [CF OK]"
        return formatted_entry
    return None

def delete_old_kv():
    """擦除旧的 KV 数据，防止失效 IP 遗留"""
    kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}"
    }
    try:
        res = requests.delete(kv_url, headers=headers)
        if res.status_code == 200:
            print("🧹 已成功彻底擦除上一轮旧的 KV 数据！")
        else:
            print(f"⚠️ 清理旧 KV 状态响应: {res.status_code} (可能原本即为空)")
    except Exception as e:
        print(f"⚠️ 清除旧 KV 时发生异常: {e}")

def update_cloudflare_kv(valid_entries):
    # 1. 无论本轮是否有活 IP，先彻底清除旧值
    delete_old_kv()

    # 2. 如果存在有效 IP，写入 KV
    if valid_entries:
        kv_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/BEST_PROXY_IPS"
        headers = {
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "text/plain; charset=utf-8"
        }
        content = "\n".join(valid_entries)
        res = requests.put(kv_url, data=content.encode('utf-8'), headers=headers)
        if res.status_code == 200:
            print(f"🎉 成功将本轮精选的 {len(valid_entries)} 个优质 CF 落地 IP 写入 KV！")
        else:
            print(f"❌ 写入 KV 失败: {res.status_code} - {res.text}")
    else:
        print("⚠️ 本轮未筛选出通过连通校验的 IP，KV 已保持空状态。")

def main():
    raw_ips = fetch_candidate_ips()
    valid_entries = []
    
    print("🚀 开始在 GitHub 环境上并发测试 Cloudflare 落地连通性...")
    # 40 线程并发测试
    with ThreadPoolExecutor(max_workers=40) as executor:
        results = executor.map(process_ip, raw_ips)
        for r in results:
            if r:
                valid_entries.append(r)
                
    print(f"联通性测试完成！通过测试的存活 IP: {len(valid_entries)} 个")
    
    if CF_ACCOUNT_ID and CF_KV_NAMESPACE_ID and CF_API_TOKEN:
        update_cloudflare_kv(valid_entries)
    else:
        print("❌ 缺少必要的 GitHub Secrets 环境变量，跳过 KV 操作。")

if __name__ == "__main__":
    main()
