"""Region measurement, classification and allocation advice.

The egress country/region/city comes from ipinfo.io/json (or the compatible
endpoints) through the probed node.  The label format is the project's existing
Chinese ``国家/地区/城市`` form.

Region groups: 美国 / 新加坡 / 日本 / 其他 / 未知.

* 美国 / 新加坡 / 日本 come from the *measured* egress first; if egress is not
  measured, a node-name regex may classify it (required by the B-part spec).
* 其他 means egress was measured but is none of the three regions.
* 未知 means neither measurement nor name matched.

Allocation advice (computed, never applied):
  * GPT and US-FAST may use US > SG > JP.
  * MUSE is US-only.
"""

from __future__ import annotations

import re
from typing import Any, Dict

GROUP_US = "美国"
GROUP_SG = "新加坡"
GROUP_JP = "日本"
GROUP_OTHER = "其他"
GROUP_UNKNOWN = "未知"

REGION_TIERS = {GROUP_US: 3, GROUP_SG: 2, GROUP_JP: 1, GROUP_OTHER: 0, GROUP_UNKNOWN: 0}

SG_PATTERN = re.compile(r"新加坡|狮城|獅城|singapore|🇸🇬|\bSGP?\b", re.IGNORECASE)
US_PATTERN = re.compile(
    r"美国|美國|united states|\bUSA?\b|🇺🇸|los angeles|san jose|seattle|dallas|chicago|"
    r"new york|ashburn|silicon valley|phoenix|miami|atlanta|las vegas|portland|denver|"
    r"virginia|ohio|california|圣何塞|洛杉矶|西雅图|达拉斯|芝加哥|纽约|硅谷|凤凰城",
    re.IGNORECASE,
)
JP_PATTERN = re.compile(
    r"日本|japan|tokyo|osaka|🇯🇵|\bJP\b|\bJPN\b|东京|東京",
    re.IGNORECASE,
)

COUNTRY_ZH: Dict[str, str] = {
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

COUNTRY_NAME_ZH: Dict[str, str] = {
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

REGION_CITY_ZH: Dict[str, str] = {
    "california": "加利福尼亚", "ca": "加利福尼亚", "new york": "纽约",
    "new york state": "纽约", "ny": "纽约", "texas": "德克萨斯",
    "tx": "德克萨斯", "washington": "华盛顿", "wa": "华盛顿",
    "illinois": "伊利诺伊", "il": "伊利诺伊", "florida": "佛罗里达",
    "fl": "佛罗里达", "georgia": "佐治亚", "ga": "佐治亚",
    "virginia": "弗吉尼亚", "va": "弗吉尼亚", "ohio": "俄亥俄",
    "oh": "俄亥俄", "oregon": "俄勒冈", "or": "俄勒冈",
    "arizona": "亚利桑那", "az": "亚利桑那", "colorado": "科罗拉多",
    "co": "科罗拉多", "nevada": "内华达", "nv": "内华达",
    "new jersey": "新泽西", "nj": "新泽西", "massachusetts": "马萨诸塞",
    "ma": "马萨诸塞", "pennsylvania": "宾夕法尼亚", "pa": "宾夕法尼亚",
    "north carolina": "北卡罗来纳", "south carolina": "南卡罗来纳",
    "michigan": "密歇根", "minnesota": "明尼苏达", "missouri": "密苏里",
    "maryland": "马里兰", "utah": "犹他", "hawaii": "夏威夷",
    "alaska": "阿拉斯加", "los angeles": "洛杉矶", "san jose": "圣何塞",
    "seattle": "西雅图", "dallas": "达拉斯", "chicago": "芝加哥",
    "new york city": "纽约", "ashburn": "阿什本", "silicon valley": "硅谷",
    "phoenix": "凤凰城", "miami": "迈阿密", "atlanta": "亚特兰大",
    "las vegas": "拉斯维加斯", "portland": "波特兰", "denver": "丹佛",
    "san francisco": "旧金山", "fremont": "弗里蒙特", "sunnyvale": "桑尼维尔",
    "santa clara": "圣克拉拉", "reston": "雷斯顿", "sterling": "斯特灵",
    "columbus": "哥伦布", "honolulu": "檀香山", "anchorage": "安克雷奇",
    "changhua": "彰化", "changhua county": "彰化", "yuanlin": "员林",
    "yuanlin city": "员林", "taipei": "台北", "taipei city": "台北",
    "new taipei": "新北", "taichung": "台中", "taichung city": "台中",
    "tainan": "台南", "kaohsiung": "高雄", "kaohsiung city": "高雄",
    "taoyuan": "桃园", "hsinchu": "新竹", "keelung": "基隆",
    "chiayi": "嘉义", "yilan": "宜兰", "hualien": "花莲",
    "taitung": "台东", "miaoli": "苗栗", "nantou": "南投",
    "yunlin": "云林", "pingtung": "屏东", "penghu": "澎湖",
    "kinmen": "金门", "hong kong": "香港", "hongkong": "香港",
    "kowloon": "九龙", "new territories": "新界", "macau": "澳门",
    "macao": "澳门", "singapore": "新加坡", "central singapore": "新加坡",
    "tokyo": "东京", "osaka": "大阪", "kyoto": "京都", "hokkaido": "北海道",
    "aichi": "爱知", "fukuoka": "福冈", "kanagawa": "神奈川",
    "saitama": "埼玉", "chiba": "千叶", "hyogo": "兵库", "hiroshima": "广岛",
    "okinawa": "冲绳", "seoul": "首尔", "busan": "釜山", "incheon": "仁川",
    "gyeonggi": "京畿", "gyeonggi-do": "京畿", "hessen": "黑森",
    "hesse": "黑森", "bavaria": "巴伐利亚", "bayern": "巴伐利亚",
    "berlin": "柏林", "hamburg": "汉堡", "munich": "慕尼黑",
    "münchen": "慕尼黑", "saxony": "萨克森",
    "north rhine-westphalia": "北莱茵-威斯特法伦",
    "baden-württemberg": "巴登-符腾堡", "lower saxony": "下萨克森",
    "frankfurt": "法兰克福", "frankfurt am main": "法兰克福",
    "london": "伦敦", "manchester": "曼彻斯特", "amsterdam": "阿姆斯特丹",
    "paris": "巴黎", "sydney": "悉尼", "melbourne": "墨尔本",
    "mumbai": "孟买", "bangkok": "曼谷", "kuala lumpur": "吉隆坡",
    "manila": "马尼拉", "hanoi": "河内", "ho chi minh city": "胡志明市",
    "jakarta": "雅加达", "milan": "米兰", "madrid": "马德里",
    "sao paulo": "圣保罗", "mexico city": "墨西哥城", "moscow": "莫斯科",
    "istanbul": "伊斯坦布尔", "dubai": "迪拜", "riyadh": "利雅得",
    "dublin": "都柏林", "stockholm": "斯德哥尔摩", "zurich": "苏黎世",
    "vienna": "维也纳", "auckland": "奥克兰", "hunan": "湖南",
    "guangdong": "广东", "zhejiang": "浙江", "jiangsu": "江苏",
    "fujian": "福建", "sichuan": "四川", "shanghai": "上海",
    "beijing": "北京", "shenzhen": "深圳", "guangzhou": "广州",
    "zhuzhou": "株洲", "changsha": "长沙",
}


def _geo_key(value: str) -> str:
    return re.sub(r"[\s_]+", " ", (value or "").strip().lower())


def to_zh_geo_part(kind: str, value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if kind == "country":
        zh = COUNTRY_ZH.get(text.upper())
        if zh:
            return zh
        return COUNTRY_NAME_ZH.get(_geo_key(text), text)
    return REGION_CITY_ZH.get(_geo_key(text), text)


def format_geo_payload(payload: Dict[str, Any]) -> str:
    """Normalize ipinfo.io / ipapi.co / api.ip.sb JSON to 中文 国家/地区/城市."""
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
        if part and part not in parts:
            parts.append(part)
    return "/".join(parts)


def format_cf_trace(text: str) -> str:
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    loc = to_zh_geo_part("country", fields.get("loc") or "")
    ip = fields.get("ip") or ""
    if loc and ip:
        return f"{loc}/{ip}"
    return loc or ip or ""


def group_from_actual_area(area: str) -> str | None:
    text = (area or "").strip()
    if not text or text == GROUP_UNKNOWN:
        return None
    country = text.split("/", 1)[0].strip() or text
    if SG_PATTERN.search(country):
        return GROUP_SG
    if US_PATTERN.search(country):
        return GROUP_US
    if JP_PATTERN.search(country):
        return GROUP_JP
    return None


def group_from_name(name: str) -> str | None:
    if SG_PATTERN.search(name or ""):
        return GROUP_SG
    if US_PATTERN.search(name or ""):
        return GROUP_US
    if JP_PATTERN.search(name or ""):
        return GROUP_JP
    return None


def classify_region_group(name: str, actual_area: str) -> str:
    """Measured egress wins; the node name is only a fallback when unmeasured."""
    measured = group_from_actual_area(actual_area)
    if measured:
        return measured
    area = (actual_area or "").strip()
    if area and area != GROUP_UNKNOWN:
        return GROUP_OTHER
    return group_from_name(name) or GROUP_UNKNOWN


def advise_groups(region_group: str) -> str:
    """Allocation advice only.  Never applied to any controller here."""
    if region_group == GROUP_US:
        return "GPT/US-FAST/MUSE"
    if region_group in (GROUP_SG, GROUP_JP):
        return "GPT/US-FAST"
    return ""


def tier_of(region_group: str) -> int:
    return REGION_TIERS.get(region_group, 0)


def preferred_order() -> tuple[str, ...]:
    """Candidate preference for GPT/US-FAST: US > SG > JP."""
    return (GROUP_US, GROUP_SG, GROUP_JP)
