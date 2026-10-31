#!/usr/bin/env python3
"""Sync mihomo three-source status + ChatGPT speed test into a Feishu Bitable.

Three tables are maintained in one Bitable app (one sync batch per run):

  1. 订阅源状态   - one fixed row per subscription source (飞兔/星辰/CoffeeCloud)
  2. 可测速节点   - every parsed node with its ChatGPT-trace delay
  3. GPT可用地区组 - Nodes whose ChatGPT trace test succeeded AND whose measured
     egress (ipinfo through that node) is Singapore/US

Security contract
-----------------
* Subscription URLs, FEISHU_APP_SECRET and tenant_access_token are never
  printed, logged or persisted by this script.
* Log lines carry only HTTP status codes, row counts and error
  classifications (http_403 / token_error / timeout / network_error /
  parse_error / api_error).
* The state file keeps table_id values only (no credentials).
* stdout carries the operator summary only; progress goes to the log file.

Usage
-----
  python sync-feishu-bitable.py                     # full sync
  python sync-feishu-bitable.py --dry-run           # collect + test, no write
  python sync-feishu-bitable.py --ensure-tables     # only create tables
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import convert_sub  # noqa: E402  (same directory, project-local converter)

try:
    import yaml  # noqa: F401  (convert_sub already requires it)
except ImportError:  # pragma: no cover - convert_sub fails closed on its own
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_APP_TOKEN = "URo9bAsd5azYe5swAmtclIbEnNh"
DEFAULT_ENV_FILE = r"C:\Users\Administrator\.hermes\.env"
DEFAULT_STATE_FILE = r"C:\mihomo\feishu-bitable-tables.json"
DEFAULT_LOG_FILE = r"C:\mihomo\logs\feishu-bitable-sync.log"
DEFAULT_SOURCES_ENV = str(SCRIPT_DIR.parent / "config" / "subscription.env")
DEFAULT_CORE = r"C:\mihomo\mihomo.exe"
DEFAULT_TARGET = "https://chatgpt.com/cdn-cgi/trace"
DEFAULT_LIVE_CONTROLLER = "http://127.0.0.1:9090"
DEFAULT_LIVE_ENV = r"C:\mihomo\subscription.env"
DEFAULT_FETCH_PROXY = "http://127.0.0.1:7890"
DEFAULT_LOCK_FILE = r"C:\mihomo\feishu-bitable-sync.lock"
PAGE_PROBE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PLACEHOLDER_TABLE_ID = "tbldzw2ismrKCEAC"
PLACEHOLDER_TABLE_NAME = "数据表"

SOURCE_NAMES = {1: "飞兔", 2: "星辰", 3: "CoffeeCloud"}

# Feishu Bitable field types
FT_TEXT = 1
FT_NUMBER = 2
FT_SINGLE_SELECT = 3
FT_DATE_TIME = 5
FT_CHECKBOX = 7

DATE_PROPERTY = {"date_formatter": "yyyy/MM/dd HH:mm", "auto_fill": False}
NUMBER_PROPERTY = {"formatter": "0"}

TABLE_SPECS: list[dict[str, Any]] = [
    {
        "key": "sources",
        "name": "订阅源状态",
        "fields": [
            ("源名称", FT_TEXT, None),
            ("源编号", FT_NUMBER, NUMBER_PROPERTY),
            ("HTTP状态", FT_NUMBER, NUMBER_PROPERTY),
            ("拉取结果", FT_SINGLE_SELECT, {"options": [{"name": "成功"}, {"name": "失败"}, {"name": "缓存"}]}),
            ("节点数", FT_NUMBER, NUMBER_PROPERTY),
            ("存活数", FT_NUMBER, NUMBER_PROPERTY),
            ("流量剩余", FT_TEXT, None),
            ("到期时间", FT_TEXT, None),
            ("失败原因", FT_TEXT, None),
            ("页面网址", FT_TEXT, None),
            ("页面可达", FT_SINGLE_SELECT, {"options": [{"name": n} for n in ("可达", "不可达", "未配置")]}),
            ("页面HTTP状态", FT_NUMBER, NUMBER_PROPERTY),
            ("页面探测延迟ms", FT_NUMBER, NUMBER_PROPERTY),
            ("同步时间", FT_DATE_TIME, DATE_PROPERTY),
        ],
    },
    {
        "key": "nodes",
        "name": "可测速节点",
        "fields": [
            ("节点名", FT_TEXT, None),
            ("来源", FT_SINGLE_SELECT, {"options": [{"name": n} for n in ("飞兔", "星辰", "CoffeeCloud", "未知")]}),
            ("协议", FT_TEXT, None),
            # Free-form from ipinfo.io/json via that node: 中文 国家/地区/城市 or "未知".
            ("实际地区", FT_TEXT, None),
            ("延迟ms", FT_NUMBER, NUMBER_PROPERTY),
            ("存活", FT_CHECKBOX, None),
            ("测速目标", FT_TEXT, None),
            ("同步时间", FT_DATE_TIME, DATE_PROPERTY),
        ],
    },
    {
        "key": "gpt_regions",
        "name": "GPT可用地区组",
        "fields": [
            ("节点名", FT_TEXT, None),
            ("地区组", FT_SINGLE_SELECT, {"options": [{"name": "新加坡"}, {"name": "美国"}]}),
            # Full 中文 egress path (国家/地区/城市) from ipinfo; 地区组 stays coarse.
            ("实际地区", FT_TEXT, None),
            ("来源", FT_SINGLE_SELECT, {"options": [{"name": n} for n in ("飞兔", "星辰", "CoffeeCloud", "未知")]}),
            ("延迟ms", FT_NUMBER, NUMBER_PROPERTY),
            ("GPT可用", FT_CHECKBOX, None),
            ("代理组", FT_TEXT, None),
            ("同步时间", FT_DATE_TIME, DATE_PROPERTY),
        ],
    },
]

# Rate-limit / transient API codes returned by the Bitable endpoints.
RETRY_CODES = {1254290, 1254291, 1254607, 99991400, 1310213}
TOKEN_CODES = {99991661, 99991663, 99991664, 99991668}

SG_PATTERN = re.compile(r"新加坡|狮城|獅城|singapore|🇸🇬|\bSGP?\b", re.IGNORECASE)
TW_PATTERN = re.compile(r"台湾|台灣|taiwan|🇹🇼|\bTW\b", re.IGNORECASE)
US_PATTERN = re.compile(
    r"美国|美國|united states|\bUSA?\b|🇺🇸|los angeles|san jose|seattle|dallas|chicago|"
    r"new york|ashburn|silicon valley|phoenix|miami|atlanta|las vegas|portland|denver|"
    r"virginia|ohio|california|圣何塞|洛杉矶|西雅图|达拉斯|芝加哥|纽约|硅谷|凤凰城",
    re.IGNORECASE,
)
HK_PATTERN = re.compile(
    r"香港|hong\s*kong|🇭🇰|\bHK\b|\bHKG\b",
    re.IGNORECASE,
)
JP_PATTERN = re.compile(
    r"日本|japan|tokyo|osaka|🇯🇵|\bJP\b|\bJPN\b",
    re.IGNORECASE,
)
KR_PATTERN = re.compile(
    r"韩国|韓國|korea|seoul|🇰🇷|\bKR\b|\bKOR\b",
    re.IGNORECASE,
)
UK_PATTERN = re.compile(
    r"英国|英國|united\s*kingdom|\bUK\b|\bGBR\b|london|🇬🇧",
    re.IGNORECASE,
)
DE_PATTERN = re.compile(
    r"德国|德國|germany|frankfurt|berlin|🇩🇪|\bDE\b|\bDEU\b",
    re.IGNORECASE,
)

# --- 实际地区 中文标注 -------------------------------------------------------
# ISO2 (upper) -> 中文. Unknown codes keep the raw spelling.
COUNTRY_ZH: dict[str, str] = {
    "US": "美国", "TW": "台湾", "HK": "香港", "MO": "澳门", "CN": "中国",
    "JP": "日本", "KR": "韩国", "SG": "新加坡", "MY": "马来西亚",
    "TH": "泰国", "VN": "越南", "PH": "菲律宾", "ID": "印度尼西亚",
    "IN": "印度", "PK": "巴基斯坦", "BD": "孟加拉国", "KH": "柬埔寨",
    "LA": "老挝", "MM": "缅甸", "NP": "尼泊尔", "LK": "斯里兰卡",
    "MN": "蒙古", "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦",
    "DE": "德国", "GB": "英国", "UK": "英国", "IE": "爱尔兰",
    "FR": "法国", "NL": "荷兰", "BE": "比利时", "LU": "卢森堡",
    "CH": "瑞士", "AT": "奥地利", "IT": "意大利", "ES": "西班牙",
    "PT": "葡萄牙", "SE": "瑞典", "NO": "挪威", "DK": "丹麦",
    "FI": "芬兰", "IS": "冰岛", "PL": "波兰", "CZ": "捷克",
    "SK": "斯洛伐克", "HU": "匈牙利", "RO": "罗马尼亚", "BG": "保加利亚",
    "GR": "希腊", "HR": "克罗地亚", "SI": "斯洛文尼亚", "RS": "塞尔维亚",
    "LT": "立陶宛", "LV": "拉脱维亚", "EE": "爱沙尼亚", "UA": "乌克兰",
    "BY": "白俄罗斯", "MD": "摩尔多瓦", "CY": "塞浦路斯", "MT": "马耳他",
    "RU": "俄罗斯", "TR": "土耳其", "IL": "以色列", "AE": "阿联酋",
    "SA": "沙特阿拉伯", "QA": "卡塔尔", "KW": "科威特", "EG": "埃及",
    "ZA": "南非", "NG": "尼日利亚", "KE": "肯尼亚",
    "CA": "加拿大", "MX": "墨西哥", "BR": "巴西", "AR": "阿根廷",
    "CL": "智利", "CO": "哥伦比亚", "PE": "秘鲁", "UY": "乌拉圭",
    "EC": "厄瓜多尔", "VE": "委内瑞拉", "PA": "巴拿马", "CR": "哥斯达黎加",
    "AU": "澳大利亚", "NZ": "新西兰", "GU": "关岛", "PR": "波多黎各",
    "FJ": "斐济", "AM": "亚美尼亚", "GE": "格鲁吉亚", "AZ": "阿塞拜疆",
}

# Full English country names (lower-case) -> 中文, for payloads without an ISO code.
COUNTRY_NAME_ZH: dict[str, str] = {
    "united states": "美国", "united states of america": "美国", "usa": "美国",
    "taiwan": "台湾", "hong kong": "香港", "macau": "澳门", "macao": "澳门",
    "china": "中国", "japan": "日本", "south korea": "韩国",
    "korea, republic of": "韩国", "republic of korea": "韩国",
    "singapore": "新加坡", "malaysia": "马来西亚", "thailand": "泰国",
    "vietnam": "越南", "viet nam": "越南", "philippines": "菲律宾",
    "indonesia": "印度尼西亚", "india": "印度", "pakistan": "巴基斯坦",
    "germany": "德国", "united kingdom": "英国", "great britain": "英国",
    "ireland": "爱尔兰", "france": "法国", "netherlands": "荷兰",
    "the netherlands": "荷兰", "belgium": "比利时", "luxembourg": "卢森堡",
    "switzerland": "瑞士", "austria": "奥地利", "italy": "意大利",
    "spain": "西班牙", "portugal": "葡萄牙", "sweden": "瑞典",
    "norway": "挪威", "denmark": "丹麦", "finland": "芬兰", "iceland": "冰岛",
    "poland": "波兰", "czechia": "捷克", "czech republic": "捷克",
    "hungary": "匈牙利", "romania": "罗马尼亚", "greece": "希腊",
    "russia": "俄罗斯", "russian federation": "俄罗斯", "turkey": "土耳其",
    "israel": "以色列", "united arab emirates": "阿联酋",
    "saudi arabia": "沙特阿拉伯", "qatar": "卡塔尔", "egypt": "埃及",
    "south africa": "南非", "canada": "加拿大", "mexico": "墨西哥",
    "brazil": "巴西", "argentina": "阿根廷", "chile": "智利",
    "colombia": "哥伦比亚", "peru": "秘鲁", "australia": "澳大利亚",
    "new zealand": "新西兰",
}

# Region / city spellings (lower-case, single-spaced) -> 中文.
# Missing entries keep the original English/local spelling.
REGION_CITY_ZH: dict[str, str] = {
    # --- US states / cities ---
    "california": "加利福尼亚", "ca": "加利福尼亚",
    "new york": "纽约", "new york state": "纽约", "ny": "纽约",
    "texas": "德克萨斯", "tx": "德克萨斯",
    "washington": "华盛顿", "wa": "华盛顿",
    "illinois": "伊利诺伊", "il": "伊利诺伊",
    "florida": "佛罗里达", "fl": "佛罗里达",
    "georgia": "佐治亚", "ga": "佐治亚",
    "virginia": "弗吉尼亚", "va": "弗吉尼亚",
    "ohio": "俄亥俄", "oh": "俄亥俄",
    "oregon": "俄勒冈", "or": "俄勒冈",
    "arizona": "亚利桑那", "az": "亚利桑那",
    "colorado": "科罗拉多", "co": "科罗拉多",
    "nevada": "内华达", "nv": "内华达",
    "new jersey": "新泽西", "nj": "新泽西",
    "massachusetts": "马萨诸塞", "ma": "马萨诸塞",
    "pennsylvania": "宾夕法尼亚", "pa": "宾夕法尼亚",
    "north carolina": "北卡罗来纳", "south carolina": "南卡罗来纳",
    "michigan": "密歇根", "minnesota": "明尼苏达", "missouri": "密苏里",
    "maryland": "马里兰", "utah": "犹他", "hawaii": "夏威夷",
    "alaska": "阿拉斯加",
    "los angeles": "洛杉矶", "san jose": "圣何塞", "seattle": "西雅图",
    "dallas": "达拉斯", "chicago": "芝加哥", "new york city": "纽约",
    "ashburn": "阿什本", "silicon valley": "硅谷", "phoenix": "凤凰城",
    "miami": "迈阿密", "atlanta": "亚特兰大", "las vegas": "拉斯维加斯",
    "portland": "波特兰", "denver": "丹佛", "san francisco": "旧金山",
    "fremont": "弗里蒙特", "sunnyvale": "桑尼维尔",
    "santa clara": "圣克拉拉", "reston": "雷斯顿", "sterling": "斯特灵",
    "columbus": "哥伦布", "honolulu": "檀香山", "anchorage": "安克雷奇",
    # --- Taiwan ---
    "changhua": "彰化", "changhua county": "彰化",
    "yuanlin": "员林", "yuanlin city": "员林",
    "taipei": "台北", "taipei city": "台北", "new taipei": "新北",
    "taichung": "台中", "taichung city": "台中", "tainan": "台南",
    "kaohsiung": "高雄", "kaohsiung city": "高雄", "taoyuan": "桃园",
    "hsinchu": "新竹", "keelung": "基隆", "chiayi": "嘉义",
    "yilan": "宜兰", "hualien": "花莲", "taitung": "台东",
    "miaoli": "苗栗", "nantou": "南投", "yunlin": "云林",
    "pingtung": "屏东", "penghu": "澎湖", "kinmen": "金门",
    # --- Hong Kong / Macau / Singapore ---
    "hong kong": "香港", "hongkong": "香港", "kowloon": "九龙",
    "new territories": "新界", "macau": "澳门", "macao": "澳门",
    "singapore": "新加坡", "central singapore": "新加坡",
    # --- Japan / Korea ---
    "tokyo": "东京", "osaka": "大阪", "kyoto": "京都", "hokkaido": "北海道",
    "aichi": "爱知", "fukuoka": "福冈", "kanagawa": "神奈川",
    "saitama": "埼玉", "chiba": "千叶", "hyogo": "兵库",
    "hiroshima": "广岛", "okinawa": "冲绳",
    "seoul": "首尔", "busan": "釜山", "incheon": "仁川",
    "gyeonggi": "京畿", "gyeonggi-do": "京畿",
    # --- Germany ---
    "hessen": "黑森", "hesse": "黑森", "bavaria": "巴伐利亚",
    "bayern": "巴伐利亚", "berlin": "柏林", "hamburg": "汉堡",
    "munich": "慕尼黑", "münchen": "慕尼黑", "saxony": "萨克森",
    "north rhine-westphalia": "北莱茵-威斯特法伦",
    "baden-württemberg": "巴登-符腾堡", "lower saxony": "下萨克森",
    "frankfurt": "法兰克福", "frankfurt am main": "法兰克福",
    # --- Other common exits ---
    "london": "伦敦", "manchester": "曼彻斯特", "amsterdam": "阿姆斯特丹",
    "paris": "巴黎", "sydney": "悉尼", "melbourne": "墨尔本",
    "mumbai": "孟买", "bangkok": "曼谷", "kuala lumpur": "吉隆坡",
    "manila": "马尼拉", "hanoi": "河内", "ho chi minh city": "胡志明市",
    "jakarta": "雅加达", "milan": "米兰", "madrid": "马德里",
    "sao paulo": "圣保罗", "mexico city": "墨西哥城", "moscow": "莫斯科",
    "istanbul": "伊斯坦布尔", "dubai": "迪拜", "riyadh": "利雅得",
    "dublin": "都柏林", "stockholm": "斯德哥尔摩", "zurich": "苏黎世",
    "vienna": "维也纳", "auckland": "奥克兰",
    # --- China mainland ---
    "hunan": "湖南", "guangdong": "广东", "zhejiang": "浙江",
    "jiangsu": "江苏", "fujian": "福建", "sichuan": "四川",
    "shanghai": "上海", "beijing": "北京", "shenzhen": "深圳",
    "guangzhou": "广州", "zhuzhou": "株洲", "changsha": "长沙",
}

# ---------------------------------------------------------------------------
# Logging (never writes secrets or URLs)
# ---------------------------------------------------------------------------

class Logger:
    MAX_BYTES = 5 * 1024 * 1024

    def __init__(self, path: str, verbose: bool = False) -> None:
        self.path = Path(path)
        self.verbose = verbose
        self._ready = False

    def _open(self) -> None:
        if self._ready:
            return
        self._ready = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.MAX_BYTES:
                backup = self.path.with_suffix(self.path.suffix + ".1")
                try:
                    if backup.exists():
                        backup.unlink()
                    self.path.rename(backup)
                except OSError:
                    pass
        except OSError:
            pass

    def _write(self, level: str, message: str) -> None:
        line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {level} {message}"
        self._open()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        if self.verbose:
            print(line, file=sys.stderr)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)


# ---------------------------------------------------------------------------
# Env / secret loading
# ---------------------------------------------------------------------------

def read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except (OSError, UnicodeError):
        pass
    return values


class SyncError(Exception):
    """Failure carrying a classification string that is safe to log."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------
#
# Windows: a named kernel mutex (Local\mihomo-feishu-bitable-sync) provides the
# real mutual exclusion, so a crashed holder is taken over via WAIT_ABANDONED
# with no stale-unlink race. The lock FILE is metadata only (pid/ts/token) and
# is written after the mutex is held, so stale metadata is simply overwritten.
#
# POSIX: the lock file itself is the mutex (O_CREAT|O_EXCL) plus stale-pid
# takeover, as before.

SYNC_MUTEX_NAME = r"Local\mihomo-feishu-bitable-sync"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102

_KERNEL32: Any = None


def _kernel32() -> Any:
    """Load kernel32 once with explicit signatures (no pointer truncation)."""
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k32.ReleaseMutex.restype = wintypes.BOOL
        _KERNEL32 = k32
    return _KERNEL32


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        k32 = _kernel32()
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            k32.CloseHandle(handle)
            return True
        # OpenProcess can fail for a live process we may not fully query
        # (e.g. another user / elevated). ACCESS_DENIED means "still alive".
        err = ctypes.get_last_error()
        return err == ERROR_ACCESS_DENIED
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock_meta(path: str) -> tuple[int, str, str]:
    """Return (pid, ts, token) from the lock file; zeros/empty on failure."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, "", ""
    pid = 0
    ts = ""
    token = ""
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("pid="):
            try:
                pid = int(line[4:].strip())
            except ValueError:
                pid = 0
        elif line.startswith("ts="):
            ts = line[3:].strip()
        elif line.startswith("token="):
            token = line[6:].strip()
    return pid, ts, token


class SyncLock:
    """Single-instance lock with explicit, token-checked ownership.

    ``release()`` is a no-op unless THIS object actually acquired the lock, and
    it never unlinks a lock file that carries another process's token.
    """

    def __init__(self, path: str, log: "Logger") -> None:
        self.path = str(path)
        self.log = log
        self.pid = os.getpid()
        self.token = secrets.token_hex(8)
        self.fd: int | None = None      # POSIX: lock file fd
        self.mutex: int | None = None   # Windows: named mutex handle
        self.held = False

    # -- helpers ---------------------------------------------------------
    def _payload(self) -> bytes:
        return (
            f"pid={self.pid}\n"
            f"ts={dt.datetime.now().isoformat(timespec='seconds')}\n"
            f"token={self.token}\n"
        ).encode("utf-8")

    def _ensure_dir(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SyncError("lock_busy", "lock_dir_unavailable") from exc

    def _write_meta(self) -> None:
        try:
            with open(self.path, "wb") as handle:
                handle.write(self._payload())
        except OSError:
            # Metadata is advisory; the mutex/file still protects us.
            self.log.warn(f"lock metadata write failed path={self.path}")

    # -- acquire ---------------------------------------------------------
    def acquire(self) -> "SyncLock":
        self._ensure_dir()
        if sys.platform == "win32":
            self._acquire_mutex()
        else:
            self._acquire_file()
        self.held = True
        self.log.info(f"lock acquired path={self.path} pid={self.pid}")
        return self

    def _acquire_mutex(self) -> None:
        import ctypes

        k32 = _kernel32()
        handle = k32.CreateMutexW(None, True, SYNC_MUTEX_NAME)
        if not handle:
            err = ctypes.get_last_error()
            self.log.error(f"lock busy path={self.path} reason=mutex_create_failed err={err}")
            raise SyncError("lock_busy", f"mutex_create_failed err={err}")

        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            # Another instance owns (or owned) the mutex. bInitialOwner is
            # ignored for an existing object, so poll for ownership.
            rc = k32.WaitForSingleObject(handle, 0)
            if rc not in (WAIT_OBJECT_0, WAIT_ABANDONED):
                k32.CloseHandle(handle)
                reason = "mutex_timeout" if rc == WAIT_TIMEOUT else f"mutex_wait_failed rc={rc}"
                self.log.error(f"lock busy path={self.path} reason={reason}")
                raise SyncError("lock_busy", reason)
            if rc == WAIT_ABANDONED:
                self.log.warn(f"lock takeover path={self.path} reason=abandoned_mutex")

        self.mutex = int(handle)
        old_pid, old_ts, _old_token = _read_lock_meta(self.path)
        if old_pid and old_pid != self.pid:
            # Mutex already serializes writers, so stale metadata is safe to
            # overwrite here (no unlink/recreate race).
            self.log.warn(
                f"lock stale metadata overwritten path={self.path} "
                f"old_pid={old_pid} old_ts={old_ts or '?'}"
            )
        self._write_meta()

    def _acquire_file(self) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        for _attempt in range(2):
            try:
                fd = os.open(self.path, flags)
            except FileExistsError:
                holder_pid, holder_ts, _token = _read_lock_meta(self.path)
                if holder_pid and _pid_alive(holder_pid):
                    self.log.error(
                        f"lock busy path={self.path} holder_pid={holder_pid} "
                        f"holder_ts={holder_ts or '?'}"
                    )
                    raise SyncError("lock_busy", f"holder_pid={holder_pid}")
                self.log.warn(
                    f"lock stale takeover path={self.path} dead_pid={holder_pid or 0} "
                    f"holder_ts={holder_ts or '?'}"
                )
                try:
                    os.unlink(self.path)
                except OSError as exc:
                    raise SyncError("lock_busy", "stale_unlink_failed") from exc
                continue
            except OSError as exc:
                raise SyncError("lock_busy", f"lock_open_failed:{exc.errno}") from exc
            try:
                os.write(fd, self._payload())
            except OSError:
                with contextlib.suppress(OSError):
                    os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(self.path)
                raise SyncError("lock_busy", "lock_write_failed") from None
            self.fd = fd
            return
        raise SyncError("lock_busy", "reacquire_failed")

    # -- release ---------------------------------------------------------
    def release(self) -> None:
        if self.fd is None and self.mutex is None:
            # Never acquired, or already released: touch nothing on disk.
            self.log.info(f"lock release skipped (not owner) path={self.path}")
            self.held = False
            return

        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None

        # Unlink only while we still own the mutex, only if this object held the
        # lock, and only if the file still carries OUR token. A missing or
        # mismatched token means the file is not provably ours: leave it alone.
        try:
            if Path(self.path).exists():
                owner_pid, _ts, owner_token = _read_lock_meta(self.path)
                mine = self.held and bool(owner_token) and owner_token == self.token
                if mine:
                    os.unlink(self.path)
                    self.log.info(f"lock released path={self.path} pid={self.pid}")
                else:
                    self.log.warn(
                        f"lock release skipped (not owner) path={self.path} "
                        f"owner_pid={owner_pid or 0}"
                    )
        except OSError:
            self.log.warn(f"lock release failed path={self.path}")
        finally:
            self._release_mutex()
            self.held = False

    def _release_mutex(self) -> None:
        if self.mutex is None:
            return
        handle, self.mutex = self.mutex, None
        k32 = _kernel32()
        try:
            k32.ReleaseMutex(handle)
        finally:
            k32.CloseHandle(handle)


def acquire_sync_lock(path: str, log: "Logger") -> SyncLock:
    """Acquire the single-instance lock; raise SyncError(lock_busy) if held."""
    return SyncLock(path, log).acquire()


def release_sync_lock(lock: SyncLock | None, log: "Logger") -> None:
    """Release only if *lock* owns it; safe to call after a failed acquire."""
    if lock is None:
        log.info("lock release skipped (not owner)")
        return
    lock.release()



# ---------------------------------------------------------------------------
# Feishu client
# ---------------------------------------------------------------------------

class FeishuClient:
    BASE = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str, app_token: str, log: Logger) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.log = log
        self._token: str | None = None
        self._expires_at = 0.0

    # -- transport ---------------------------------------------------------

    def _raw(self, method: str, url: str, body: dict | None, headers: dict[str, str],
             timeout: int = 30) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # The HTTP body may echo request details; only the status is kept.
            raise SyncError(f"http_{exc.code}") from None
        except (socket.timeout, TimeoutError):
            raise SyncError("timeout") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise SyncError("timeout") from None
            raise SyncError("network_error") from None
        except Exception as exc:  # pragma: no cover - defensive
            raise SyncError(type(exc).__name__.lower()) from None
        try:
            return json.loads(raw or "{}")
        except ValueError:
            raise SyncError("bad_response") from None

    def token(self) -> str:
        if self._token and time.time() < self._expires_at - 120:
            return self._token
        payload = self._raw(
            "POST",
            f"{self.BASE}/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret},
            {"Content-Type": "application/json; charset=utf-8"},
            timeout=20,
        )
        code = payload.get("code")
        if code != 0 or not payload.get("tenant_access_token"):
            raise SyncError("token_error", f"code={code}")
        self._token = payload["tenant_access_token"]
        self._expires_at = time.time() + int(payload.get("expire") or 7200)
        self.log.info("token acquired")
        return self._token

    def call(self, method: str, path: str, body: dict | None = None,
             params: dict | None = None) -> dict:
        url = self.BASE + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        delay = 1.0
        for attempt in range(4):
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {self.token()}",
            }
            payload = self._raw(method, url, body, headers)
            code = payload.get("code", 0)
            if code == 0:
                return payload.get("data") or {}
            if code in TOKEN_CODES:
                self._token = None
                if attempt < 3:
                    time.sleep(0.5)
                    continue
                raise SyncError("token_error", f"code={code}")
            if code in RETRY_CODES:
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise SyncError("rate_limited", f"code={code}")
            raise SyncError("api_error", f"code={code}")
        raise SyncError("api_error", "retry_exhausted")

    # -- tables ------------------------------------------------------------

    def list_tables(self) -> list[dict]:
        tables: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.call("GET", f"/bitable/v1/apps/{self.app_token}/tables", params=params)
            tables.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                return tables

    def create_table(self, name: str, fields: list[tuple]) -> str:
        body = {
            "table": {
                "name": name,
                "default_view_name": "表格",
                "fields": [
                    {"field_name": f[0], "type": f[1], **({"property": f[2]} if f[2] else {})}
                    for f in fields
                ],
            }
        }
        data = self.call("POST", f"/bitable/v1/apps/{self.app_token}/tables", body=body)
        table_id = data.get("table_id") or ""
        if not table_id:
            raise SyncError("api_error", "create_table_no_id")
        return table_id

    def delete_table(self, table_id: str) -> None:
        self.call("DELETE", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}")

    def list_fields(self, table_id: str) -> list[dict]:
        fields: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self.call(
                "GET", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields", params=params
            )
            fields.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                return fields

    def create_field(self, table_id: str, name: str, ftype: int, prop: dict | None) -> None:
        body: dict[str, Any] = {"field_name": name, "type": ftype}
        if prop:
            body["property"] = prop
        self.call("POST", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields", body=body)

    def update_field(self, table_id: str, field_id: str, name: str, ftype: int, prop: dict) -> None:
        body = {"field_name": name, "type": ftype, "property": prop}
        self.call(
            "PUT", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields/{field_id}", body=body
        )

    def delete_field(self, table_id: str, field_id: str) -> None:
        self.call(
            "DELETE", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields/{field_id}"
        )

    # -- records -----------------------------------------------------------

    def list_records(self, table_id: str) -> list[dict]:
        records: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self.call(
                "GET", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records", params=params
            )
            records.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                return records

    def batch_delete(self, table_id: str, record_ids: list[str]) -> int:
        deleted = 0
        for start in range(0, len(record_ids), 200):
            chunk = record_ids[start:start + 200]
            self.call(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_delete",
                body={"records": chunk},
            )
            deleted += len(chunk)
            time.sleep(0.3)
        return deleted

    def batch_create(self, table_id: str, records: list[dict]) -> int:
        created = 0
        for start in range(0, len(records), 100):
            chunk = records[start:start + 100]
            self.call(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_create",
                body={"records": chunk},
            )
            created += len(chunk)
            time.sleep(0.3)
        return created


# ---------------------------------------------------------------------------
# Table provisioning
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, tables: dict[str, dict[str, str]]) -> None:
    payload = {"tables": tables, "updated": dt.datetime.now().isoformat(timespec="seconds")}
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        pass


def ensure_fields(client: FeishuClient, table_id: str, fields: list[tuple], log: Logger) -> None:
    existing = {f.get("field_name"): f for f in client.list_fields(table_id)}
    for name, ftype, prop in fields:
        current = existing.get(name)
        if current is not None and int(current.get("type") or 0) != int(ftype):
            # e.g. 实际地区 SINGLE_SELECT → TEXT for free-form ipinfo strings.
            try:
                client.delete_field(table_id, current.get("field_id", ""))
                log.info(f"field deleted for retype name={name} old_type={current.get('type')}")
                current = None
                existing.pop(name, None)
            except SyncError as exc:
                log.warn(f"field retype delete failed name={name} class={exc.kind}")
        if current is None:
            try:
                client.create_field(table_id, name, ftype, prop)
                log.info(f"field created name={name} type={ftype}")
            except SyncError as exc:
                log.warn(f"field create failed name={name} class={exc.kind}")
            continue
        if ftype != FT_SINGLE_SELECT or not prop:
            continue
        wanted = [o["name"] for o in prop.get("options", [])]
        current_prop = current.get("property") or {}
        have = [o.get("name") for o in (current_prop.get("options") or [])]
        missing = [name_ for name_ in wanted if name_ not in have]
        if not missing:
            continue
        merged = {"options": [{"name": n} for n in have + missing]}
        try:
            client.update_field(table_id, current.get("field_id", ""), name, ftype, merged)
            log.info(f"field options extended name={name} added={len(missing)}")
        except SyncError as exc:
            log.warn(f"field update failed name={name} class={exc.kind}")


def ensure_tables(client: FeishuClient, state_file: str, keep_placeholder: bool,
                  log: Logger) -> dict[str, dict[str, str]]:
    remote = {t.get("name"): t.get("table_id") for t in client.list_tables()}
    log.info(f"remote tables={len(remote)}")
    tables: dict[str, dict[str, str]] = {}
    for spec in TABLE_SPECS:
        table_id = remote.get(spec["name"])
        if table_id:
            log.info(f"table exists name={spec['name']}")
        else:
            table_id = client.create_table(spec["name"], spec["fields"])
            log.info(f"table created name={spec['name']}")
            time.sleep(0.4)
        ensure_fields(client, table_id, spec["fields"], log)
        tables[spec["key"]] = {"table_id": table_id, "name": spec["name"]}

    real_ids = {entry["table_id"] for entry in tables.values()}
    placeholder = remote.get(PLACEHOLDER_TABLE_NAME) or PLACEHOLDER_TABLE_ID
    if not keep_placeholder and placeholder and placeholder not in real_ids and len(tables) == 3:
        try:
            client.delete_table(placeholder)
            log.info("placeholder table removed")
        except SyncError as exc:
            log.warn(f"placeholder delete failed class={exc.kind}")

    save_state(state_file, tables)
    return tables


# ---------------------------------------------------------------------------
# Subscription collection (per source, then tagged)
# ---------------------------------------------------------------------------

class SourceResult:
    def __init__(self, index: int, name: str) -> None:
        self.index = index
        self.name = name
        self.http_status = 0
        self.result = "失败"
        self.nodes: list[dict] = []
        self.error = ""
        self.traffic = ""
        self.expire = ""
        # Landing-page probe (SOURCE_PAGE_URL_N); public URLs only.
        self.page_url = ""
        self.page_urls: list[str] = []  # primary → backup chain
        self.page_reach = "未配置"  # 可达 / 不可达 / 未配置
        self.page_http = 0
        self.page_delay_ms = 0

    @property
    def alive(self) -> int:
        return sum(1 for node in self.nodes if node.get("_delay"))


def _classify_reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    text = type(exc).__name__.lower()
    return "timeout" if "timeout" in text else "network_error"


def http_fetch(url: str, ua: str, timeout: int,
               proxy: str | None) -> tuple[int, str, dict[str, str], str]:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            headers = {k.lower(): v for k, v in response.headers.items()}
            raw = response.read()
        return status, raw.decode("utf-8", "replace"), headers, ""
    except urllib.error.HTTPError as exc:
        return int(exc.code), "", {}, f"http_{int(exc.code)}"
    except urllib.error.URLError as exc:
        return 0, "", {}, _classify_reason(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return 0, "", {}, _classify_reason(exc)



def _page_status_ok(status: int) -> bool:
    """Portal login walls (401/403) still count as reachable."""
    return (200 <= status <= 399) or status in (401, 403)


def probe_page(url: str, ua: str, timeout: int,
               proxy: str | None) -> tuple[str, int, int, str]:
    """Probe a public landing page via optional local proxy.

    Returns (reachability, http_status, delay_ms, class). class is safe to log.
    Empty url -> 未配置. Network failure or non-OK status -> 不可达.
    """
    if not (url or "").strip():
        return "未配置", 0, 0, "unconfigured"

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": ua or convert_sub.DEFAULT_USER_AGENT}

    def _one(method: str) -> tuple[int, str]:
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 0) or 0)
                try:
                    response.read(64)
                except Exception:
                    pass
            return status, ""
        except urllib.error.HTTPError as exc:
            return int(exc.code), f"http_{int(exc.code)}"
        except urllib.error.URLError as exc:
            return 0, _classify_reason(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return 0, _classify_reason(exc)

    started = time.monotonic()
    status, err = _one("HEAD")
    # Some portals reject HEAD; fall back to GET once.
    if status == 0 or (err and not _page_status_ok(status)):
        if err in {"timeout", "network_error"} or status == 0 or status in (405, 501):
            status2, err2 = _one("GET")
            if status2 or not err2:
                status, err = status2, err2
    delay_ms = max(0, int((time.monotonic() - started) * 1000))

    if err in {"timeout", "network_error"} and status == 0:
        return "不可达", 0, delay_ms, err
    if _page_status_ok(status):
        return "可达", status, delay_ms, "ok"
    if status:
        return "不可达", status, delay_ms, f"http_{status}"
    return "不可达", 0, delay_ms, err or "no_status"



def strip_url_fragment(url: str) -> str:
    """Delay probes should not send #fragment; keep scheme/host/path/query."""
    return urllib.parse.urldefrag((url or "").strip()).url


def list_live_leaf_names(api: str, token: str | None, log: "Logger") -> list[str]:
    """Read-only leaf proxy names from the live controller (no group switch)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        request = urllib.request.Request(f"{api.rstrip('/')}/proxies", headers=headers)
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        log.warn("page_probe_nodes controller_unreachable")
        return []
    skip_types = {"selector", "urltest", "fallback", "loadbalance", "relay", "compatible"}
    skip_names = {"DIRECT", "REJECT", "PASS", "GLOBAL", "COMPATIBLE"}
    names: list[str] = []
    for name, meta in (data.get("proxies") or {}).items():
        if not isinstance(name, str) or name in skip_names:
            continue
        ptype = str((meta or {}).get("type") or "").lower()
        if ptype in skip_types:
            continue
        names.append(name)
    names.sort()
    log.info(f"page_probe_nodes live_leafs={len(names)}")
    return names



def normalize_page_url(url: str) -> str:
    """Strip fragment; IDN-encode non-ASCII hosts (e.g. 咖啡云.net)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    raw = urllib.parse.urldefrag(raw).url
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw
    host = parts.hostname or ""
    if host and any(ord(ch) > 127 for ch in host):
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except Exception:
            ascii_host = host
        # Rebuild netloc preserving port/userinfo if any.
        port = parts.port
        netloc = ascii_host if port is None else f"{ascii_host}:{port}"
        raw = urllib.parse.urlunsplit(
            (parts.scheme, netloc, parts.path, parts.query, "")
        )
    return raw


def parse_page_urls(sources_env: dict[str, str], index: int) -> list[str]:
    """Unified multi-URL parse for SOURCE_PAGE_URL_N (comma/whitespace) + _N_1/_N_2/_N_3."""
    chunks: list[str] = []
    primary = (sources_env.get(f"SOURCE_PAGE_URL_{index}") or "").strip()
    if primary:
        chunks.append(primary)
    for suffix in (1, 2, 3):
        extra = (sources_env.get(f"SOURCE_PAGE_URL_{index}_{suffix}") or "").strip()
        if extra:
            chunks.append(extra)
    urls: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        # Split on comma or any whitespace; keep order.
        for part in re.split(r"[\s,]+", chunk):
            part = part.strip()
            if not part:
                continue
            normalized = normalize_page_url(part)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)
    return urls


def node_region_bucket(name: str) -> str:
    """Coarse region for page-probe scatter (TW/US preferred over SG)."""
    if TW_PATTERN.search(name):
        return "TW"
    if US_PATTERN.search(name):
        return "US"
    if SG_PATTERN.search(name):
        return "SG"
    return "OTHER"




def interleave_by_region(names: list[str], prefer: tuple[str, ...] = ("TW", "US", "OTHER", "SG")) -> list[str]:
    """Round-robin across region buckets so SG does not burn consecutive slots."""
    buckets: dict[str, list[str]] = {key: [] for key in prefer}
    for name in names:
        buckets.setdefault(node_region_bucket(name), [])
        bucket = node_region_bucket(name)
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append(name)
    order = [key for key in prefer if buckets.get(key)]
    for key in buckets:
        if key not in order and buckets[key]:
            order.append(key)
    result: list[str] = []
    indexes = {key: 0 for key in order}
    while True:
        progressed = False
        for key in order:
            idx = indexes[key]
            series = buckets.get(key) or []
            if idx < len(series):
                result.append(series[idx])
                indexes[key] = idx + 1
                progressed = True
        if not progressed:
            break
    return result


def pick_page_probe_candidates(
    result: "SourceResult",
    live_names: list[str],
    delays: dict[str, int | None],
    limit: int = 12,
    preferred_names: list[str] | None = None,
) -> list[str]:
    """Prefer AUTO-FOREIGN (caller-given) members, then this source's nodes, then live leafs.

    Region-interleaved so SINGAPORE does not burn consecutive early slots.
    """
    live_set = set(live_names)
    seen: set[str] = set()
    preferred: list[str] = []
    for name in preferred_names or []:
        if name in live_set and name not in seen:
            seen.add(name)
            preferred.append(name)
    # Put url-test/select "now" already first in preferred_names by caller.
    pref_alive = [n for n in preferred if delays.get(n)]
    pref_rest = [n for n in preferred if n not in set(pref_alive)]
    seed = interleave_by_region(pref_alive) + interleave_by_region(pref_rest)

    source_names = [
        node.get("name")
        for node in result.nodes
        if isinstance(node.get("name"), str) and node.get("name") in live_set
    ]
    ordered: list[str] = []
    for name in source_names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    alive = [n for n in ordered if delays.get(n)]
    rest = [n for n in ordered if n not in set(alive)]
    seed = seed + interleave_by_region(alive) + interleave_by_region(rest)
    for name in seed:
        seen.add(name)
    extras = [n for n in live_names if n not in seen]
    extras = interleave_by_region(extras)
    # AUTO-FOREIGN "now" stays pinned first; preferred members next (region
    # interleaved), then source/live leafs (region interleaved).
    pinned = preferred[:1]
    pref_others = [n for n in seed if n in set(preferred) and n not in pinned]
    others = [n for n in seed + extras if n not in set(preferred)]
    candidates = pinned + interleave_by_region(pref_others) + interleave_by_region(others)
    return candidates[:limit]


def probe_page_via_nodes(
    api: str,
    token: str | None,
    url: str,
    candidates: list[str],
    timeout_ms: int,
    log: "Logger",
    budget_s: float = 50.0,
) -> tuple[bool, int, str | None, int]:
    """Try /proxies/{node}/delay for the page URL. Does not switch select groups.

    On failure, deprioritize the rest of that node's region so TW/US still get budget.
    Returns (ok, delay_ms, node_name_or_None, tried_count).
    """
    target = strip_url_fragment(url)
    if not target or not candidates:
        return False, 0, None, 0
    pending = list(candidates)
    tried = 0
    deadline = time.monotonic() + max(5.0, budget_s)
    while pending:
        if time.monotonic() >= deadline:
            log.info(f"page_probe_nodes budget_exhausted tried={tried}")
            break
        name = pending.pop(0)
        tried += 1
        delay = request_delay(api, name, target, timeout_ms, token)
        if delay and delay > 0:
            return True, int(delay), name, tried
        # Deprioritize remaining nodes in the same region bucket.
        failed_bucket = node_region_bucket(name)
        same: list[str] = []
        others: list[str] = []
        for item in pending:
            (same if node_region_bucket(item) == failed_bucket else others).append(item)
        pending = others + same
    return False, 0, None, tried


def _format_bytes(value: int) -> str:
    if value <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f}{unit}" if unit != "B" else f"{value}B"
        value /= 1024.0
    return ""


def parse_userinfo(headers: dict[str, str]) -> tuple[str, str]:
    raw = headers.get("subscription-userinfo") or headers.get("subscription-user-info") or ""
    if not raw:
        return "", ""
    values: dict[str, str] = {}
    for part in raw.split(";"):
        key, _, value = part.partition("=")
        values[key.strip().lower()] = value.strip()
    traffic = ""
    try:
        total = int(float(values.get("total", "0") or 0))
        upload = int(float(values.get("upload", "0") or 0))
        download = int(float(values.get("download", "0") or 0))
        if total > 0:
            used = min(upload + download, total)
            traffic = f"剩余 {_format_bytes(total - used)} / 共 {_format_bytes(total)}"
    except (TypeError, ValueError):
        traffic = ""
    expire = ""
    try:
        stamp = int(float(values.get("expire", "0") or 0))
        if stamp > 0:
            expire = dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        expire = ""
    return traffic, expire


def collect_source(index: int, url: str, ua: str, timeout: int,
                   fallback_proxy: str | None, log: Logger) -> SourceResult:
    result = SourceResult(index, SOURCE_NAMES.get(index, f"源{index}"))
    if not url:
        result.error = "url_missing"
        log.warn(f"source#{index} url_missing")
        return result

    status, text, headers, err = http_fetch(url, ua, timeout, None)
    if err and fallback_proxy:
        log.info(f"source#{index} direct failed class={err}; retry via local proxy")
        status2, text2, headers2, err2 = http_fetch(url, ua, timeout, fallback_proxy)
        if not err2:
            status, text, headers, err = status2, text2, headers2, ""
        else:
            err = err2 if not status else err
    result.http_status = status
    result.traffic, result.expire = parse_userinfo(headers)

    if err:
        result.error = err
        log.warn(f"source#{index} fetch failed status={status} class={err}")
        return result
    if not text.strip():
        result.error = "empty_body"
        log.warn(f"source#{index} empty body status={status}")
        return result

    try:
        nodes, stats = convert_sub.convert_content(
            text, warn=lambda message: log.info(f"source#{index} convert: {message}")
        )
    except convert_sub.ConvertError as exc:
        result.error = f"parse_error(code={exc.exit_code})"
        log.warn(f"source#{index} parse failed class=parse_error")
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"parse_error({type(exc).__name__.lower()})"
        log.warn(f"source#{index} parse failed class=parse_error")
        return result

    result.nodes = nodes
    if nodes:
        result.result = "成功"
        log.info(
            f"source#{index} parsed status={status} nodes={len(nodes)} "
            f"format={stats.input_format} skipped={stats.input_nodes - stats.output_nodes}"
        )
    else:
        result.error = "no_nodes"
        log.warn(f"source#{index} produced no nodes status={status}")
    return result


def load_cache_nodes(paths: list[str], log: Logger) -> list[dict]:
    """Load proxy nodes from yaml/txt cache files via convert_sub when possible."""
    nodes: list[dict] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        valid: list[dict] = []
        try:
            converted, stats = convert_sub.convert_content(content, warn=lambda _m: None)
            proxies = (converted or {}).get("proxies") if isinstance(converted, dict) else None
            if isinstance(proxies, list):
                valid = [p for p in proxies if isinstance(p, dict) and p.get("name") and p.get("type")]
                if valid:
                    log.info(
                        f"cache used path_suffix={path.suffix} nodes={len(valid)} "
                        f"format={getattr(stats, 'input_format', 'unknown')}"
                    )
        except Exception:
            valid = []
        if not valid:
            try:
                data = yaml.safe_load(content) if yaml is not None else None
            except Exception:
                log.warn("cache parse failed class=parse_error")
                continue
            proxies = (data or {}).get("proxies") if isinstance(data, dict) else None
            if isinstance(proxies, list):
                valid = [p for p in proxies if isinstance(p, dict) and p.get("name") and p.get("type")]
                if valid:
                    log.info(f"cache used path_suffix={path.suffix} nodes={len(valid)} format=yaml")
        if valid:
            nodes = valid
            break
    return nodes


def build_pool(results: list[SourceResult]) -> list[tuple[dict, str]]:
    pool: list[tuple[dict, str]] = []
    seen_keys: set[tuple] = set()
    used_names: set[str] = set()
    for result in results:
        for node in result.nodes:
            key = (node.get("type"), node.get("server"), node.get("port"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            name = str(node.get("name") or f"{result.name}-{len(pool) + 1}")
            candidate, suffix = name, 2
            while candidate in used_names:
                candidate = f"{name} [{suffix}]"
                suffix += 1
            used_names.add(candidate)
            pool.append(({**node, "name": candidate}, result.name))
    return pool


# ---------------------------------------------------------------------------
# Speed test (ChatGPT trace) via mihomo controller
# ---------------------------------------------------------------------------

def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_ports(mixed_port: int, controller_port: int) -> tuple[int, int]:
    for offset in range(0, 60, 2):
        mixed, controller = mixed_port + offset, controller_port + offset
        if _port_free(mixed) and _port_free(controller):
            return mixed, controller
    raise SyncError("no_free_port")


# Per-node geo via isolated mixed-port (mode=rule + TEST select). Never use live :7890.
IPINFO_TIMEOUT_S = 5.0
IPINFO_TOTAL_BUDGET_S = 600.0  # 10 minutes hard cap for the region pass
GEO_ENDPOINTS = (
    "https://api.ip.sb/geoip",
    "https://ipapi.co/json/",
    "https://ipinfo.io/json",
)


def select_proxy(api: str, group: str, name: str, token: str | None = None) -> bool:
    """PUT /proxies/{group} {"name": ...}. Isolated TEST only — never GPT/MUSE/US-FAST."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({"name": name}).encode("utf-8")
    req = urllib.request.Request(
        f"{api}/proxies/{urllib.parse.quote(group, safe='')}",
        data=body,
        headers=headers,
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except Exception:
        return False


def _mixed_opener(mixed_port: int) -> urllib.request.OpenerDirector:
    """Force HTTP(S) through isolated mixed-port; ignore process HTTP(S)_PROXY env (:7890)."""
    proxy = f"http://127.0.0.1:{mixed_port}"
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
    )


@contextlib.contextmanager
def _force_proxy_env():
    """urllib ProxyHandler still honors NO_PROXY/no_proxy (*=bypass). Clear while forcing mixed-port."""
    keys = (
        "NO_PROXY", "no_proxy",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    )
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _geo_key(value: str) -> str:
    """Normalize a geo label for case-insensitive dict lookup."""
    return re.sub(r"[\s_]+", " ", (value or "").strip().lower())


def to_zh_geo_part(kind: str, value: str) -> str:
    """Map one geo part to Chinese; unknown values keep their original spelling."""
    text = (value or "").strip()
    if not text:
        return ""
    if kind == "country":
        zh = COUNTRY_ZH.get(text.upper())
        if zh:
            return zh
        return COUNTRY_NAME_ZH.get(_geo_key(text), text)
    return REGION_CITY_ZH.get(_geo_key(text), text)


def format_geo_payload(payload: dict) -> str:
    """Normalize api.ip.sb / ipapi.co / ipinfo.io JSON to 中文 国家/地区/城市."""
    country = str(
        payload.get("country_code")
        or payload.get("countryCode")
        or payload.get("country")
        or ""
    ).strip()
    if len(country) > 3 and _geo_key(country) not in COUNTRY_NAME_ZH:
        country = str(
            payload.get("country_code") or payload.get("countryCode") or country[:2]
        ).strip()
    region = str(
        payload.get("region")
        or payload.get("region_name")
        or payload.get("regionName")
        or ""
    ).strip()
    city = str(payload.get("city") or "").strip()
    parts: list[str] = []
    for part in (
        to_zh_geo_part("country", country),
        to_zh_geo_part("region", region),
        to_zh_geo_part("city", city),
    ):
        # Collapse repeats: HK/SG payloads often echo the country in region+city.
        if part and part not in parts:
            parts.append(part)
    return "/".join(parts) if parts else ""


def format_cf_trace(text: str) -> str:
    """Parse cloudflare cdn-cgi/trace (loc=CC, ip=...); loc mapped to 中文 country."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            fields[key.strip()] = val.strip()
    loc = to_zh_geo_part("country", fields.get("loc") or "")
    ip = fields.get("ip") or ""
    if loc and ip:
        return f"{loc}/{ip}"
    return loc or ip or ""


def fetch_geo_via_mixed(mixed_port: int, timeout_s: float = IPINFO_TIMEOUT_S) -> str:
    """GET geo JSON/trace through isolated mixed-port for the currently selected leaf."""
    opener = _mixed_opener(mixed_port)
    headers = {"User-Agent": "mihomo-feishu-sync/geo", "Accept": "application/json"}
    with _force_proxy_env():
        for url in GEO_ENDPOINTS:
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=timeout_s) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    text = format_geo_payload(payload)
                    if text:
                        return text
            except Exception:
                continue
        try:
            req = urllib.request.Request(
                "https://cloudflare.com/cdn-cgi/trace",
                headers={"User-Agent": "mihomo-feishu-sync/geo"},
            )
            with opener.open(req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8", "replace")
            text = format_cf_trace(raw)
            if text:
                return text
        except Exception:
            pass
    return "未知"


def probe_direct_geo(timeout_s: float = IPINFO_TIMEOUT_S) -> str:
    """Best-effort geo of THIS host without any proxy (fingerprint for bind-fail detection)."""
    headers = {"User-Agent": "mihomo-feishu-sync/geo-direct", "Accept": "application/json"}
    # Must NOT use mixed opener — intentional direct path.
    for url in GEO_ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            if isinstance(payload, dict):
                text = format_geo_payload(payload)
                if text:
                    return text
        except Exception:
            continue
    return ""


def validate_region_diversity(
    regions: dict[str, str],
    names: list[str],
    log: "Logger",
    direct_geo: str = "",
) -> dict[str, str]:
    """Blank to 未知 when bind failed: all identical, or matches host DIRECT egress."""
    # Strip any leaf that merely echoed the machine's own egress.
    if direct_geo:
        stripped = 0
        for name in list(regions):
            if regions.get(name) == direct_geo:
                regions[name] = "未知"
                stripped += 1
        if stripped:
            log.warn(
                f"ipinfo direct_egress_stripped count={stripped} direct={direct_geo} "
                f"— those leaves matched host WAN, not proxy"
            )
    real = [regions.get(n) or "未知" for n in names]
    real = [v for v in real if v and v != "未知"]
    if len(real) < 2:
        if real and direct_geo and real[0] == direct_geo:
            log.error(
                f"ipinfo diversity_fail direct_only={direct_geo} — blanking to 未知"
            )
            return {name: "未知" for name in names}
        return regions
    unique = set(real)
    if len(unique) == 1:
        only = next(iter(unique))
        log.error(
            f"ipinfo diversity_fail identical={only} count={len(real)} "
            f"— egress not per-leaf; blanking to 未知"
        )
        return {name: "未知" for name in names}
    log.info(f"ipinfo diversity_ok unique={len(unique)} sample={sorted(unique)[:6]}")
    return regions


def lookup_regions_isolated(
    api: str,
    mixed_port: int,
    names: list[str],
    log: "Logger",
    timeout_s: float = IPINFO_TIMEOUT_S,
    budget_s: float = IPINFO_TOTAL_BUDGET_S,
) -> dict[str, str]:
    """Sequential: PUT TEST to leaf (mode=rule) -> geo via that mixed-port. No live switches."""
    regions: dict[str, str] = {}
    started = time.monotonic()
    ok = 0
    direct_geo = probe_direct_geo(timeout_s=timeout_s)
    if direct_geo:
        log.info(f"ipinfo direct_fingerprint={direct_geo}")
    else:
        log.warn("ipinfo direct_fingerprint unavailable")
    for index, name in enumerate(names, 1):
        if time.monotonic() - started > budget_s:
            log.warn(
                f"ipinfo budget_exhausted tried={index - 1} ok={ok} remain={len(names) - index + 1}"
            )
            for rest in names[index - 1 :]:
                regions.setdefault(rest, "未知")
            break
        if not select_proxy(api, "TEST", name):
            regions[name] = "未知"
            continue
        try:
            with urllib.request.urlopen(
                f"{api}/proxies/{urllib.parse.quote('TEST', safe='')}", timeout=3
            ) as resp:
                now = json.loads(resp.read().decode("utf-8", "replace")).get("now")
            if now != name:
                log.warn(f"ipinfo select_mismatch want={name} now={now}")
                regions[name] = "未知"
                continue
        except Exception:
            regions[name] = "未知"
            continue
        value = fetch_geo_via_mixed(mixed_port, timeout_s=timeout_s)
        regions[name] = value
        if value and value != "未知":
            ok += 1
        if index % 20 == 0 or index == len(names):
            log.info(f"ipinfo progress={index}/{len(names)} ok={ok}")
    log.info(f"ipinfo done tested={len(regions)} ok={ok}")
    return validate_region_diversity(regions, names, log, direct_geo=direct_geo)



def request_delay(api: str, name: str, target: str, timeout_ms: int,
                  token: str | None = None) -> int | None:
    query = urllib.parse.urlencode({"url": target, "timeout": str(timeout_ms)})
    endpoint = f"{api}/proxies/{urllib.parse.quote(name, safe='')}/delay?{query}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(endpoint, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=(timeout_ms / 1000) + 5) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        value = int(payload.get("delay") or 0)
        return value if value > 0 else None
    except Exception:
        return None


def test_isolated(core: str, names: list[str], proxies: list[dict], mixed_port: int,
                  controller_port: int, target: str, timeout_ms: int, workers: int,
                  log: Logger) -> tuple[dict[str, int | None], dict[str, str]]:
    if not Path(core).exists():
        raise SyncError("core_missing")
    mixed, controller = pick_ports(mixed_port, controller_port)
    runtime = Path(tempfile.mkdtemp(prefix="mihomo-feishu-"))
    api = f"http://127.0.0.1:{controller}"
    # mode=rule required: global ignores MATCH,TEST; mixed would follow GLOBAL.
    config = {
        "mixed-port": mixed,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "external-controller": f"127.0.0.1:{controller}",
        "dns": {"enable": True, "enhanced-mode": "redir-host", "nameserver": ["8.8.8.8", "1.1.1.1"]},
        "tun": {"enable": False},
        "proxies": proxies,
        "proxy-groups": [{"name": "TEST", "type": "select", "proxies": names}],
        "rules": ["MATCH,TEST"],
    }
    config_path = runtime / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [core, "-d", str(runtime), "-f", str(config_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        deadline = time.monotonic() + 20
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SyncError("core_exited")
            try:
                with urllib.request.urlopen(f"{api}/version", timeout=1):
                    ready = True
                    break
            except Exception:
                time.sleep(0.3)
        if not ready:
            raise SyncError("core_not_ready")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            delays = dict(
                zip(names, pool.map(lambda n: request_delay(api, n, target, timeout_ms), names))
            )
        # Separate pass: egress IP geo via ipinfo through each node (not ChatGPT delay).
        log.info(f"ipinfo enter nodes={len(names)} timeout_s={IPINFO_TIMEOUT_S}")
        regions = lookup_regions_isolated(api, mixed, names, log)
        return delays, regions
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(runtime, ignore_errors=True)


def test_live(api: str, token: str | None, names: list[str], target: str,
              timeout_ms: int, workers: int, log: Logger) -> dict[str, int | None]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        request = urllib.request.Request(f"{api}/proxies", headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        raise SyncError("controller_unreachable") from None
    available = set((data.get("proxies") or {}).keys())
    testable = [name for name in names if name in available]
    log.info(f"live controller available={len(available)} testable={len(testable)}")
    if not testable:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        delays = dict(
            zip(
                testable,
                pool.map(lambda n: request_delay(api, n, target, timeout_ms, token), testable),
            )
        )
    return delays


# ---------------------------------------------------------------------------
# Region / proxy-group inference
# ---------------------------------------------------------------------------

def region_of(name: str) -> str | None:
    if SG_PATTERN.search(name):
        return "新加坡"
    if US_PATTERN.search(name):
        return "美国"
    return None


def group_from_actual_area(area: str) -> str | None:
    """Coarse 地区组 from the measured egress path (国家/地区/城市), not the node name."""
    text = (area or "").strip()
    if not text or text == "未知":
        return None
    country = text.split("/", 1)[0].strip() or text
    if SG_PATTERN.search(country):
        return "新加坡"
    if US_PATTERN.search(country):
        return "美国"
    return None


def load_group_members(config_path: str, log: Logger) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    path = Path(config_path)
    if not path.exists():
        return groups
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        log.warn("config parse failed class=parse_error")
        return groups
    for group in (data or {}).get("proxy-groups") or []:
        if not isinstance(group, dict):
            continue
        name = group.get("name")
        members = group.get("proxies")
        if isinstance(name, str) and isinstance(members, list):
            groups[name] = [m for m in members if isinstance(m, str)]
    return groups


def pick_group(node_name: str, region: str, groups: dict[str, list[str]]) -> str:
    keywords = ("SINGAPORE", "SG", "新加坡", "狮城") if region == "新加坡" else (
        "US", "USA", "AMERICA", "GPT", "美国"
    )
    matched = [
        name for name, members in groups.items()
        if node_name in members and any(keyword in name.upper() for keyword in keywords)
    ]
    if matched:
        return "/".join(sorted(matched)[:3])
    return "SINGAPORE" if region == "新加坡" else "US-FAST"


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------

def compact(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None and v != ""}


def build_source_records(results: list[SourceResult], sync_ms: int) -> list[dict]:
    records = []
    for result in results:
        fields = compact(
            {
                "源名称": result.name,
                "源编号": result.index,
                "HTTP状态": result.http_status or 0,
                "拉取结果": result.result,
                "节点数": len(result.nodes),
                "存活数": result.alive,
                "流量剩余": result.traffic,
                "到期时间": result.expire,
                "失败原因": result.error,
                "页面网址": result.page_url,
                "页面可达": result.page_reach,
                "页面HTTP状态": result.page_http if result.page_reach != "未配置" else 0,
                "页面探测延迟ms": result.page_delay_ms if result.page_reach != "未配置" else 0,
                "同步时间": sync_ms,
            }
        )
        records.append({"fields": fields})
    return records


def build_node_records(pool: list[tuple[dict, str]], delays: dict[str, int | None],
                       target: str, sync_ms: int,
                       regions: dict[str, str] | None = None) -> list[dict]:
    regions = regions or {}
    records = []
    for node, source in pool:
        name = node["name"]
        delay = delays.get(name)
        # ipinfo via node only — never invent from node name; missing → 未知.
        area = (regions.get(name) or "").strip() or "未知"
        fields = compact(
            {
                "节点名": name,
                "来源": source or "未知",
                "协议": str(node.get("type") or ""),
                "实际地区": area,
                "延迟ms": delay if delay else -1,
                "存活": bool(delay),
                "测速目标": target,
                "同步时间": sync_ms,
            }
        )
        records.append({"fields": fields})
    return records


def build_region_records(pool: list[tuple[dict, str]], delays: dict[str, int | None],
                         groups: dict[str, list[str]], sync_ms: int,
                         regions: dict[str, str] | None = None) -> list[dict]:
    regions = regions or {}
    records = []
    for node, source in pool:
        name = node["name"]
        delay = delays.get(name)
        if not delay:
            continue
        # Gate on measured egress only — never on the node name; 未知/non-US-SG is out.
        area = (regions.get(name) or "").strip() or "未知"
        region = group_from_actual_area(area)
        if not region:
            continue
        fields = compact(
            {
                "节点名": name,
                "地区组": region,
                "实际地区": area,
                "来源": source or "未知",
                "延迟ms": delay,
                "GPT可用": True,
                "代理组": pick_group(name, region, groups),
                "同步时间": sync_ms,
            }
        )
        records.append({"fields": fields})
    return records


# ---------------------------------------------------------------------------
# Table write (one batch per run)
# ---------------------------------------------------------------------------

def replace_records(client: FeishuClient, table_id: str, records: list[dict],
                    log: Logger) -> int:
    """Create new rows first, then delete old ones — never leave the table empty.

    An empty *records* payload with existing rows is refused so a bad run cannot
    wipe production tables.
    """
    existing = client.list_records(table_id)
    ids = [r.get("record_id") for r in existing if r.get("record_id")]
    if not records:
        if ids:
            log.warn(f"records replace refused empty_payload keep_old={len(ids)}")
            return 0
        log.info("records created=0")
        return 0
    created = client.batch_create(table_id, records)
    log.info(f"records created={created}")
    if ids:
        deleted = client.batch_delete(table_id, ids)
        log.info(f"records deleted={deleted}")
    return created


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync mihomo status into a Feishu Bitable.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="File holding FEISHU_APP_ID/SECRET")
    parser.add_argument("--app-token", default=DEFAULT_APP_TOKEN)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    parser.add_argument("--sources-env", default=DEFAULT_SOURCES_ENV)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--timeout-ms", type=int, default=7000)
    parser.add_argument("--fetch-timeout", type=int, default=30)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--core", default=DEFAULT_CORE)
    parser.add_argument("--mixed-port", type=int, default=17890)
    parser.add_argument("--controller-port", type=int, default=19090)
    parser.add_argument("--live-controller", default=DEFAULT_LIVE_CONTROLLER)
    parser.add_argument("--live-env", default=DEFAULT_LIVE_ENV)
    parser.add_argument("--config", default=r"C:\mihomo\config.yaml")
    parser.add_argument("--fetch-proxy", default=DEFAULT_FETCH_PROXY)
    parser.add_argument("--source1-cache", action="append", default=None,
                        help="Candidate YAML used when source 1 fails (repeatable)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Test at most N nodes (0 = all); dry-run only — forbidden with production writes")
    parser.add_argument("--dry-run", action="store_true", help="Collect and test only; never write")
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE,
                        help="Single-instance lock file (metadata: pid+ts+token)")
    parser.add_argument("--ensure-tables", action="store_true", help="Only create/repair tables")
    parser.add_argument("--no-speedtest", action="store_true")
    parser.add_argument("--keep-placeholder", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def print_summary(tables: dict[str, dict[str, str]], script_path: str, results: list[SourceResult],
                  node_rows: int, region_rows: int, error: str) -> None:
    labels = {"sources": "表1 订阅源状态", "nodes": "表2 可测速节点", "gpt_regions": "表3 GPT可用地区组"}
    for key, label in labels.items():
        entry = tables.get(key) or {}
        print(f"{label}\ttable_id={entry.get('table_id', '?')}\t{entry.get('name', '')}")
    print(f"脚本\t{script_path}")
    source1 = next((r for r in results if r.index == 1), None)
    http1 = source1.http_status if source1 else 0
    print(f"试跑\t表1={len(results)}行\t源1HTTP={http1}\t表2={node_rows}行\t表3={region_rows}行\t错误={error or 'none'}")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = parse_args(argv)
    log = Logger(args.log_file, verbose=args.verbose)
    log.info("sync start")

    if args.limit and not args.dry_run:
        log.error("limit_write_forbidden: --limit cannot write production (use --dry-run)")
        print("ERROR	limit_write_forbidden")
        return 2

    tables = load_state(args.state_file).get("tables") or {}
    results: list[SourceResult] = []
    node_rows = 0
    region_rows = 0
    error = ""
    sync_ms = int(time.time() * 1000)
    lock = SyncLock(args.lock_file, log)

    try:
        try:
            lock.acquire()
        except SyncError as exc:
            error = exc.kind
            log.error(f"sync failed class={exc.kind} detail={exc.detail}")
            print_summary(tables, str(Path(__file__).resolve()), results, node_rows, region_rows, error)
            return 1

        env = read_env_file(args.env_file)
        app_id = (env.get("FEISHU_APP_ID") or "").strip()
        app_secret = (env.get("FEISHU_APP_SECRET") or "").strip()
        if not app_id or not app_secret:
            raise SyncError("token_error", "credentials_missing")
        client = FeishuClient(app_id, app_secret, args.app_token, log)

        if args.dry_run:
            log.info("dry run: table provisioning skipped")
            if not tables:
                log.warn("dry run without state file; table ids unknown")
        else:
            tables = ensure_tables(client, args.state_file, args.keep_placeholder, log)
        if args.ensure_tables:
            print_summary(tables, str(Path(__file__).resolve()), [], 0, 0, "")
            log.info("ensure-tables done")
            return 0

        # --- sources -----------------------------------------------------
        sources_env = read_env_file(args.sources_env)
        # Optional overlay from live env (same key names) when present.
        live_overlay = read_env_file(args.live_env)
        for key, value in live_overlay.items():
            if key.startswith("SOURCE_PAGE_URL_") and (value or "").strip():
                sources_env[key] = value
        page_timeout = min(int(args.fetch_timeout), 15)
        for index in (1, 2, 3):
            url = (sources_env.get(f"SUBSCRIPTION_URL_{index}") or "").strip()
            ua = (sources_env.get(f"SUBSCRIPTION_UA_{index}") or "").strip() or convert_sub.DEFAULT_USER_AGENT
            result = collect_source(index, url, ua, args.fetch_timeout, args.fetch_proxy, log)
            page_urls = parse_page_urls(sources_env, index)
            result.page_urls = page_urls
            result.page_url = page_urls[0] if page_urls else ""
            if not page_urls:
                result.page_reach = "未配置"
                result.page_http = 0
                result.page_delay_ms = 0
                log.info(
                    f"source#{index} page_probe status=0 class=unconfigured "
                    f"delay_ms=0 reach=未配置"
                )
            else:
                # First pass: try each URL via fetch_proxy with browser Chrome UA.
                found = False
                last_status, last_delay, last_class = 0, 0, "network_error"
                for page_url in page_urls:
                    reach, page_status, page_delay, page_class = probe_page(
                        page_url, PAGE_PROBE_UA, page_timeout, args.fetch_proxy
                    )
                    last_status, last_delay, last_class = page_status, page_delay, page_class
                    # TLS/urllib failure is fine — node delay is authoritative later.
                    log.info(
                        f"source#{index} page_probe status={page_status} class={page_class} "
                        f"delay_ms={page_delay} reach={reach} urls={len(page_urls)}"
                    )
                    if reach == "可达":
                        result.page_url = page_url
                        result.page_reach = "可达"
                        result.page_http = page_status
                        result.page_delay_ms = page_delay
                        found = True
                        break
                if not found:
                    result.page_reach = "不可达"
                    result.page_http = last_status
                    result.page_delay_ms = last_delay
            results.append(result)

        # --- source 1 cache fallback ------------------------------------
        first = next((r for r in results if r.index == 1), None)
        if first is not None and first.result == "失败":
            cache_paths = args.source1_cache or [
                r"C:\mihomo\sub1_check.txt",
                r"C:\mihomo\providers\sub1-cache.yaml",
                r"C:\mihomo\providers\sub1_check.yaml",
                r"C:\mihomo\providers\source1.yaml",
                r"C:\mihomo\sub1_check.yaml",
                r"C:\mihomo\providers\sub1_check.txt",
            ]
            cached = load_cache_nodes(cache_paths, log)
            if cached:
                first.nodes = cached
                first.result = "缓存"
                first.error = f"{first.error or 'fetch_failed'}; using cache"

        # --- pool + speed test ------------------------------------------
        pool = build_pool(results)
        if args.limit and len(pool) > args.limit:
            pool = pool[: args.limit]
        log.info(f"pool nodes={len(pool)}")

        delays: dict[str, int | None] = {}
        regions: dict[str, str] = {}
        speedtest_ran = False
        if not pool:
            log.info("speedtest skipped reason=empty_pool")
        elif args.no_speedtest:
            # Page-probe / quick runs must NOT wipe table2/table3 delays.
            log.info("speedtest skipped reason=--no-speedtest")
        else:
            names = [node["name"] for node, _ in pool]
            proxies = [{k: v for k, v in node.items() if not k.startswith("_")} for node, _ in pool]
            log.info(f"speedtest enter mode=isolated nodes={len(names)}")
            try:
                delays, regions = test_isolated(
                    args.core, names, proxies, args.mixed_port,
                    args.controller_port, args.target, args.timeout_ms,
                    args.workers, log,
                )
                log.info(f"speedtest isolated done tested={len(delays)}")
                speedtest_ran = True
            except SyncError as exc:
                log.warn(f"speedtest isolated failed class={exc.kind}; trying live controller")
                live_env = read_env_file(args.live_env)
                token = (live_env.get("SECRET") or "").strip() or None
                try:
                    log.info(f"speedtest enter mode=live nodes={len(names)}")
                    delays = test_live(args.live_controller, token, names, args.target,
                                       args.timeout_ms, args.workers, log)
                    log.info(f"speedtest live done tested={len(delays)}")
                    regions = {name: "未知" for name in names}
                    log.warn("ipinfo skipped reason=live_fallback_no_group_switch")
                    speedtest_ran = True
                except SyncError as live_exc:
                    log.error(f"speedtest unavailable class={live_exc.kind}")
                    delays = {}
                    regions = {}
        for node, _ in pool:
            node["_delay"] = delays.get(node["name"])

        # Backfill delays onto each source's own node list so 表1「存活数」 matches 表2.
        delay_by_endpoint: dict[tuple, int | None] = {}
        for node, _source_name in pool:
            key = (node.get("type"), node.get("server"), node.get("port"))
            delay_by_endpoint[key] = delays.get(node["name"])
        for result in results:
            for node in result.nodes:
                key = (node.get("type"), node.get("server"), node.get("port"))
                if key in delay_by_endpoint:
                    node["_delay"] = delay_by_endpoint[key]

        # --- page probe node retry (read-only delay API; no group switch) ---
        unreachable = [
            result for result in results
            if result.page_reach == "不可达" and (result.page_url or "").strip()
        ]
        if unreachable:
            live_env = read_env_file(args.live_env)
            node_token = (live_env.get("SECRET") or "").strip() or None
            live_names = list_live_leaf_names(args.live_controller, node_token, log)
            # Per-node delay budget: 5–8s; keep total page-retry under ~2 minutes.
            node_timeout_ms = min(8000, max(5000, int(args.timeout_ms)))
            per_source_budget = 40.0  # ~2 min total across 3 sources
            # Prefer AUTO-FOREIGN (url-test, page domains route there): "now" first, then members.
            auto_foreign_preferred: list[str] = []
            try:
                headers = {"Authorization": f"Bearer {node_token}"} if node_token else {}
                req = urllib.request.Request(
                    f"{args.live_controller.rstrip('/')}/proxies/AUTO-FOREIGN", headers=headers
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    af_group = json.loads(resp.read().decode("utf-8", "replace"))
                now = af_group.get("now")
                all_members = [m for m in (af_group.get("all") or []) if isinstance(m, str)]
                if now and now in all_members:
                    auto_foreign_preferred = [now] + [m for m in all_members if m != now]
                else:
                    auto_foreign_preferred = all_members
                # Drop nested group names from preferred.
                live_set = set(live_names)
                auto_foreign_preferred = [m for m in auto_foreign_preferred if m in live_set]
                log.info(f"page_probe_nodes prefer_group=AUTO-FOREIGN count={len(auto_foreign_preferred)}")
            except Exception:
                log.warn("page_probe_nodes AUTO-FOREIGN prefer unavailable")

            for result in unreachable:
                url_chain = result.page_urls or ([result.page_url] if result.page_url else [])
                candidates = pick_page_probe_candidates(
                    result, live_names, delays, limit=12, preferred_names=auto_foreign_preferred
                )
                # Split budget across remaining URLs so backups still get TW/US slots.
                per_url_budget = max(12.0, per_source_budget / max(1, len(url_chain)))
                total_tried = 0
                succeeded = False
                for page_url in url_chain:
                    ok, delay_ms, node_name, tried = probe_page_via_nodes(
                        args.live_controller,
                        node_token,
                        page_url,
                        candidates,
                        node_timeout_ms,
                        log,
                        budget_s=per_url_budget,
                    )
                    total_tried += tried
                    if ok and node_name:
                        result.page_url = page_url
                        result.page_reach = "可达"
                        result.page_delay_ms = delay_ms
                        # Keep first-pass HTTP status (or 0).
                        log.info(
                            f"source#{result.index} page_probe status={result.page_http} "
                            f"class=node_ok delay_ms={delay_ms} reach=可达 "
                            f"node={node_name} tried={tried} total_tried={total_tried}"
                        )
                        succeeded = True
                        break
                if not succeeded:
                    log.info(
                        f"source#{result.index} page_probe status={result.page_http} "
                        f"class=node_fail delay_ms={result.page_delay_ms} "
                        f"reach=不可达 tried={total_tried} urls={len(url_chain)}"
                    )

        # --- records -----------------------------------------------------
        source_records = build_source_records(results, sync_ms)
        node_records = build_node_records(pool, delays, args.target, sync_ms, regions)
        groups = load_group_members(args.config, log)
        region_records = build_region_records(pool, delays, groups, sync_ms, regions)
        node_rows = len(node_records)
        region_rows = len(region_records)
        alive_total = sum(1 for _, d in delays.items() if d)
        log.info(f"rows sources={len(source_records)} nodes={node_rows} regions={region_rows} "
                 f"alive={alive_total}")

        if args.dry_run:
            log.info("dry run: no write")
            print_summary(tables, str(Path(__file__).resolve()), results, node_rows, region_rows, "")
            return 0

        # --- write -------------------------------------------------------
        # Page-probe / --no-speedtest runs still refresh 表1, but must not
        # replace 表2/表3 with empty delays (that was the 12:45 wipe).
        write_keys: list[tuple[str, list]] = [("sources", source_records)]
        if speedtest_ran:
            write_keys.append(("nodes", node_records))
            write_keys.append(("gpt_regions", region_records))
        else:
            log.info(
                "table write skip keys=nodes,gpt_regions "
                "reason=speedtest_not_run (preserve last delays)"
            )
        for key, records in write_keys:
            table_id = (tables.get(key) or {}).get("table_id")
            if not table_id:
                raise SyncError("api_error", f"missing_table_id:{key}")
            count = replace_records(client, table_id, records, log)
            log.info(f"table {key} rows={count}")

        log.info("sync ok")
        print_summary(tables, str(Path(__file__).resolve()), results, node_rows, region_rows, "")
        return 0

    except SyncError as exc:
        error = exc.kind
        log.error(f"sync failed class={exc.kind} detail={exc.detail}")
    except Exception as exc:  # pragma: no cover - defensive
        error = type(exc).__name__.lower()
        log.error(f"sync failed class={error}")
    finally:
        release_sync_lock(lock, log)

    print_summary(tables, str(Path(__file__).resolve()), results, node_rows, region_rows, error)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
