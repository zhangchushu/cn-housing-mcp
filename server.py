import os
import re
import json
import time
import html
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple, Set

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ValidationError

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cn-housing")

# ---------------------------
# Config
# ---------------------------
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "25"))
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "1.2"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)

# 搜索返回的候选链接数（再从中挑 limit 个做预览/解析）
SEARCH_CANDIDATE_MULTIPLIER = int(os.getenv("SEARCH_CANDIDATE_MULTIPLIER", "4"))

# 预览抓取时，最多抓多少个候选页面（避免太重）
MAX_PREFETCH = int(os.getenv("MAX_PREFETCH", "12"))

_last_call_ts = 0.0


def _rate_limit():
    global _last_call_ts
    now = time.time()
    wait = RATE_LIMIT_SECONDS - (now - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.time()


def _http_client() -> httpx.Client:
    return httpx.Client(
        timeout=HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"},
    )


# ---------------------------
# Models
# ---------------------------
class SearchRequest(BaseModel):
    city: Optional[str] = Field(None, description="城市，如 北京/上海/成都")
    district: Optional[str] = Field(None, description="区域/区县，如 朝阳/浦东")
    keywords: Optional[str] = Field(None, description="关键词，如 整租/一居/近地铁/学区/小区名")
    purpose: str = Field("rent", description="rent 或 buy")
    price_min: Optional[int] = Field(None, description="最低价格：租房为月租（元），买房为总价（万）")
    price_max: Optional[int] = Field(None, description="最高价格：租房为月租（元），买房为总价（万）")
    rooms: Optional[int] = Field(None, description="几室：1/2/3...")
    page: int = Field(1, description="页码（预留字段）")
    limit: int = Field(10, description="返回条数上限（建议 5-20）")

    # 站点过滤：可选
    site_allow: Optional[List[str]] = Field(
        default=None,
        description="允许的域名列表，如 ['ke.com','lianjia.com']；为空表示不限制",
    )
    site_block: Optional[List[str]] = Field(
        default=None,
        description="屏蔽的域名列表，如 ['58.com']",
    )


class Listing(BaseModel):
    title: Optional[str] = None
    price: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    address: Optional[str] = None
    area_sqm: Optional[float] = None
    rooms: Optional[int] = None
    source: Optional[str] = None
    url: str
    extracted: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    results: List[Listing]
    warnings: List[str] = Field(default_factory=list)


class DetailResponse(BaseModel):
    url: str
    listing: Listing
    raw_snippet: Optional[str] = Field(None, description="截断后的原始结构化片段，便于调试")
    warnings: List[str] = Field(default_factory=list)


# ---------------------------
# Helpers: search (duckduckgo html)
# ---------------------------
def _build_search_query(req: SearchRequest) -> str:
    parts = []
    if req.city:
        parts.append(req.city)
    if req.district:
        parts.append(req.district)
    if req.keywords:
        parts.append(req.keywords)
    parts.append("租房" if req.purpose == "rent" else "买房")
    if req.rooms:
        parts.append(f"{req.rooms}室")
    if req.price_min or req.price_max:
        # 对人更友好，也能影响搜索
        parts.append(f"{req.price_min or ''}-{req.price_max or ''}")
    return " ".join([p for p in parts if p])


def _ddg_search(query: str, limit: int) -> Tuple[List[str], List[str]]:
    """
    使用 DuckDuckGo 的 HTML 页面做轻量级网页搜索（无需 API key）。
    注意：这不是官方 API，可能会变；这里用于“最小可用”。
    """
    warnings = []
    urls: List[str] = []
    _rate_limit()
    with _http_client() as client:
        r = client.get("https://duckduckgo.com/html/", params={"q": query})
        if r.status_code != 200:
            return [], [f"search_http_status={r.status_code}"]

        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a.result__a"):
            href = a.get("href")
            if not href:
                continue
            href = html.unescape(href)
            # DDG 有时会返回重定向链接，这里尽量保留真实链接
            if href.startswith("/l/?kh=") or "duckduckgo.com/l/?" in href:
                # 尝试解析 uddg 参数
                parsed = urllib.parse.urlparse(href if href.startswith("http") else "https://duckduckgo.com" + href)
                qs = urllib.parse.parse_qs(parsed.query)
                uddg = qs.get("uddg", [None])[0]
                if uddg:
                    href = urllib.parse.unquote(uddg)

            if href.startswith("http"):
                urls.append(href)
            if len(urls) >= limit:
                break

    if not urls:
        warnings.append("search_returned_no_urls")
    return urls, warnings


# ---------------------------
# Helpers: filter/normalize urls
# ---------------------------
def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _allowed(url: str, allow: Optional[List[str]], block: Optional[List[str]]) -> bool:
    d = _domain(url)
    if not d:
        return False
    if block:
        for b in block:
            if b and b.lower() in d:
                return False
    if allow:
        ok = False
        for a in allow:
            if a and a.lower() in d:
                ok = True
                break
        if not ok:
            return False
    return True


def _dedupe_urls(urls: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for u in urls:
        # 统一去掉 fragment
        try:
            p = urllib.parse.urlparse(u)
            norm = p._replace(fragment="").geturl()
        except Exception:
            norm = u
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


# ---------------------------
# Helpers: extract listing info
# ---------------------------
_JSONLD_RE = re.compile(r'(<script[^>]+type="application/ld\+json"[^>]*>)(.*?)(</script>)', re.S | re.I)
_NEXTDATA_RE = re.compile(r'(<script[^>]+id="__NEXT_DATA__"[^>]*>)(.*?)(</script>)', re.S | re.I)

def _guess_source(url: str) -> str:
    u = url.lower()
    if "ke.com" in u:
        return "beike/ke.com"
    if "lianjia.com" in u:
        return "lianjia"
    if "anjuke.com" in u:
        return "anjuke"
    if "58.com" in u:
        return "58"
    return "web"


def _detect_blocked(html_text: str, final_url: str) -> List[str]:
    w = []
    txt = html_text.lower()
    if "captcha" in txt or "验证码" in html_text or "人机验证" in html_text or "安全验证" in html_text:
        w.append("possible_captcha_or_bot_challenge")
    if "access denied" in txt or "forbidden" in txt or "拒绝访问" in html_text:
        w.append("possible_access_denied")
    if "login" in txt or "登录" in html_text:
        w.append("possible_login_required")
    if final_url != final_url.strip():
        w.append("url_has_whitespace")
    return w


def _safe_json_loads(s: str) -> Optional[Any]:
    try:
        return json.loads(s)
    except Exception:
        return None


def _extract_from_jsonld(html_text: str) -> Tuple[Dict[str, Any], Optional[str]]:
    m = _JSONLD_RE.search(html_text)
    if not m:
        return {}, None
    payload = m.group(2).strip()
    data = _safe_json_loads(payload)
    if not data:
        return {}, payload[:2000]
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        return data, payload[:2000]
    return {}, payload[:2000]


def _extract_from_next_data(html_text: str) -> Tuple[Dict[str, Any], Optional[str]]:
    m = _NEXTDATA_RE.search(html_text)
    if not m:
        return {}, None
    payload = m.group(2).strip()
    data = _safe_json_loads(payload)
    if isinstance(data, dict):
        return data, payload[:2000]
    return {}, payload[:2000]


def _extract_basic_meta(html_text: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html_text, "lxml")
    title = (soup.title.text.strip() if soup.title and soup.title.text else None)
    meta: Dict[str, Any] = {}
    if title:
        meta["title"] = title
    for name in ["description", "keywords"]:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            meta[name] = tag["content"].strip()
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        meta["og:title"] = og_title["content"].strip()
    og_url = soup.find("meta", attrs={"property": "og:url"})
    if og_url and og_url.get("content"):
        meta["og:url"] = og_url["content"].strip()
    return meta


def _try_extract_price_text(s: str) -> Optional[str]:
    if not s:
        return None
    # 常见：xxxx元/月、xxxx元、xxx万、xxx万元
    m = re.search(r"(\d+(?:\.\d+)?)\s*(元/月|元|万|万元)", s)
    if m:
        return "".join(m.groups())
    return None


def _normalize_listing(url: str, extracted: Dict[str, Any], meta: Dict[str, Any], warnings: List[str]) -> Listing:
    title = None
    price = None
    address = None
    area_sqm = None
    rooms = None
    city = None
    district = None

    # 1) JSON-LD 常见字段
    if isinstance(extracted, dict):
        title = extracted.get("name") or extracted.get("title") or title
        address_obj = extracted.get("address")
        if isinstance(address_obj, dict):
            address = address_obj.get("streetAddress") or address
            city = address_obj.get("addressLocality") or city
            district = address_obj.get("addressRegion") or district

        offers = extracted.get("offers")
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("lowPrice") or price
            if price and offers.get("priceCurrency"):
                price = f"{price} {offers.get('priceCurrency')}"

    # 2) meta fallback
    title = title or meta.get("og:title") or meta.get("title")
    desc = meta.get("description") or ""

    # area/rooms from desc
    m_area = re.search(r"(\d+(?:\.\d+)?)\s*㎡", desc)
    if m_area:
        try:
            area_sqm = float(m_area.group(1))
        except Exception:
            pass
    m_room = re.search(r"(\d+)\s*室", desc)
    if m_room:
        try:
            rooms = int(m_room.group(1))
        except Exception:
            pass

    # price fallback
    price = price or _try_extract_price_text((title or "") + " " + desc)

    return Listing(
        title=title,
        price=price,
        city=city,
        district=district,
        address=address,
        area_sqm=area_sqm,
        rooms=rooms,
        source=_guess_source(url),
        url=url,
        extracted={"structured": extracted, "meta": meta},
        warnings=warnings,
    )


def _fetch_html(url: str) -> Tuple[str, str, List[str]]:
    _rate_limit()
    warnings: List[str] = []
    with _http_client() as client:
        r = client.get(url)
        warnings.extend(_detect_blocked(r.text, str(r.url)))
        if r.status_code >= 400:
            warnings.append(f"http_status={r.status_code}")
        return r.text, str(r.url), warnings


def _extract_structured(html_text: str) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
    """
    解析页面的结构化信息：
    - JSON-LD
    - __NEXT_DATA__
    返回：best_structured_dict, raw_snippet, warnings
    """
    warnings: List[str] = []
    jsonld, jsonld_snip = _extract_from_jsonld(html_text)
    if jsonld:
        return jsonld, jsonld_snip, warnings

    next_data, next_snip = _extract_from_next_data(html_text)
    if next_data:
        # next_data 很大，先返回顶层，后续可以按站点适配进一步下钻
        return next_data, next_snip, warnings

    # 没找到结构化数据
    if jsonld_snip or next_snip:
        # 找到了 script 但 parse 失败
        warnings.append("structured_data_parse_failed")
        return {}, (jsonld_snip or next_snip), warnings

    return {}, None, warnings


# ---------------------------
# MCP Tools
# ---------------------------
@mcp.tool()
def search_listings(req: Dict[str, Any]) -> Dict[str, Any]:
    """
    搜索房源（通用版）。
    最小可用实现：
    1) 构造 query -> DDG HTML 搜索候选链接
    2) 过滤域名 + 去重
    3) 对前 N 个链接做轻量预抓取（meta/jsonld/nextdata）以便输出更可用字段
    """
    try:
        r = SearchRequest(**req)
    except ValidationError as e:
        return {"error": "invalid_request", "details": json.loads(e.json())}

    warnings: List[str] = []
    query = _build_search_query(r)

    # 候选数：limit 的 multiplier
    candidate_limit = max(r.limit, 1) * max(SEARCH_CANDIDATE_MULTIPLIER, 1)

    urls, w = _ddg_search(query, limit=candidate_limit)
    warnings.extend(w)

    # domain filter + dedupe
    urls = [u for u in urls if _allowed(u, r.site_allow, r.site_block)]
    urls = _dedupe_urls(urls)

    if not urls:
        resp = SearchResponse(query=query, results=[], warnings=warnings + ["no_urls_after_filter"]).model_dump()
        return resp

    # 轻量预抓取（最多 MAX_PREFETCH），其余只返回 url
    prefetch_n = min(len(urls), min(MAX_PREFETCH, max(r.limit, 1)))
    results: List[Listing] = []

    # 先抓 prefetch_n 个，填充 title/price/rooms/area 等
    for i, url in enumerate(urls[:prefetch_n]):
        try:
            html_text, final_url, w2 = _fetch_html(url)
            warnings.extend([f"url[{i}]:{x}" for x in w2])
            meta = _extract_basic_meta(html_text)
            structured, raw_snip, w3 = _extract_structured(html_text)
            warnings.extend([f"url[{i}]:{x}" for x in w3])

            listing = _normalize_listing(final_url, structured, meta, w2 + w3)
            # raw_snip 放在 extracted 里，方便上层调试（search 里不单独返回 raw_snippet）
            if raw_snip:
                listing.extracted["raw_snippet"] = raw_snip
            results.append(listing)
        except Exception as ex:
            results.append(
                Listing(
                    url=url,
                    source=_guess_source(url),
                    warnings=[f"prefetch_failed:{type(ex).__name__}:{str(ex)[:120]}"],
                )
            )

    # 补齐剩余链接（不抓页面）
    for url in urls[prefetch_n:]:
        results.append(Listing(url=url, source=_guess_source(url)))

    # 截断到 limit
    results = results[: max(r.limit, 1)]

    resp = SearchResponse(query=query, results=results, warnings=warnings).model_dump()
    return resp


@mcp.tool()
def get_listing_detail(url: str) -> Dict[str, Any]:
    """
    获取房源详情（通用版）：
    1) 抓 HTML
    2) 优先 JSON-LD，其次 __NEXT_DATA__，再 meta / 正则兜底
    3) 返回 Listing + raw_snippet + warnings
    """
    url = (url or "").strip()
    if not url.startswith("http"):
        return {"error": "invalid_url", "details": "url must start with http(s)"}

    warnings: List[str] = []
    try:
        html_text, final_url, w = _fetch_html(url)
        warnings.extend(w)

        meta = _extract_basic_meta(html_text)
        structured, raw_snip, w2 = _extract_structured(html_text)
        warnings.extend(w2)

        listing = _normalize_listing(final_url, structured, meta, warnings.copy())

        # 如果 structured 是 __NEXT_DATA__，往往字段太深，做一个轻量“关键词下钻”（不做站点专用 hardcode）
        # 只从大 dict 里找一些常见键名
        if structured and isinstance(structured, dict) and ("props" in structured or "pageProps" in structured):
            # 尝试在 json 中找 price/title/address/area/rooms 的字符串线索
            s = json.dumps(structured, ensure_ascii=False)
            # 兜底：从巨大 json 里提取一些“看起来像价格/面积/户型”的片段（不保证准确）
            guess_price = _try_extract_price_text(s[:20000])
            if guess_price and not listing.price:
                listing.price = guess_price

        detail = DetailResponse(
            url=final_url,
            listing=listing,
            raw_snippet=raw_snip,
            warnings=warnings,
        ).model_dump()
        return detail

    except Exception as ex:
        return {
            "error": "detail_fetch_failed",
            "url": url,
            "warnings": warnings,
            "details": f"{type(ex).__name__}:{str(ex)[:200]}",
        }


def main():
    # FastMCP 默认以 stdio 方式运行（供 MCP client 调用）
    mcp.run()


if __name__ == "__main__":
    main()
