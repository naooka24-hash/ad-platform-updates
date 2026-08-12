import os
import re
import json
import time
import html
import hashlib
import smtplib
import urllib.parse
import urllib.request
import urllib.error
import feedparser
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from datetime import datetime, timedelta, timezone

HISTORY_FILE = "update_history.json"
HISTORY_DAYS = 400
MAX_ITEMS_PER_SOURCE = 8
LOOKBACK_HOURS = 168
JST = timezone(timedelta(hours=9))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

NOISE_PATTERNS = [
    r"^(home|top|menu|search|login|sign in|contact|privacy|terms)$",
    r"^(ホーム|トップ|検索|ログイン|お問い合わせ|プライバシー)$",
    r"^\s*$",
    r"^(next|prev|more|続きを読む|もっと見る)$",
]

RELEVANT_HINTS = [
    "update", "new", "launch", "release", "announce", "introduc",
    "deprecat", "sunset", "end of", "discontinu", "retire",
    "policy", "change", "migrat", "beta", "available", "version",
    "アップデート", "更新", "新機能", "リリース", "提供開始", "追加",
    "廃止", "終了", "変更", "改定", "対応", "仕様", "ポリシー",
    "お知らせ", "重要", "移行", "ベータ",
]


def now_jst():
    return datetime.now(JST)


def today_str():
    return now_jst().strftime("%Y-%m-%d")


def clean_text(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_key(platform, title, link):
    base = platform + "|" + clean_text(title).lower()[:80]
    if link:
        try:
            p = urllib.parse.urlparse(link)
            base += "|" + (p.netloc + p.path).rstrip("/").lower()
        except Exception:
            pass
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:16]


def is_noise(title):
    t = clean_text(title).lower()
    if len(t) < 8 or len(t) > 300:
        return True
    for pat in NOISE_PATTERNS:
        if re.match(pat, t, re.IGNORECASE):
            return True
    return False


def looks_relevant(title, body=""):
    text = (clean_text(title) + " " + clean_text(body)).lower()
    for h in RELEVANT_HINTS:
        if h in text:
            return True
    return False


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("[INFO] " + path + " が見つかりません")
        return default
    except Exception as e:
        print("[WARN] " + path + " 読込失敗: " + str(e))
        return default


def load_history():
    data = load_json(HISTORY_FILE, {})
    if not isinstance(data, dict):
        return {"seen": {}, "monthly": {}}
    seen = data.get("seen", {})
    monthly = data.get("monthly", {})

    cutoff = (now_jst() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    kept = {}
    for k, v in seen.items():
        if isinstance(v, dict):
            if str(v.get("date", "")) >= cutoff:
                kept[k] = v
        elif str(v) >= cutoff:
            kept[k] = {"date": str(v)}
    print("[OK] 履歴読込: " + str(len(kept)) + "件")
    return {"seen": kept, "monthly": monthly}


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=1, sort_keys=True)
        print("[OK] 履歴保存: " + str(len(history.get("seen", {}))) + "件")
        return True
    except Exception as e:
        print("[ERROR] 履歴保存失敗: " + str(e))
        return False


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def fetch_rss(url, platform):
    items = []
    raw = http_get(url)
    feed = feedparser.parse(raw)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = clean_text(entry.get("title", ""))
        link = (entry.get("link") or "").strip()
        if not title or is_noise(title):
            continue

        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub:
            try:
                dt = datetime(*pub[:6], tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
            except Exception:
                pass

        body = clean_text(entry.get("summary", ""))[:600]
        items.append({
            "platform": platform,
            "title": title,
            "link": link,
            "body": body,
        })
    return items


def fetch_html(url, platform, selector=None):
    items = []
    raw = http_get(url)
    soup = BeautifulSoup(raw, "lxml")

    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    scope = None
    if selector:
        for sel in [s.strip() for s in selector.split(",")]:
            try:
                found = soup.select_one(sel)
                if found:
                    scope = found
                    break
            except Exception:
                continue
    if scope is None:
        scope = soup.body or soup

    seen_local = set()
    for a in scope.find_all("a", href=True)[:200]:
        title = clean_text(a.get_text())
        if not title or is_noise(title):
            continue

        href = a["href"]
        link = urllib.parse.urljoin(url, href)

        key = title.lower()[:60]
        if key in seen_local:
            continue

        context = ""
        parent = a.find_parent(["li", "article", "div", "tr", "section"])
        if parent:
            context = clean_text(parent.get_text())[:400]

        if not looks_relevant(title, context):
            continue

        seen_local.add(key)
        items.append({
            "platform": platform,
            "title": title,
            "link": link,
            "body": context,
        })

        if len(items) >= MAX_ITEMS_PER_SOURCE:
            break

    return items


def collect_updates(platforms):
    all_items = []
    ok = 0
    ng = 0
    failed = []

    for p in platforms:
        name = p.get("name", "unknown")
        got = 0
        for src in p.get("sources", []):
            stype = src.get("type", "rss")
            url = src.get("url", "")
            if not url:
                continue
            try:
                if stype == "rss":
                    items = fetch_rss(url, name)
                else:
                    items = fetch_html(url, name, src.get("selector"))
                all_items.extend(items)
                got += len(items)
                ok += 1
            except urllib.error.HTTPError as e:
                ng += 1
                failed.append(name)
                print("[NG] " + name + " " + stype + ": HTTP " + str(e.code))
            except Exception as e:
                ng += 1
                failed.append(name)
                print("[NG] " + name + " " + stype + ": " + type(e).__name__)
            time.sleep(1)

        print("[OK] " + name + ": " + str(got) + "件")

    print("[INFO] ソース成功 " + str(ok) + " / 失敗 " + str(ng))
    return all_items, failed

def call_llm(prompt, max_tokens=6000):
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY が未設定です")

    last_error = None
    for model in GROQ_MODELS:
        for attempt in range(3):
            try:
                payload = {
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "あなたは日本のデジタル広告運用に精通した専門家です。必ず日本語で、指定されたJSON形式のみを出力します。",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                }
                res = requests.post(
                    GROQ_URL,
                    headers={
                        "Authorization": "Bearer " + api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=180,
                )

                if res.status_code == 429:
                    wait = 45
                    try:
                        msg = str(res.json().get("error", {}).get("message", ""))
                        m = re.search(r"try again in ([\d.]+)s", msg)
                        if m:
                            wait = int(float(m.group(1))) + 5
                    except Exception:
                        pass
                    wait = min(max(wait, 30), 120)
                    print("[WARN] " + model + " 429。" + str(wait) + "秒待機")
                    time.sleep(wait)
                    continue

                if res.status_code in (400, 413):
                    print("[WARN] " + model + " 入力エラー " + str(res.status_code))
                    break

                res.raise_for_status()
                print("[OK] LLM応答: " + model)
                return res.json()["choices"][0]["message"]["content"]

            except requests.exceptions.Timeout:
                last_error = "Timeout"
                print("[WARN] " + model + " タイムアウト")
                time.sleep(8)
            except Exception as e:
                last_error = str(e)[:120]
                print("[WARN] " + model + ": " + type(e).__name__)
                time.sleep(6)

    raise RuntimeError("LLM失敗: " + str(last_error))


def parse_json_safely(raw):
    if not raw:
        raise ValueError("空の応答")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("JSON解析失敗: " + raw[:200])


def analyze_batch(items, offset):
    """1バッチ分をLLMで分析"""
    blocks = []
    for i, it in enumerate(items):
        blocks.append(
            "ID:" + str(i)
            + "\nP:" + it["platform"]
            + "\nT:" + it["title"][:120]
            + "\nB:" + it["body"][:120]
        )
    indexed = "\n\n".join(blocks)

    prompt = (
        "以下は広告プラットフォームの更新情報の候補です。\n"
        "日本の広告運用担当者にとって意味のある更新だけを選び、日本語で要約してください。\n"
        + """
# 出力形式
以下のJSON形式のみを出力してください。

{
  "updates": [
    {
      "id": 元のID番号（整数）,
      "headline": "日本語の見出し。25〜45文字",
      "summary": "変更内容を2〜3文で具体的に",
      "impact": "運用担当者が取るべき対応を1文で",
      "severity": "critical | important | info",
      "type": "feature | deprecation | policy | product | api"
    }
  ]
}

# 含めるもの
- 管理画面の機能追加・変更・削除
- 入札戦略、ターゲティング、フォーマットの変更
- 広告ポリシー、審査基準の変更
- API/SDKの仕様変更、バージョン廃止
- 新しい広告プロダクト、配信面の発表

# 除外するもの
- 企業PR、受賞、イベント告知、導入事例
- 求人、決算、人事
- ナビゲーションリンク、目次的な項目
- 内容が不明瞭なもの

# severity
critical  : 対応しないと配信停止や不具合が起きる
important : 運用改善や設定見直しが必要
info      : 知っておくとよい

# 該当がなければ updates を空配列で返すこと

# 候補
"""
        + indexed
    )

    raw = call_llm(prompt, max_tokens=4000)
    data = parse_json_safely(raw)
    updates = data.get("updates", [])

    valid = []
    for u in updates:
        if not isinstance(u, dict):
            continue
        idx = u.get("id")
        if isinstance(idx, str) and idx.isdigit():
            idx = int(idx)
        if not isinstance(idx, int) or idx < 0 or idx >= len(items):
            continue

        sev = str(u.get("severity", "info")).lower().strip()
        if sev not in ("critical", "important", "info"):
            sev = "info"

        valid.append({
            "platform": items[idx]["platform"],
            "link": items[idx]["link"],
            "original_title": items[idx]["title"],
            "headline": clean_text(u.get("headline") or items[idx]["title"])[:120],
            "summary": clean_text(u.get("summary") or "")[:400],
            "impact": clean_text(u.get("impact") or "")[:200],
            "severity": sev,
            "type": str(u.get("type", "feature")).lower().strip(),
        })
    return valid


def analyze_updates(items):
    """バッチに分割してLLM分析"""
    if not items:
        return []

    batch_size = 18
    all_updates = []
    total_batches = (len(items) + batch_size - 1) // batch_size

    for bi in range(total_batches):
        start = bi * batch_size
        chunk = items[start:start + batch_size]
        print("[INFO] バッチ " + str(bi + 1) + "/" + str(total_batches)
              + " (" + str(len(chunk)) + "件)")
        try:
            res = analyze_batch(chunk, start)
            all_updates.extend(res)
            print("[OK] バッチ" + str(bi + 1) + ": " + str(len(res)) + "件抽出")
        except Exception as e:
            print("[WARN] バッチ" + str(bi + 1) + "失敗: " + str(e)[:100])

        if bi < total_batches - 1:
            time.sleep(12)

    print("[OK] 有効な更新: " + str(len(all_updates)) + "件")
    return all_updates


def build_monthly_summary(monthly_records):
    """月次サマリーをLLMで生成"""
    if not monthly_records:
        return None

    by_platform = {}
    for r in monthly_records:
        by_platform.setdefault(r["platform"], []).append(r)

    lines = []
    for plat in sorted(by_platform.keys()):
        lines.append("[" + plat + "]")
        for r in by_platform[plat]:
            lines.append("- (" + r.get("severity", "info") + ") " + r.get("headline", ""))
        lines.append("")
    body = "\n".join(lines)

    month = now_jst().strftime("%Y年%m月")

    prompt = (
        month + "に各広告プラットフォームで発表された更新の一覧です。\n"
        "日本の広告運用担当者向けに、今月の総括を作成してください。\n"
        + """
# 出力形式
以下のJSON形式のみを出力してください。

{
  "overview": "今月全体の傾向を3〜4文で。共通するトレンドや注目点",
  "key_actions": [
    "対応が必要な事項を3〜5件。期限があれば明記"
  ],
  "trends": [
    "業界横断で見られた動きを2〜4件"
  ],
  "next_month": "来月に向けて注視すべき点を1〜2文"
}

# 記述ルール
- 具体的なプラットフォーム名と機能名を含める
- 「様々な更新がありました」等の抽象的な表現は禁止
- key_actions は実務で何をすべきかを明記

# 今月の更新一覧
"""
        + body
    )

    try:
        raw = call_llm(prompt, max_tokens=3000)
        return parse_json_safely(raw)
    except Exception as e:
        print("[WARN] 月次サマリー生成失敗: " + str(e))
        return None


def is_month_end():
    """今日が今月最後の平日かを判定"""
    today = now_jst()
    tomorrow = today + timedelta(days=1)
    if tomorrow.month != today.month:
        return True

    d = today
    for _ in range(7):
        d = d + timedelta(days=1)
        if d.month != today.month:
            return True
        if d.weekday() < 5:
            return False
    return False


def load_members():
    data = load_json("members.json", {})
    members = data.get("members", [])
    valid = []
    for m in members:
        if m.get("email"):
            valid.append(m)
    print("[OK] メンバー: " + str(len(valid)) + "名")
    return valid


SEVERITY_ORDER = {"critical": 0, "important": 1, "info": 2}

SEVERITY_STYLE = {
    "critical": {
        "label": "要対応",
        "bg": "#fef2f2",
        "border": "#dc2626",
        "text": "#991b1b",
        "badge_bg": "#dc2626",
    },
    "important": {
        "label": "要確認",
        "bg": "#fffbeb",
        "border": "#d97706",
        "text": "#92400e",
        "badge_bg": "#d97706",
    },
    "info": {
        "label": "参考",
        "bg": "#f0f9ff",
        "border": "#0284c7",
        "text": "#075985",
        "badge_bg": "#0284c7",
    },
}


def group_by_platform(updates):
    groups = {}
    for u in updates:
        groups.setdefault(u["platform"], []).append(u)
    for plat in groups:
        groups[plat].sort(key=lambda x: SEVERITY_ORDER.get(x["severity"], 3))
    return groups


def build_email_html(updates, monthly=None, failed_sources=None):
    today = now_jst().strftime("%Y.%m.%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][now_jst().weekday()]

    crit = len([u for u in updates if u["severity"] == "critical"])
    imp = len([u for u in updates if u["severity"] == "important"])
    info = len([u for u in updates if u["severity"] == "info"])

    p = []
    p.append('<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">')
    p.append('<meta name="viewport" content="width=device-width,initial-scale=1"></head>')
    p.append('<body style="margin:0;padding:0;background-color:#f1f5f9;">')
    p.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             'style="background-color:#f1f5f9;padding:28px 12px;">')
    p.append('<tr><td align="center">')
    p.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             'style="max-width:660px;font-family:-apple-system,BlinkMacSystemFont,'
             '\'Segoe UI\',\'Hiragino Sans\',\'Yu Gothic UI\',sans-serif;">')

    # ヘッダー
    p.append('<tr><td style="background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);'
             'border-radius:14px;padding:32px 30px;">')
    p.append('<div style="color:#7dd3fc;font-size:10px;font-weight:700;'
             'letter-spacing:2.5px;margin-bottom:9px;">PLATFORM UPDATES</div>')
    p.append('<div style="color:#ffffff;font-size:25px;font-weight:800;'
             'letter-spacing:-0.4px;">広告プラットフォーム更新情報</div>')
    p.append('<div style="height:1px;background:rgba(255,255,255,0.14);'
             'margin:17px 0 13px 0;"></div>')
    p.append('<div style="color:#94b8d8;font-size:12px;">'
             + today + ' (' + weekday + ')　|　新着 ' + str(len(updates)) + '件</div>')
    p.append('</td></tr>')

    p.append('<tr><td style="height:16px;"></td></tr>')

    # サマリーバー
    p.append('<tr><td style="background-color:#ffffff;border-radius:12px;padding:18px 22px;">')
    p.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>')
    for key, count in [("critical", crit), ("important", imp), ("info", info)]:
        st = SEVERITY_STYLE[key]
        p.append('<td align="center" style="padding:4px;">')
        p.append('<div style="color:' + st["text"] + ';font-size:22px;font-weight:800;">'
                 + str(count) + '</div>')
        p.append('<div style="color:#94a3b8;font-size:11px;margin-top:3px;">'
                 + st["label"] + '</div>')
        p.append('</td>')
    p.append('</tr></table>')
    p.append('</td></tr>')

    p.append('<tr><td style="height:16px;"></td></tr>')

    # プラットフォーム別
    groups = group_by_platform(updates)
    plat_order = sorted(
        groups.keys(),
        key=lambda k: min(SEVERITY_ORDER.get(u["severity"], 3) for u in groups[k])
    )

    for plat in plat_order:
        rows = groups[plat]

        p.append('<tr><td style="padding:6px 4px 10px 4px;">')
        p.append('<div style="color:#0f172a;font-size:15px;font-weight:800;'
                 'letter-spacing:0.2px;">' + html.escape(plat)
                 + ' <span style="color:#94a3b8;font-size:12px;font-weight:600;">('
                 + str(len(rows)) + ')</span></div>')
        p.append('</td></tr>')

        for u in rows:
            st = SEVERITY_STYLE.get(u["severity"], SEVERITY_STYLE["info"])

            p.append('<tr><td style="background-color:#ffffff;border-radius:12px;'
                     'padding:0;border-left:4px solid ' + st["border"] + ';">')
            p.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">')
            p.append('<tr><td style="padding:20px 24px;">')

            p.append('<div style="margin-bottom:11px;">')
            p.append('<span style="display:inline-block;background-color:' + st["badge_bg"]
                     + ';color:#ffffff;font-size:10px;font-weight:700;padding:3px 10px;'
                     'border-radius:4px;letter-spacing:0.5px;">' + st["label"] + '</span>')
            p.append('</div>')

            p.append('<div style="font-size:16px;font-weight:700;color:#0f172a;'
                     'line-height:1.55;margin-bottom:11px;">'
                     + html.escape(u["headline"]) + '</div>')

            if u.get("summary"):
                p.append('<div style="font-size:13.5px;color:#475569;line-height:1.85;'
                         'margin-bottom:13px;">' + html.escape(u["summary"]) + '</div>')

            if u.get("impact"):
                p.append('<div style="background-color:' + st["bg"] + ';border-radius:8px;'
                         'padding:11px 14px;margin-bottom:13px;">')
                p.append('<div style="font-size:10px;color:' + st["text"] + ';'
                         'font-weight:700;letter-spacing:1px;margin-bottom:4px;">ACTION</div>')
                p.append('<div style="font-size:12.5px;color:#334155;line-height:1.7;">'
                         + html.escape(u["impact"]) + '</div>')
                p.append('</div>')

            if u.get("link"):
                p.append('<a href="' + html.escape(u["link"], quote=True)
                         + '" style="display:inline-block;color:#2563eb;text-decoration:none;'
                         'font-size:12.5px;font-weight:700;">元記事を確認する &rarr;</a>')

            p.append('</td></tr></table>')
            p.append('</td></tr>')
            p.append('<tr><td style="height:10px;"></td></tr>')

        p.append('<tr><td style="height:8px;"></td></tr>')

    # 月次サマリー
    if monthly:
        p.append('<tr><td style="height:14px;"></td></tr>')
        p.append('<tr><td style="background:linear-gradient(135deg,#1e293b 0%,#334155 100%);'
                 'border-radius:14px;padding:28px 26px;">')
        p.append('<div style="color:#fbbf24;font-size:10px;font-weight:700;'
                 'letter-spacing:2px;margin-bottom:8px;">MONTHLY SUMMARY</div>')
        p.append('<div style="color:#ffffff;font-size:20px;font-weight:800;'
                 'margin-bottom:18px;">' + now_jst().strftime("%Y年%m月") + ' 総括</div>')

        if monthly.get("overview"):
            p.append('<div style="color:#cbd5e1;font-size:13px;line-height:1.9;'
                     'margin-bottom:20px;">' + html.escape(str(monthly["overview"])) + '</div>')

        acts = monthly.get("key_actions", [])
        if isinstance(acts, list) and acts:
            p.append('<div style="background-color:rgba(255,255,255,0.07);border-radius:9px;'
                     'padding:16px 18px;margin-bottom:14px;">')
            p.append('<div style="color:#fbbf24;font-size:11px;font-weight:700;'
                     'margin-bottom:10px;letter-spacing:0.5px;">対応が必要な事項</div>')
            for a in acts[:6]:
                p.append('<div style="color:#e2e8f0;font-size:12.5px;line-height:1.8;'
                         'margin-bottom:7px;">・' + html.escape(str(a)) + '</div>')
            p.append('</div>')

        trends = monthly.get("trends", [])
        if isinstance(trends, list) and trends:
            p.append('<div style="background-color:rgba(255,255,255,0.07);border-radius:9px;'
                     'padding:16px 18px;margin-bottom:14px;">')
            p.append('<div style="color:#7dd3fc;font-size:11px;font-weight:700;'
                     'margin-bottom:10px;letter-spacing:0.5px;">今月のトレンド</div>')
            for t in trends[:5]:
                p.append('<div style="color:#e2e8f0;font-size:12.5px;line-height:1.8;'
                         'margin-bottom:7px;">・' + html.escape(str(t)) + '</div>')
            p.append('</div>')

        if monthly.get("next_month"):
            p.append('<div style="color:#94a3b8;font-size:12px;line-height:1.8;'
                     'padding-top:6px;">来月の注目: '
                     + html.escape(str(monthly["next_month"])) + '</div>')

        p.append('</td></tr>')

    # 取得失敗の通知
    if failed_sources:
        p.append('<tr><td style="height:14px;"></td></tr>')
        p.append('<tr><td style="background-color:#ffffff;border-radius:12px;'
                 'padding:16px 22px;">')
        p.append('<div style="color:#94a3b8;font-size:11px;line-height:1.8;">'
                 '取得できなかったソース: ' + html.escape(", ".join(failed_sources[:12])))
        if len(failed_sources) > 12:
            p.append(' ほか' + str(len(failed_sources) - 12) + '件')
        p.append('</div>')
        p.append('</td></tr>')

    # フッター
    p.append('<tr><td style="padding:22px 20px 10px 20px;text-align:center;">')
    p.append('<div style="height:1px;background-color:#e2e8f0;margin-bottom:16px;"></div>')
    p.append('<div style="color:#94a3b8;font-size:10.5px;line-height:1.9;">')
    p.append('配信済みの更新は再送されません<br>')
    p.append('<span style="color:#cbd5e1;">Automated by GitHub Actions</span>')
    p.append('</div>')
    p.append('</td></tr>')

    p.append('</table></td></tr></table></body></html>')
    return "".join(p)


def build_email_text(updates, monthly=None):
    lines = []
    lines.append("広告プラットフォーム更新情報 " + now_jst().strftime("%Y/%m/%d"))
    lines.append("新着 " + str(len(updates)) + "件")
    lines.append("")

    groups = group_by_platform(updates)
    for plat in sorted(groups.keys()):
        lines.append("■ " + plat)
        for u in groups[plat]:
            st = SEVERITY_STYLE.get(u["severity"], SEVERITY_STYLE["info"])
            lines.append("[" + st["label"] + "] " + u["headline"])
            if u.get("summary"):
                lines.append(u["summary"])
            if u.get("impact"):
                lines.append("対応: " + u["impact"])
            if u.get("link"):
                lines.append(u["link"])
            lines.append("")
        lines.append("")

    if monthly:
        lines.append("=" * 40)
        lines.append(now_jst().strftime("%Y年%m月") + " 総括")
        lines.append("=" * 40)
        if monthly.get("overview"):
            lines.append(str(monthly["overview"]))
            lines.append("")
        acts = monthly.get("key_actions", [])
        if isinstance(acts, list) and acts:
            lines.append("【対応が必要な事項】")
            for a in acts[:6]:
                lines.append("・" + str(a))
            lines.append("")
        trends = monthly.get("trends", [])
        if isinstance(trends, list) and trends:
            lines.append("【今月のトレンド】")
            for t in trends[:5]:
                lines.append("・" + str(t))
            lines.append("")
        if monthly.get("next_month"):
            lines.append("【来月の注目】")
            lines.append(str(monthly["next_month"]))

    return "\n".join(lines)


def send_mail(to_email, html_body, text_body, subject):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("Ad Platform Updates", "utf-8")), gmail_user))
    msg["To"] = to_email

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(gmail_user, gmail_pass)
        server.send_message(msg)

    print("[OK] 送信完了: " + to_email)


def main():
    members = load_members()
    if not members:
        print("[ERROR] 配信先が設定されていません")
        return

    cfg = load_json("platforms.json", {})
    platforms = cfg.get("platforms", [])
    if not platforms:
        print("[ERROR] platforms.json が読み込めません")
        return
    print("[OK] 監視対象: " + str(len(platforms)) + "プラットフォーム")

    history = load_history()
    seen = history.get("seen", {})
    monthly_store = history.get("monthly", {})

    print("")
    print("===== 情報収集 =====")
    raw_items, failed = collect_updates(platforms)
    print("[INFO] 候補: " + str(len(raw_items)) + "件")

    new_items = []
    for it in raw_items:
        key = make_key(it["platform"], it["title"], it["link"])
        if key in seen:
            continue
        it["_key"] = key
        new_items.append(it)

    print("[INFO] 未配信: " + str(len(new_items)) + "件")

    if len(new_items) > 70:
        new_items = new_items[:70]
        print("[INFO] 70件に制限")

    month_key = now_jst().strftime("%Y-%m")
    month_end = is_month_end()
    monthly_data = None

    if not new_items:
        print("[INFO] 新着なし")
        if not month_end:
            print("[SKIP] 配信をスキップします")
            return
        print("[INFO] 月末のためサマリーのみ配信します")

    updates = []
    if new_items:
        print("")
        print("===== LLM分析 =====")
        try:
            updates = analyze_updates(new_items)
        except Exception as e:
            print("[ERROR] 分析失敗: " + str(e))
            for it in new_items:
                seen[it["_key"]] = {"date": today_str()}
            history["seen"] = seen
            save_history(history)
            return

        for it in new_items:
            seen[it["_key"]] = {"date": today_str()}

        if updates:
            if month_key not in monthly_store:
                monthly_store[month_key] = []
            for u in updates:
                monthly_store[month_key].append({
                    "platform": u["platform"],
                    "headline": u["headline"],
                    "severity": u["severity"],
                    "date": today_str(),
                })

    if month_end:
        print("")
        print("===== 月次サマリー生成 =====")
        records = monthly_store.get(month_key, [])
        print("[INFO] 今月の更新: " + str(len(records)) + "件")
        if records:
            time.sleep(8)
            monthly_data = build_monthly_summary(records)

    if not updates and not monthly_data:
        print("[SKIP] 配信対象がありません")
        history["seen"] = seen
        history["monthly"] = monthly_store
        save_history(history)
        return

    print("")
    print("===== 配信 =====")

    subject = "広告プラットフォーム更新 " + now_jst().strftime("%m/%d")
    if monthly_data:
        subject = "【月次総括】" + subject
    crit = len([u for u in updates if u["severity"] == "critical"])
    if crit > 0:
        subject = "【要対応" + str(crit) + "件】" + subject

    html_body = build_email_html(updates, monthly_data, failed)
    text_body = build_email_text(updates, monthly_data)

    sent = 0
    for m in members:
        try:
            send_mail(m["email"], html_body, text_body, subject)
            sent += 1
        except Exception as e:
            print("[ERROR] 送信失敗 " + m["email"] + ": " + str(e))
        time.sleep(2)

    history["seen"] = seen
    history["monthly"] = monthly_store
    save_history(history)

    print("")
    print("[DONE] 配信 " + str(sent) + "/" + str(len(members)) + "名")


if __name__ == "__main__":
    main()
