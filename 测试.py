import requests, json

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://music.163.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

def get(aid, offset, limit):
    url = f"https://music.163.com/api/artist/albums/{aid}?offset={offset}&limit={limit}"
    r = requests.get(url, headers=headers, timeout=15)
    try:
        return r.status_code, r.json()
    except Exception as e:
        return r.status_code, {"_raw_head": r.text[:300]}

# 1. 正常首页：字段结构
st, data = get(53678173, 0, 12)
print("=== 1) offset=0 limit=12 ===")
print("status:", st)
print("code:", data.get("code"))
ha = data.get("hotAlbums") or []
print("hotAlbums 长度:", len(ha))
if ha:
    print("第一项 keys:", list(ha[0].keys()))
    print("第一项内容:", json.dumps(ha[0], ensure_ascii=False)[:600])

# 2. 大 limit 是否生效
st, data = get(53678173, 0, 50)
ha = data.get("hotAlbums") or []
print("\n=== 2) offset=0 limit=50 ===")
print("status:", st, "hotAlbums 长度:", len(ha))

# 3. 翻到末尾（该歌手 44 张）
for off in [36, 44, 48, 60, 100]:
    st, data = get(53678173, off, 12)
    ha = data.get("hotAlbums") or []
    print(f"\n=== 3) offset={off} limit=12 ===")
    print("status:", st, "code:", data.get("code"), "hotAlbums 长度:", len(ha))
    print("raw head:", json.dumps(data, ensure_ascii=False)[:200])

# 4. 无效歌手 ID
for bad in [0, 1, 99999999999]:
    st, data = get(bad, 0, 12)
    print(f"\n=== 4) 无效 ID={bad} ===")
    print("status:", st, "raw head:", json.dumps(data, ensure_ascii=False)[:300])