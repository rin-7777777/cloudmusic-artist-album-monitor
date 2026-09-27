# -*- coding: utf-8 -*-
"""
网易云音乐歌手专辑监控（单文件脚本）

职责
----
读取「歌手配置.txt」→ 通过网易云公开 API 逐页抓取每位歌手的全部专辑 →
与「专辑快照.json」中的旧快照按专辑ID对比 → 把新增/下架写入「监控日志.log」→
全部歌手处理完毕后，一次性把新快照写回 JSON。

关键设计
--------
1. 数据源固定为公开 API：GET https://music.163.com/api/artist/albums/{歌手ID}?offset=&limit=
   网页版 HTML 已不含专辑数据，因此本脚本不做任何 HTML 解析。
2. 全同步顺序请求（requests），不使用异步 / 多线程 / Selenium。
3. 抓取失败或歌手ID无效时，绝不用新数据覆盖该歌手的旧专辑列表，
   只更新「状态 / 上一次尝试时间 / 上一次失败原因」。
4. 单个歌手失败不影响其他歌手；快照在全部处理完后整体一次写回（原子替换）。
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ============================================================ 常量与配置

BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = BASE_DIR / "歌手配置.txt"      # 文件C：歌手配置（只读）
SNAPSHOT_FILE = BASE_DIR / "专辑快照.json"   # 文件A：全量快照（读写）
LOG_FILE = BASE_DIR / "监控日志.log"         # 文件B：日志（追加）

API_URL = "https://music.163.com/api/artist/albums/{artist_id}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

REQUEST_TIMEOUT = 15          # 单次请求超时（秒）
MAX_ATTEMPTS = 3              # 网络类错误的总尝试次数（首次 + 重试 2 次）
RETRY_DELAY_RANGE = (2.0, 3.0)    # 重试间隔随机区间（秒）
PAGE_DELAY_RANGE = (1.5, 3.5)     # 每页之间的随机延时（秒）
ARTIST_DELAY_RANGE = (5.0, 10.0)  # 每个歌手处理完毕后的随机延时（秒）
DEFAULT_LIMIT = 50            # 每页条数默认值
MAX_LIMIT_WARN = 200          # 超过此值仅提示，不擅自修改
MAX_PAGES = 200               # 翻页数量保护上限，防止死循环

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_INVALID = "invalid"

LOG_REASON_MAX = 200          # 写入日志的失败原因最大长度（超出只截断日志，快照保留全文）

# 快照中的字段名（中文，便于直接阅读）
FIELD_NAME = "歌手名"
FIELD_ID = "歌手ID"
FIELD_STATUS = "状态"
FIELD_ALBUMS = "专辑列表"
FIELD_LAST_OK = "上一次成功时间"
FIELD_LAST_TRY = "上一次尝试时间"
FIELD_REASON = "上一次失败原因"


# ============================================================ 自定义异常

class ArtistInvalid(Exception):
    """歌手ID无效：API 返回 code != 200，或 artist 为 null。不重试。"""


class FetchFailed(Exception):
    """网络类错误重试耗尽，或返回数据格式异常。"""


# ============================================================ 通用小工具

def now_str() -> str:
    """当前本地时间，格式 YYYY-MM-DD HH:MM:SS。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cprint(message: str = "") -> None:
    """控制台输出并立即刷新（便于被重定向时实时查看进度）。"""
    print(message, flush=True)


def sleep_random(delay_range) -> float:
    """在给定区间内随机休眠，返回实际休眠秒数。"""
    delay = random.uniform(*delay_range)
    time.sleep(delay)
    return delay


def ms_to_date(value) -> str:
    """毫秒时间戳 → 'YYYY-MM-DD'；缺失或非法时返回 '未知'。"""
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return "未知"
    if timestamp <= 0:
        return "未知"
    try:
        return datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "未知"


def clean_field(value) -> str:
    """日志字段清洗：压缩空白并去掉竖线/换行，避免破坏 '|' 分隔格式。"""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return " ".join(text.split()).replace("|", "/")


def log_reason(value, max_length: int = LOG_REASON_MAX) -> str:
    """
    失败原因写入日志前清洗并截断（网络错误信息里常带整段 URL，过长影响阅读）。
    完整原因仍完整保存在「专辑快照.json」中。
    """
    text = clean_field(value)
    if len(text) <= max_length:
        return text
    return text[:max_length] + "…（已截断，完整原因见专辑快照.json）"


def read_text_auto(path: Path) -> str:
    """
    读取文本文件：优先 UTF-8（兼容 BOM），失败则退回 GBK。
    文件不存在时返回空字符串。
    """
    if not path.exists():
        return ""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ============================================================ 文件C：读取歌手配置

LIMIT_PATTERN = re.compile(r"^limit\s*=\s*(\d+)$", re.IGNORECASE)


def parse_config(path: Path = CONFIG_FILE):
    """
    解析「歌手配置.txt」。

    支持的行：
        LIMIT = 50            → 全局每页条数
        歌手名 | 歌手ID        → 一个待监控的歌手
        以 # 开头的注释行、空行 → 忽略

    :return: (artists, limit)，artists 为 [(歌手名, 歌手ID字符串), ...]
    """
    artists = []
    seen_ids = set()
    limit = DEFAULT_LIMIT

    text = read_text_auto(path)
    if not text.strip():
        cprint(f"[警告] 未找到配置文件或内容为空：{path}")
        return artists, limit

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        # 全角竖线统一成半角，避免中文输入法带来的格式问题
        line = raw_line.strip().replace("｜", "|")
        if not line or line.startswith("#"):
            continue

        # 全局配置行：LIMIT = 50
        matched = LIMIT_PATTERN.match(line)
        if matched:
            value = int(matched.group(1))
            if value < 1:
                cprint(f"[警告] 第 {lineno} 行 LIMIT={value} 不合法，改用默认值 {DEFAULT_LIMIT}")
            else:
                limit = value
                if value > MAX_LIMIT_WARN:
                    cprint(f"[提示] 第 {lineno} 行 LIMIT={value} 偏大，仍按你的设置执行")
            continue

        # 歌手行：歌手名 | 歌手ID
        if "|" not in line:
            cprint(f"[警告] 第 {lineno} 行格式不合法，已跳过：{raw_line.strip()}")
            continue
        name, _, artist_id = line.partition("|")
        name, artist_id = name.strip(), artist_id.strip()
        if not name or not artist_id.isdigit():
            cprint(f"[警告] 第 {lineno} 行格式不合法，已跳过：{raw_line.strip()}")
            continue
        if artist_id in seen_ids:
            cprint(f"[警告] 第 {lineno} 行歌手ID {artist_id} 重复，已跳过：{name}")
            continue
        seen_ids.add(artist_id)
        artists.append((name, artist_id))

    return artists, limit


# ============================================================ 文件A：读写快照

def make_block(name, artist_id, status, albums, last_ok, last_try, reason) -> dict:
    """构造一位歌手的快照区块。"""
    return {
        FIELD_NAME: name,
        FIELD_ID: int(artist_id),
        FIELD_STATUS: status,
        FIELD_ALBUMS: albums,
        FIELD_LAST_OK: last_ok,
        FIELD_LAST_TRY: last_try,
        FIELD_REASON: reason,
    }


def extract_old(old_block):
    """
    从旧区块里安全取出需要的字段（旧区块可能是 None 或结构异常）。

    :return: (旧专辑列表, 旧状态, 上一次成功时间)
    """
    if not isinstance(old_block, dict):
        return [], None, ""
    albums = old_block.get(FIELD_ALBUMS)
    if not isinstance(albums, list):
        albums = []
    return albums, old_block.get(FIELD_STATUS), old_block.get(FIELD_LAST_OK, "") or ""


def load_snapshot(path: Path = SNAPSHOT_FILE) -> dict:
    """
    载入文件A（快照）。文件不存在 → 首次运行，返回空 dict。
    文件损坏时备份原文件后按首次运行处理，避免覆盖用户数据。
    """
    if not path.exists():
        cprint(f"[提示] 未找到快照文件，本次按首次运行处理：{path.name}")
        return {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        backup = path.with_name(f"{path.stem}.损坏备份-{datetime.now():%Y%m%d%H%M%S}{path.suffix}")
        cprint(f"[警告] 快照文件读取失败：{error}")
        try:
            os.replace(path, backup)
            cprint(f"[警告] 已把损坏的快照备份为：{backup.name}，本次按首次运行处理")
        except OSError as backup_error:
            cprint(f"[警告] 备份损坏快照失败：{backup_error}，本次按首次运行处理")
        return {}

    if not isinstance(data, dict):
        cprint("[警告] 快照内容不是「歌手ID → 区块」的字典结构，本次按首次运行处理")
        return {}
    return data


def save_snapshot(snapshot: dict, path: Path = SNAPSHOT_FILE) -> None:
    """原子写入文件A：先写临时文件再替换，避免中断导致快照损坏。"""
    tmp_path = path.with_name(f"{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


# ============================================================ 文件B：写日志

def write_log(records) -> None:
    """把若干条已拼好的日志行追加写入文件B（UTF-8）。"""
    if not records:
        return
    with LOG_FILE.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(record + "\n")


# ============================================================ 网络请求

def request_page(session, artist_id, offset, limit) -> dict:
    """
    请求单页专辑数据，返回 API 的 JSON dict。

    * 网络类错误（超时 / 连接错误 / 5xx / 返回内容不是 JSON 对象）：
      重试 MAX_ATTEMPTS 次（间隔 2~3 秒随机），仍失败抛 FetchFailed。
    * code != 200 或 artist 为 null：立即抛 ArtistInvalid，绝不重试。
    """
    url = API_URL.format(artist_id=artist_id)
    params = {"offset": offset, "limit": limit}
    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            # 5xx 视为网络类问题（可重试），其余交给 raise_for_status
            if response.status_code >= 500:
                raise requests.HTTPError(f"HTTP {response.status_code}")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("返回内容不是 JSON 对象")
        except (requests.RequestException, ValueError) as error:
            last_error = f"{type(error).__name__}: {error}"
            if attempt < MAX_ATTEMPTS:
                delay = random.uniform(*RETRY_DELAY_RANGE)
                cprint(f"      请求失败（{last_error}），{delay:.1f} 秒后重试"
                       f"（第 {attempt}/{MAX_ATTEMPTS - 1} 次重试）...")
                time.sleep(delay)
                continue
            raise FetchFailed(
                f"网络请求失败（共尝试 {MAX_ATTEMPTS} 次）：{last_error}"
            ) from error

        # 走到这里说明 HTTP + JSON 都正常，开始判定业务状态
        code = data.get("code")
        if code != 200 or not data.get("artist"):
            raise ArtistInvalid(f"歌手ID无效（API 返回 code={code}）")
        return data

    # 理论上不可达，兜底
    raise FetchFailed(f"网络请求失败：{last_error}")


def fetch_all_albums(session, artist_id, limit) -> list:
    """
    翻页抓取某歌手的全部专辑。

    翻页停止条件：
        * 本页 hotAlbums 长度为 0 → 到末尾，本次成功
        * 本页长度 < limit        → 到末尾，本次成功
        * 本页长度 == limit       → 继续下一页（页间随机延时 1.5~3.5 秒）

    :return: [{'专辑ID': int, '专辑名': str, '发行日期': 'YYYY-MM-DD'}, ...]
    """
    albums = []
    offset = 0
    page_no = 0

    while True:
        page_no += 1
        if page_no > MAX_PAGES:
            raise FetchFailed(f"翻页超过上限 {MAX_PAGES} 页，疑似异常，已放弃本次抓取")

        data = request_page(session, artist_id, offset, limit)
        raw_list = data.get("hotAlbums") or []
        if not isinstance(raw_list, list):
            raise FetchFailed("hotAlbums 字段格式异常（不是数组）")

        for item in raw_list:
            if not isinstance(item, dict):
                continue
            try:
                album_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue  # 没有有效专辑ID的脏数据直接跳过
            albums.append({
                "专辑ID": album_id,
                "专辑名": (item.get("name") or "").strip(),
                "发行日期": ms_to_date(item.get("publishTime")),
            })

        cprint(f"      第 {page_no} 页 offset={offset} 取回 {len(raw_list)} 张（累计 {len(albums)} 张）")

        # 正常到达末尾
        if len(raw_list) < limit:
            break

        offset += limit
        delay = random.uniform(*PAGE_DELAY_RANGE)
        cprint(f"      本页已满，{delay:.1f} 秒后翻下一页 ...")
        time.sleep(delay)

    return albums


# ============================================================ 单个歌手的处理

def success_log_records(name, artist_id, attempt_time, added, removed, old_status):
    """
    生成成功场景下的日志行：新增、下架、失败恢复。

    * [新增] 本次列表里、专辑ID 不在旧列表中的专辑
    * [下架] 旧列表里、专辑ID 不在本次列表中的专辑
    * [恢复] 上一次状态为 failed 而本次成功时，追加一条
    """
    records = []
    for album in added:
        records.append(
            f"[{attempt_time}] [新增] {clean_field(name)} | {artist_id} | "
            f"{clean_field(album['专辑名'])} | {album['发行日期']} | {album['专辑ID']}"
        )
    for album in removed:
        records.append(
            f"[{attempt_time}] [下架] {clean_field(name)} | {artist_id} | "
            f"{clean_field(album.get('专辑名', ''))} | {album.get('发行日期', '未知')} | "
            f"{album.get('专辑ID', '')}"
        )
    if old_status == STATUS_FAILED:
        records.append(
            f"[{attempt_time}] [恢复] {clean_field(name)} | {artist_id} | "
            f"本次新增{len(added)}张 | 状态已恢复ok"
        )
    return records


def failed_log_record(name, artist_id, attempt_time, reason, old_count) -> str:
    """生成失败/无效场景的日志行（只记旧专辑数量，不逐张列名）。"""
    return (
        f"[{attempt_time}] [失败] {clean_field(name)} | {artist_id} | "
        f"原因={log_reason(reason)} | 旧专辑数={old_count} | 已保留旧快照"
    )


def process_artist(session, name, artist_id, limit, old_block):
    """
    处理单个歌手：抓取 → 对比 → 写日志 → 更新内存区块。

    :param old_block: 文件A 中该歌手的旧区块，首次运行时为 None
    :return: (新区块, 结果摘要dict)
    """
    attempt_time = now_str()
    old_albums, old_status, old_last_ok = extract_old(old_block)
    old_ids = {str(a.get("专辑ID")) for a in old_albums if isinstance(a, dict)}

    # ---------- 抓取 ----------
    try:
        albums = fetch_all_albums(session, artist_id, limit)
    except ArtistInvalid as error:
        # 歌手ID无效：不重试，旧专辑列表原样保留
        reason = str(error)
        block = make_block(name, artist_id, STATUS_INVALID, old_albums,
                           old_last_ok, attempt_time, reason)
        write_log([failed_log_record(name, artist_id, attempt_time, reason, len(old_albums))])
        return block, {"状态": STATUS_INVALID, "原因": reason,
                       "新增": 0, "下架": 0, "总数": len(old_albums),
                       "旧专辑数": len(old_albums), "恢复": False}

    except FetchFailed as error:
        # 网络类失败重试耗尽：旧专辑列表原样保留
        reason = str(error)
        block = make_block(name, artist_id, STATUS_FAILED, old_albums,
                           old_last_ok, attempt_time, reason)
        write_log([failed_log_record(name, artist_id, attempt_time, reason, len(old_albums))])
        return block, {"状态": STATUS_FAILED, "原因": reason,
                       "新增": 0, "下架": 0, "总数": len(old_albums),
                       "旧专辑数": len(old_albums), "恢复": False}

    # ---------- 本次成功：按专辑ID对比旧列表 ----------
    new_ids = {str(a["专辑ID"]) for a in albums}
    added = [a for a in albums if str(a["专辑ID"]) not in old_ids]
    removed = [a for a in old_albums
               if not (isinstance(a, dict) and str(a.get("专辑ID")) in new_ids)]

    write_log(success_log_records(name, artist_id, attempt_time, added, removed, old_status))

    # 本次完整列表覆盖旧列表；状态改 ok、清除失败原因、更新成功时间
    block = make_block(name, artist_id, STATUS_OK, albums,
                       attempt_time, attempt_time, "")
    return block, {"状态": STATUS_OK, "原因": "",
                   "新增": len(added), "下架": len(removed), "总数": len(albums),
                   "旧专辑数": len(old_albums), "恢复": old_status == STATUS_FAILED}


# ============================================================ 主流程

def main() -> int:
    cprint("=" * 62)
    cprint(f"网易云音乐歌手专辑监控    {now_str()}")
    cprint("=" * 62)

    # 1. 读文件C
    artists, limit = parse_config()
    if not artists:
        cprint("[结束] 没有可处理的歌手，请检查「歌手配置.txt」")
        return 1
    cprint(f"[配置] 共 {len(artists)} 位歌手，每页 limit={limit}")

    # 2. 读文件A到内存
    snapshot = load_snapshot()
    cprint(f"[快照] 已载入 {len(snapshot)} 位歌手的历史记录")

    session = requests.Session()
    stats = {"成功": 0, "失败": 0, "无效": 0, "新增": 0, "下架": 0, "恢复": 0}
    interrupted = False
    total = len(artists)

    # 3. 逐个歌手处理
    try:
        for index, (name, artist_id) in enumerate(artists, 1):
            cprint(f"\n[{index}/{total}] {name}（ID={artist_id}）处理中 ...")
            old_block = snapshot.get(artist_id)

            try:
                block, result = process_artist(session, name, artist_id, limit, old_block)
            except Exception as error:  # 兜底：单个歌手的意外异常不影响其他歌手
                old_albums, _old_status, old_last_ok = extract_old(old_block)
                reason = f"{type(error).__name__}: {error}"
                attempt_time = now_str()
                block = make_block(name, artist_id, STATUS_FAILED, old_albums,
                                   old_last_ok, attempt_time, reason)
                write_log([failed_log_record(name, artist_id, attempt_time,
                                             reason, len(old_albums))])
                result = {"状态": STATUS_FAILED, "原因": reason, "新增": 0, "下架": 0,
                          "总数": len(old_albums), "旧专辑数": len(old_albums),
                          "恢复": False}

            snapshot[artist_id] = block

            # 控制台进度与结果
            if result["状态"] == STATUS_OK:
                stats["成功"] += 1
                stats["新增"] += result["新增"]
                stats["下架"] += result["下架"]
                if result["恢复"]:
                    stats["恢复"] += 1
                extra = "（本次为失败恢复）" if result["恢复"] else ""
                cprint(f"    [成功] 共 {result['总数']} 张 | 新增 {result['新增']} 张 | "
                       f"下架 {result['下架']} 张{extra}")
            elif result["状态"] == STATUS_INVALID:
                stats["无效"] += 1
                cprint(f"    [歌手ID无效] 原因={result['原因']} | "
                       f"旧快照已保留（{result['旧专辑数']} 张）")
            else:
                stats["失败"] += 1
                cprint(f"    [失败] 原因={result['原因']} | "
                       f"旧快照已保留（{result['旧专辑数']} 张）")

            # 歌手之间延时（最后一位不再空等）
            if index < total:
                delay = random.uniform(*ARTIST_DELAY_RANGE)
                cprint(f"    等待 {delay:.1f} 秒后处理下一位歌手 ...")
                time.sleep(delay)
    except KeyboardInterrupt:
        interrupted = True
        cprint("\n[中断] 收到 Ctrl+C，正在保存已完成的快照 ...")

    # 4. 全部处理完毕后，一次性写回文件A
    try:
        save_snapshot(snapshot)
        cprint(f"\n[写回] 快照已保存：{SNAPSHOT_FILE.name}（{len(snapshot)} 位歌手）")
    except OSError as error:
        cprint(f"\n[错误] 快照写回失败：{error}")
        return 2

    cprint(f"[日志] 本次记录已追加到：{LOG_FILE.name}")
    cprint("-" * 62)
    cprint(f"本次结果：成功 {stats['成功']} 位 | 失败 {stats['失败']} 位 | "
           f"歌手ID无效 {stats['无效']} 位")
    cprint(f"          新增 {stats['新增']} 张 | 下架 {stats['下架']} 张 | "
           f"失败恢复 {stats['恢复']} 位")
    cprint(f"结束时间：{now_str()}")
    cprint("=" * 62)
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
