# -*- coding: utf-8 -*-
"""
八大网站每小时自动监控脚本
- 抓取8个新闻网站最近更新的文章
- 与飞书表格现有数据去重合并
- 按发布时间从晚到早排序写入
- 通过飞书机器人推送更新通知
"""

import sys
import os
import json
import time
import subprocess
import re
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from collections import Counter

# ======================== 配置 ========================
# 优先从环境变量读取（GitHub Actions 用），本地运行时使用默认值
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aa8f8a1385791bcb")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "7BxFy1zUVWeU0IsWjE6CHgvTz6REXR0C")
SPREADSHEET_TOKEN = os.environ.get("SPREADSHEET_TOKEN", "VAsxsK8e9huZn1tbAj2cnLqOnWh")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://open.feishu.cn/open-apis/bot/v2/hook/a2991681-f0a9-49b8-bde1-9ac90ed9b116")
LOOKBACK_HOURS = 3  # 每次回溯3小时的文章

SHEET_MAPPING = {
    "人民日报":   "00137f",
    "澎湃新闻":   "8XD2jV",
    "扬子晚报":   "AYikF9",
    "工人日报":   "HOmn8T",
    "辽宁日报":   "cwuwV7",
    "中国新闻网": "Wl1caX",
    "中国经济网": "ZeLURT",
    "新京报":     "tgGqWf",
}

EXCLUDE_KEYWORDS = [
    "视频", "直播", "图集", "图片", "H5", "作品",
    "海报", "活动", "专题", "首页", "数字报", "电子报",
]

BASE_URL = "https://open.feishu.cn/open-apis"

# ======================== 工具函数 ========================

def smart_decode(r):
    raw = r.content
    for enc in ['utf-8', 'gb18030', 'gbk', 'gb2312']:
        try:
            t = raw.decode(enc)
            if len(re.findall(r'[\u4e00-\u9fff]', t)) > 10:
                return t
        except Exception:
            pass
    return r.text


def smart_decode_bytes(raw):
    for enc in ['utf-8', 'gb18030', 'gbk', 'gb2312']:
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode('utf-8', errors='replace')


def fix_url(href, base):
    if not href:
        return ""
    if href.startswith('http'):
        return href
    if href.startswith('//'):
        return 'https:' + href
    if href.startswith('./'):
        return base.rstrip('/') + href[1:]
    if href.startswith('/'):
        return base.rstrip('/') + href
    return base.rstrip('/') + '/' + href


def parse_time(text):
    patterns = [
        r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}',
        r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}',
        r'\d{4}-\d{2}-\d{2}',
        r'\d{4}年\d{1,2}月\d{1,2}日\s*(\d{1,2}:\d{2})',
        r'\d{4}年\d{1,2}月\d{1,2}日',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            raw = m.group(0).strip()
            try:
                if '年' in raw:
                    raw = raw.replace('年', '-').replace('月', '-').replace('日', '').strip()
                    parts = re.split(r'[-\s]+', raw)
                    while len(parts) < 4:
                        parts.append('00')
                    raw = "%s-%s-%s %s" % (parts[0], parts[1].zfill(2), parts[2].zfill(2),
                                           parts[3] if len(parts) > 3 and parts[3] else '00:00')
                if len(raw) == 10:
                    raw = raw + " 00:00"
                elif len(raw) == 16:
                    raw = raw + ":00"
                return datetime.strptime(raw, '%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
    return None


def format_time(dt):
    return dt.strftime('%Y-%m-%d %H:%M') if dt else ""


def should_filter_title(title, strict_colon=True):
    if not title or len(title.strip()) < 6:
        return True
    for kw in EXCLUDE_KEYWORDS:
        if kw in title:
            return True
    if " · " in title:
        return True
    if strict_colon and "：" in title:
        return True
    if not strict_colon and "：" in title and len(title) < 15:
        return True
    if title and title[0].isdigit() and len(title) < 20:
        return True
    if re.match(r'\d{1,2}月\d{1,2}日[，,]', title):
        return True
    if title.startswith('近日') or title.startswith('笔者'):
        return True
    return False


# ======================== 飞书 API ========================

class FeishuClient:
    def __init__(self):
        self.token = None
        self.token_expire = 0

    def get_token(self):
        if self.token and time.time() < self.token_expire - 60:
            return self.token
        resp = requests.post(
            f"{BASE_URL}/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json"},
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10
        )
        result = resp.json()
        if result.get("code") == 0:
            self.token = result["tenant_access_token"]
            self.token_expire = time.time() + result.get("expire", 7200)
            return self.token
        raise Exception(f"获取飞书token失败: {result}")

    def read_sheet_titles(self, sheet_id):
        """读取工作表中所有已有标题（A列），用于去重"""
        token = self.get_token()
        url = f"{BASE_URL}/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values/{sheet_id}!A2:A2000"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        result = resp.json()
        values = result.get("data", {}).get("valueRange", {}).get("values", [])
        return set(r[0].strip() for r in values if r and r[0] and r[0].strip())

    def read_sheet_data(self, sheet_id):
        """读取工作表所有数据行"""
        token = self.get_token()
        url = f"{BASE_URL}/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values/{sheet_id}!A2:D2000"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        result = resp.json()
        values = result.get("data", {}).get("valueRange", {}).get("values", [])
        return [r for r in values if r and any(cell.strip() for cell in r if isinstance(cell, str))]

    def write_sheet(self, sheet_id, rows):
        """清空工作表数据区并写入新数据"""
        token = self.get_token()

        # 先读取当前行数用于清空
        url = f"{BASE_URL}/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values/{sheet_id}!A2:A2000"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        result = resp.json()
        vals = result.get("data", {}).get("valueRange", {}).get("values", [])
        row_count = len([r for r in vals if r and r[0]])

        # 清空旧数据
        if row_count > 0:
            clear_range = f"{sheet_id}!A2:D{row_count + 1}"
            requests.put(f"{BASE_URL}/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values", headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }, json={"valueRange": {"range": clear_range, "values": [[""] * 4] * row_count}}, timeout=15)

        if not rows:
            return True

        # 写入新数据
        write_range = f"{sheet_id}!A2:D{1 + len(rows)}"
        resp = requests.put(f"{BASE_URL}/sheets/v2/spreadsheets/{SPREADSHEET_TOKEN}/values", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }, json={"valueRange": {"range": write_range, "values": rows}}, timeout=15)

        return resp.json().get("code") == 0


# ======================== 网站爬虫（精简版，仅用于小时级监控）========================

class PeopleDailyScraper:
    def __init__(self):
        self.name = "人民日报"
        self.home_url = "https://www.people.com.cn/"
        self.base_url = "https://www.people.com.cn"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "zh-CN,zh;q=0.9"})

    def fetch_html(self, url=None):
        try:
            return smart_decode(self.session.get(url or self.home_url, timeout=20, verify=False))
        except Exception:
            return ""

    def scrape(self, cutoff):
        html = self.fetch_html()
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        articles = []
        seen = set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)[:80]
            if not text or len(text) < 6 or should_filter_title(text):
                continue
            full = fix_url(href, self.base_url)
            if (re.search(r'/\d{4}/', full) and '/index.html' not in full and '/cpc/' not in full
                    and not re.search(r'/c100\d{2}(/|$)', full) and (full.endswith('.html') or '.shtml' in full)):
                if text not in seen:
                    seen.add(text)
                    articles.append((text, full))
            if len(articles) >= 150:
                break
        result = []
        for title, url in articles:
            dt = self._get_time(url)
            if dt and dt >= cutoff:
                result.append({"title": title, "url": url, "pub_time": format_time(dt)})
        return result

    def _get_time(self, url):
        try:
            text = self.fetch_html(url)
            if text:
                dt = parse_time(text)
                if dt:
                    return dt
        except Exception:
            pass
        m = re.search(r'/(\d{4})/(\d{2})(\d{2})/', url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0)
            except Exception:
                pass
        return None


class PengpaiScraper:
    CHANNEL_IDS = [25950, 25951, 25952, 25953]

    def __init__(self):
        self.name = "澎湃新闻"
        self.base_url = "https://www.thepaper.cn"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9", "Content-Type": "application/json",
        })

    def scrape(self, cutoff):
        cutoff_ts = int(cutoff.timestamp() * 1000)
        api_url = f"{self.base_url}/contentapi/channel/depth"
        all_items = []
        seen_ids = set()

        for ch_id in self.CHANNEL_IDS:
            start_time = None
            for _ in range(50):
                payload = {"nodeId": ch_id}
                if start_time:
                    payload["startTime"] = start_time
                try:
                    r = self.session.post(api_url, json=payload, timeout=15, verify=False)
                    data = r.json()
                except Exception:
                    break

                inner = data.get("data", {})
                page_info = inner.get("pageInfo", {})
                items = page_info.get("list") or []
                top_content = inner.get("topContent")
                oldest_ts = None

                if top_content and top_content.get("contId"):
                    cid = str(top_content["contId"])
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        all_items.append(top_content)
                        pt = top_content.get("pubTimeLong", 0)
                        if pt and (oldest_ts is None or pt < oldest_ts):
                            oldest_ts = pt

                for item in items:
                    cid = str(item.get("contId", ""))
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        all_items.append(item)
                        pt = item.get("pubTimeLong", 0)
                        if pt and (oldest_ts is None or pt < oldest_ts):
                            oldest_ts = pt

                has_next = page_info.get("hasNext", False)
                next_start = page_info.get("startTime")
                if has_next and next_start:
                    start_time = next_start
                else:
                    break
                if oldest_ts and oldest_ts < cutoff_ts - 86400000:
                    break

        # 补充热榜+编辑精选
        try:
            r = self.session.get("https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar", timeout=15, verify=False)
            sidebar_data = r.json().get("data", {})
            for key in ["hotNews", "editorHandpicked"]:
                for item in sidebar_data.get(key, []):
                    cid = str(item.get("contId", ""))
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        all_items.append(item)
        except Exception:
            pass

        result = []
        seen_titles = set()
        for item in all_items:
            ct = item.get("contType", -1)
            if ct not in (0, 9):
                continue
            name = item.get("name", "").strip()
            if not name or len(name) < 6 or should_filter_title(name, strict_colon=False):
                continue
            pt = item.get("pubTimeLong", 0)
            if not pt:
                continue
            dt = datetime.fromtimestamp(pt / 1000)
            if dt < cutoff:
                continue
            if name in seen_titles:
                continue
            seen_titles.add(name)
            clean = re.sub(r'^推荐\d{2}:\d{2}', '', name).strip()
            clean = re.sub(r'^推荐', '', clean).strip()
            if clean and len(clean) >= 6:
                cid = str(item.get("contId", ""))
                url = f"https://www.thepaper.cn/newsDetail_forward_{cid}"
                result.append({"title": clean, "url": url, "pub_time": format_time(dt)})
        return result


class YangtseScraper:
    def __init__(self):
        self.name = "扬子晚报"
        self.home_url = "https://www.yangtse.com/"
        self.base_url = "https://www.yangtse.com"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "zh-CN,zh;q=0.9"})

    def fetch_html(self, url=None):
        try:
            return smart_decode(self.session.get(url or self.home_url, timeout=20, verify=False))
        except Exception:
            return ""

    def scrape(self, cutoff):
        html = self.fetch_html()
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        links, seen = [], set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)[:80]
            if not text or len(text) < 6 or should_filter_title(text):
                continue
            full = fix_url(href, self.base_url)
            if '/news/' in full and full.endswith('.html') and text not in seen:
                seen.add(text)
                links.append((text, full))
            if len(links) >= 150:
                break
        result = []
        for title, url in links:
            dt = self._get_time(url)
            if dt and dt >= cutoff:
                result.append({"title": title, "url": url, "pub_time": format_time(dt)})
        return result

    def _get_time(self, url):
        try:
            text = self.fetch_html(url)
            if text:
                dt = parse_time(text)
                if dt:
                    return dt
        except Exception:
            pass
        m = re.search(r'/(\d{4})(\d{2})(\d{2})/', url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0)
            except Exception:
                pass
        return None


class WorkerDailyScraper:
    def __init__(self):
        self.name = "工人日报"
        self.home_url = "http://www.workercn.cn/"
        self.base_url = "http://www.workercn.cn"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "zh-CN,zh;q=0.9", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})

    def fetch_html(self, url=None):
        try:
            return smart_decode(self.session.get(url or self.home_url, timeout=20, verify=False))
        except Exception:
            return ""

    def scrape(self, cutoff):
        html = self.fetch_html()
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        links, seen = [], set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)[:80]
            if not text or len(text) < 6 or should_filter_title(text):
                continue
            full = fix_url(href, self.base_url)
            if re.search(r'/c/\d{4}-\d{2}-\d{2}/\d+\.shtml$', full) and text not in seen:
                seen.add(text)
                links.append((text, full))
            if len(links) >= 150:
                break
        result = []
        for title, url in links:
            dt = self._get_time(url)
            if dt and dt >= cutoff:
                result.append({"title": title, "url": url, "pub_time": format_time(dt)})
        return result

    def _get_time(self, url):
        try:
            text = self.fetch_html(url)
            if text:
                dt = parse_time(text)
                if dt:
                    return dt
        except Exception:
            pass
        m = re.search(r'/c/(\d{4})-(\d{2})-(\d{2})/', url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0)
            except Exception:
                pass
        return None


class LiaoningScraper:
    def __init__(self):
        self.name = "辽宁日报"
        self.home_url = "https://www.lnd.com.cn/"
        self.base_url = "https://www.lnd.com.cn"

    def fetch_html(self, url=None):
        try:
            result = subprocess.run(["curl", "-s", "-L", "--max-time", "20", url or self.home_url], capture_output=True, timeout=25)
            return smart_decode_bytes(result.stdout)
        except Exception:
            return ""

    def scrape(self, cutoff):
        html = self.fetch_html()
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        links, seen = [], set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)[:80]
            if not text or len(text) < 6 or should_filter_title(text):
                continue
            full = fix_url(href, self.base_url)
            if re.search(r'lnd\.com\.cn/system/\d{4}/\d{2}/\d{2}/\d+\.shtml$', full) and 'epaper.lnd.com.cn' not in full and text not in seen:
                seen.add(text)
                links.append((text, full))
            if len(links) >= 200:
                break
        result = []
        for title, url in links:
            dt = self._get_time(url)
            if dt and dt >= cutoff:
                result.append({"title": title, "url": url, "pub_time": format_time(dt)})
        return result

    def _get_time(self, url):
        m = re.search(r'/system/(\d{4})/(\d{2})/(\d{2})/', url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0)
            except Exception:
                pass
        return None


class ChinaNewsScraper:
    def __init__(self):
        self.name = "中国新闻网"
        self.home_url = "https://www.chinanews.com.cn/"
        self.base_url = "https://www.chinanews.com.cn"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "zh-CN,zh;q=0.9"})

    def fetch_html(self, url=None):
        try:
            return smart_decode(self.session.get(url or self.home_url, timeout=20, verify=False))
        except Exception:
            return ""

    def scrape(self, cutoff):
        html = self.fetch_html()
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        links, seen = [], set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)[:80]
            if not text or len(text) < 6 or should_filter_title(text):
                continue
            if href.startswith("//"):
                full = "https:" + href
            elif href.startswith("http"):
                full = href
            elif href.startswith("/"):
                full = "https://www.chinanews.com.cn" + href
            else:
                full = "https://www.chinanews.com.cn/" + href
            if re.search(r'/\d{4}/', full) and full.endswith('.shtml') and text not in seen:
                seen.add(text)
                links.append((text, full))
            if len(links) >= 150:
                break
        result = []
        for title, url in links:
            dt = self._get_time(url)
            if dt and dt >= cutoff:
                result.append({"title": title, "url": url, "pub_time": format_time(dt)})
        return result

    def _get_time(self, url):
        try:
            text = self.fetch_html(url)
            if text:
                dt = parse_time(text)
                if dt:
                    return dt
        except Exception:
            pass
        m = re.search(r'/(\d{4})/(\d{2})-(\d{2})/', url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0)
            except Exception:
                pass
        return None


class ChinaEconomicScraper:
    def __init__(self):
        self.name = "中国经济网"
        self.home_url = "https://www.ce.cn/"
        self.base_url = "https://www.ce.cn"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "zh-CN,zh;q=0.9"})

    def fetch_html(self, url=None):
        try:
            return smart_decode(self.session.get(url or self.home_url, timeout=20, verify=False))
        except Exception:
            return ""

    def scrape(self, cutoff):
        html = self.fetch_html()
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        links, seen = [], set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)[:80]
            if not text or len(text) < 6 or should_filter_title(text):
                continue
            full = fix_url(href, self.base_url)
            if re.search(r'/\d{6}/t\d{8}_\d+\.shtml$', full) and text not in seen:
                seen.add(text)
                links.append((text, full))
            if len(links) >= 150:
                break
        result = []
        for title, url in links:
            dt = self._get_time(url)
            if dt and dt >= cutoff:
                result.append({"title": title, "url": url, "pub_time": format_time(dt)})
        return result

    def _get_time(self, url):
        m = re.search(r'/t(\d{4})(\d{2})(\d{2})_\d+\.shtml', url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, 0)
            except Exception:
                pass
        try:
            text = self.fetch_html(url)
            if text:
                dt = parse_time(text)
                if dt:
                    return dt
        except Exception:
            pass
        return None


class BeijingNewsScraper:
    def __init__(self):
        self.name = "新京报"
        self.home_url = "https://www.bjnews.com.cn/"
        self.base_url = "https://www.bjnews.com.cn"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "zh-CN,zh;q=0.9"})

    def fetch_html(self, url=None):
        try:
            return smart_decode(self.session.get(url or self.home_url, timeout=20, verify=False))
        except Exception:
            return ""

    def scrape(self, cutoff):
        html = self.fetch_html()
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        links, seen = [], set()
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)[:80]
            if not text or len(text) < 6 or should_filter_title(text):
                continue
            full = fix_url(href, self.base_url)
            if re.search(r'bjnews\.com\.cn/detail/\d+\.html$', full) and 'service.weibo' not in full and text not in seen:
                seen.add(text)
                links.append((text, full))
            if len(links) >= 150:
                break
        result = []
        for title, url in links:
            dt = self._get_time(url)
            if dt and dt >= cutoff:
                result.append({"title": title, "url": url, "pub_time": format_time(dt)})
        return result

    def _get_time(self, url):
        try:
            text = self.fetch_html(url)
            if text:
                dt = parse_time(text)
                if dt:
                    return dt
        except Exception:
            pass
        return None


ALL_SCRAPERS = [
    PeopleDailyScraper(),
    PengpaiScraper(),
    YangtseScraper(),
    WorkerDailyScraper(),
    LiaoningScraper(),
    ChinaNewsScraper(),
    ChinaEconomicScraper(),
    BeijingNewsScraper(),
]


# ======================== 飞书 Webhook 推送 ========================

def send_webhook(summary_data, now_str):
    """发送飞书机器人卡片消息"""
    total_new = summary_data["total_new"]
    total_total = summary_data["total_total"]

    # 构建各网站内容
    elements = []

    # 标题头
    if total_new == 0:
        header = {"title": {"tag": "plain_text", "content": f"📰 新闻监控 · {now_str}"},
                  "template": "blue"}
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "本次监控未发现新文章。"}})
    else:
        header = {"title": {"tag": "plain_text", "content": f"📰 新闻监控更新 · {now_str}"},
                  "template": "green"}
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"**新增 {total_new} 篇** | 表格共 {total_total} 篇"}})

    card = {"header": header, "elements": elements}

    # 各网站明细（仅显示有新增的）
    for site_name, info in summary_data["sites"].items():
        if info["new_count"] > 0:
            site_text = f"**{site_name}** +{info['new_count']}\n"
            for article in info["new_articles"][:3]:  # 最多显示3篇
                title = article["title"]
                if len(title) > 35:
                    title = title[:35] + "..."
                site_text += f"  • {title}\n"
            if info["new_count"] > 3:
                site_text += f"  ... 还有 {info['new_count'] - 3} 篇\n"
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": site_text}})

    # 底部链接
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看飞书表格"},
            "type": "primary",
            "url": f"https://acndmoz6qo5v.feishu.cn/sheets/{SPREADSHEET_TOKEN}"
        }]
    })

    payload = {"msg_type": "interactive", "card": card}
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            print("  ✅ 飞书推送成功")
        else:
            print(f"  ❌ 飞书推送失败: {result.get('msg')}")
    except Exception as e:
        print(f"  ❌ 飞书推送异常: {e}")


# ======================== 主逻辑 ========================

def process_site(scraper, feishu, cutoff):
    """处理单个网站：抓取 → 去重 → 合并 → 排序 → 写入"""
    print(f"\n{'─'*50}")
    print(f"  {scraper.name}")
    print(f"{'─'*50}")

    sheet_id = SHEET_MAPPING.get(scraper.name)
    if not sheet_id:
        print(f"  ❌ 未找到 sheet_id，跳过")
        return 0, []

    # 1. 抓取新文章
    print(f"  正在抓取...")
    new_articles = scraper.scrape(cutoff)
    print(f"  抓到 {len(new_articles)} 篇候选文章")

    # 2. 读取表格中已有标题
    existing_titles = feishu.read_sheet_titles(sheet_id)
    print(f"  表格已有 {len(existing_titles)} 篇")

    # 3. 过滤出真正的新文章
    truly_new = []
    for a in new_articles:
        if a["title"] not in existing_titles:
            truly_new.append(a)

    if not truly_new:
        print(f"  📭 无新文章")
        # 返回表格当前总量（仅统计行数）
        existing_data = feishu.read_sheet_data(sheet_id)
        return 0, existing_data

    print(f"  🆕 发现 {len(truly_new)} 篇新文章:")
    for a in truly_new:
        print(f"    {a['pub_time']} | {a['title'][:50]}")

    # 4. 读取表格现有数据，合并
    existing_data = feishu.read_sheet_data(sheet_id)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 将现有数据转为 dict 格式
    all_rows = []
    for row in existing_data:
        if len(row) >= 4:
            all_rows.append({"title": row[0], "url": row[1], "pub_time": row[2], "scrape_time": row[3]})
        elif len(row) >= 3:
            all_rows.append({"title": row[0], "url": row[1], "pub_time": row[2], "scrape_time": now_str})
        elif len(row) >= 2:
            all_rows.append({"title": row[0], "url": row[1], "pub_time": "", "scrape_time": now_str})

    # 添加新文章
    for a in truly_new:
        all_rows.append({
            "title": a["title"],
            "url": a["url"],
            "pub_time": a["pub_time"],
            "scrape_time": now_str,
        })

    # 5. 去重 + 按发布时间降序排序
    seen = set()
    unique = []
    for item in all_rows:
        t = item["title"].strip()
        if t and t not in seen:
            seen.add(t)
            unique.append(item)

    unique.sort(key=lambda x: x.get("pub_time", ""), reverse=True)

    # 6. 写入表格
    rows = [[item["title"], item["url"], item["pub_time"], item["scrape_time"]] for item in unique]
    if feishu.write_sheet(sheet_id, rows):
        print(f"  ✅ 写入成功: 共 {len(unique)} 篇（新增 {len(truly_new)} 篇）")
    else:
        print(f"  ❌ 写入失败")

    return len(truly_new), unique


def main():
    requests.packages.urllib3.disable_warnings()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)

    print("=" * 60)
    print(f"  八大网站每小时监控")
    print(f"  运行时间: {now_str}")
    print(f"  回溯范围: {cutoff.strftime('%Y-%m-%d %H:%M')} ~ {now_str}")
    print("=" * 60)

    feishu = FeishuClient()

    summary_data = {
        "total_new": 0,
        "total_total": 0,
        "sites": {},
    }

    for scraper in ALL_SCRAPERS:
        try:
            new_count, all_articles = process_site(scraper, feishu, cutoff)
        except Exception as e:
            print(f"  ❌ {scraper.name} 处理异常: {e}")
            new_count, all_articles = 0, []

        summary_data["sites"][scraper.name] = {
            "new_count": new_count,
            "new_articles": [a for a in all_articles if True],  # 只需总数
            "total": len(all_articles),
        }
        summary_data["total_new"] += new_count
        summary_data["total_total"] += len(all_articles)

    # 补充新文章详情（用于推送）
    # 重新收集新增的文章信息用于 webhook 显示
    for scraper in ALL_SCRAPERS:
        site_info = summary_data["sites"][scraper.name]
        if site_info["new_count"] > 0:
            sheet_id = SHEET_MAPPING.get(scraper.name)
            existing_titles = feishu.read_sheet_titles(sheet_id)
            cutoff_for_display = now - timedelta(hours=LOOKBACK_HOURS + 1)
            scraped = scraper.scrape(cutoff_for_display)
            site_info["new_articles"] = [a for a in scraped if a["title"] not in existing_titles]

    print(f"\n{'='*60}")
    print(f"  汇总: 新增 {summary_data['total_new']} 篇 | 表格共 {summary_data['total_total']} 篇")
    print(f"{'='*60}")

    # 发送飞书推送
    print(f"\n发送飞书推送...")
    send_webhook(summary_data, now_str)


if __name__ == "__main__":
    main()
