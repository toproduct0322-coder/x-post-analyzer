#!/usr/bin/env python3
"""
X ポスト分析＆バズ投稿生成ツール v2
ツイートURLを貼るだけでエンゲージメント自動取得
起動: python app.py
ブラウザ: http://localhost:5002
"""
import os
import json
import re
import asyncio
from pathlib import Path
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)

COOKIES_FILE = str(Path(__file__).parent / "x_cookies.json")


# ── cookie store ──────────────────────────────────────────────────────────────

def _load_cookies() -> dict | None:
    try:
        return json.load(open(COOKIES_FILE))
    except Exception:
        pass
    # 本番環境では環境変数からも読み込む
    auth_token = os.getenv("X_AUTH_TOKEN")
    ct0        = os.getenv("X_CT0")
    if auth_token and ct0:
        return {"auth_token": auth_token, "ct0": ct0, "username": os.getenv("X_USERNAME", "")}
    return None

def _save_cookies(auth_token: str, ct0: str, username: str) -> None:
    json.dump({"auth_token": auth_token, "ct0": ct0, "username": username},
              open(COOKIES_FILE, "w"))


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_tweet_id(url: str) -> str | None:
    m = re.search(r'/status/(\d+)', url)
    return m.group(1) if m else None

def _parse_username(account: str) -> str:
    s = account.strip()
    s = re.sub(r'^https?://(www\.)?(x|twitter)\.com/', '', s)
    return s.split('/')[0].split('?')[0].lstrip('@')

def _extract_tweets_from_graphql(obj, out: list) -> None:
    if not isinstance(obj, dict):
        return
    if "legacy" in obj and "full_text" in obj.get("legacy", {}):
        legacy = obj["legacy"]
        views  = obj.get("views", {})
        author = ""
        if "core" in obj and "user_results" in obj.get("core", {}):
            try:
                author = obj["core"]["user_results"]["result"]["legacy"]["screen_name"]
            except (KeyError, TypeError):
                pass
        tweet_id = obj.get("rest_id", legacy.get("id_str", ""))
        out.append({
            "tweet_id": tweet_id,
            "text":     legacy.get("full_text", ""),
            "likes":    legacy.get("favorite_count", 0),
            "retweets": legacy.get("retweet_count", 0),
            "replies":  legacy.get("reply_count", 0),
            "views":    int(views.get("count", 0)) if views.get("count") else 0,
            "author":   author,
        })
        return
    for v in obj.values():
        if isinstance(v, dict):
            _extract_tweets_from_graphql(v, out)
        elif isinstance(v, list):
            for item in v:
                _extract_tweets_from_graphql(item, out)


# ── playwright fetch ──────────────────────────────────────────────────────────

async def _fetch_by_url_async(tweet_url: str) -> dict:
    from playwright.async_api import async_playwright

    creds = _load_cookies()
    if not creds:
        raise RuntimeError("NO_ACCOUNT")

    tweet_id = _parse_tweet_id(tweet_url)
    if not tweet_id:
        raise ValueError("有効なツイートURLを入力してください（例: https://x.com/user/status/123456）")

    url = re.sub(r'https?://(www\.)?(twitter|x)\.com', 'https://x.com', tweet_url)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
        )
        await ctx.add_cookies([
            {"name": "auth_token", "value": creds["auth_token"],
             "domain": ".x.com", "path": "/"},
            {"name": "ct0",        "value": creds["ct0"],
             "domain": ".x.com", "path": "/"},
        ])

        captured = []
        def on_response_sync(resp):
            if "TweetDetail" in resp.url or "TweetResultByRestId" in resp.url:
                captured.append(resp)

        page = await ctx.new_page()
        page.on("response", on_response_sync)

        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(4000)

        # ブラウザを閉じる前にレスポンス本文を読む
        results = []
        for resp in captured:
            try:
                body = await resp.json()
                _extract_tweets_from_graphql(body, results)
            except Exception:
                pass

        await browser.close()

    if not results:
        raise ValueError("ツイートが取得できませんでした。Cookieを確認してください。")

    # URLのツイートIDに一致するものを優先
    main = next((t for t in results if t.get("tweet_id") == tweet_id), results[0])
    return main


async def _fetch_user_posts_async(account: str) -> list:
    from playwright.async_api import async_playwright

    creds = _load_cookies()
    if not creds:
        raise RuntimeError("NO_ACCOUNT")

    username = _parse_username(account)
    if not username:
        raise ValueError("有効なアカウントを入力してください")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
        )
        await ctx.add_cookies([
            {"name": "auth_token", "value": creds["auth_token"],
             "domain": ".x.com", "path": "/"},
            {"name": "ct0",        "value": creds["ct0"],
             "domain": ".x.com", "path": "/"},
        ])

        captured = []
        def on_response_sync(resp):
            if "UserTweets" in resp.url:
                captured.append(resp)

        page = await ctx.new_page()
        page.on("response", on_response_sync)

        await page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(5000)

        posts = []
        for resp in captured:
            try:
                body = await resp.json()
                _extract_tweets_from_graphql(body, posts)
            except Exception:
                pass

        await browser.close()

    # リツイートを除外して最新30件
    originals = [p for p in posts if not p.get("text", "").startswith("RT @")]
    return originals[:30]


# ── routes ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """あなたはXのバズ投稿の専門アナリストです。
投稿を分析してなぜバズったのかを解明し、同じメカニズムを活かした新しい投稿案を作成します。
必ず指定のJSON形式のみで回答してください。マークダウンや説明文は不要です。"""

def build_prompt(post_content, likes, retweets, replies, impressions, theme, account_posts=None):
    parts = []
    if likes:       parts.append(f"いいね: {likes}")
    if retweets:    parts.append(f"RT: {retweets}")
    if replies:     parts.append(f"返信: {replies}")
    if impressions: parts.append(f"インプレッション: {impressions}")
    eng = "　".join(parts) if parts else "不明"

    has_numbers  = any([likes, retweets, replies, impressions])
    has_posts    = bool(account_posts)
    post_count   = 10 if has_posts else 3

    # アカウントの過去ポストセクション
    account_section = ""
    style_task      = ""
    style_json      = ""
    if has_posts:
        texts = "\n".join(f"- {p['text']}" for p in account_posts[:25])
        account_section = f"\n【アカウントの過去ポスト（思考スタイル参考）】\n{texts}\n"
        style_task = """
### 2. 思考スタイル分析
過去ポストからこのアカウントの思考・文体を分析：
- voice: 文体・口調の特徴（話し言葉/書き言葉、丁寧度、テンションなど）
- topics: よく扱うテーマ・関心領域
- patterns: 繰り返し使う表現・構成パターン・特有のフレーズ
- personality: 発信者の個性・キャラクター（1〜2文）
"""
        style_json = """  "style_analysis": {
    "voice": "...", "topics": "...", "patterns": "...", "personality": "..."
  },"""

    # エンゲージメント分析セクション
    eng_task = ""
    eng_json  = ""
    if has_numbers:
        n = 3 if has_posts else 2
        eng_task = f"""
### {n}. エンゲージメント数値分析
- rate: エンゲージメント率の評価（インプレ比でのいいね・RT・返信の割合）
- rt_like_ratio: RT/いいね比率が示すもの（高い＝情報拡散型、低い＝感情共感型）
- reply_pattern: 返信数の多寡が示すもの（多い＝議論・論争性、少ない＝一方的共感）
- virality: 数値から読み取れる拡散パターンの評価（一言で）
- insight: この数値構造が次の投稿設計にどう活かせるか（2文以内）
"""
        eng_json = """  "engagement_analysis": {
    "rate": "...", "rt_like_ratio": "...", "reply_pattern": "...",
    "virality": "...", "insight": "..."
  },"""

    last_n = 2 + (1 if has_posts else 0) + (1 if has_numbers else 0)
    style_instruction = (
        "上記の思考スタイルを完全にコピーした口調・文体・構成で、" if has_posts else "同じバズのメカニズムで"
    )

    posts_example = "\n".join(
        f'    {{"text": "...", "reason": "..."}}' + ("," if i < post_count - 1 else "")
        for i in range(post_count)
    )

    return f"""以下のXポストを分析してください。
{account_section}
【分析対象ポスト】
{post_content}

【エンゲージメント】
{eng}

【発信テーマ・ジャンル】
{theme or "不明"}

## 分析タスク

### 1. コンテンツ分析
- hook: 最初の1文がなぜ引き付けるか
- structure: 情報の展開パターン
- emotion: 感情トリガー（共感/驚き/怒り/笑い/欲求など）
- keywords: 刺さった単語・フレーズ（配列）
- summary: バズった理由を2〜3文で
{style_task}{eng_task}
### {last_n}. 新投稿案（{post_count}案）
{style_instruction}バズのメカニズムを活かした別テーマ・別切り口の投稿を{post_count}案。各案に text と reason を添えて。

## 返却形式（JSON only）
{{
  "analysis": {{
    "hook": "...", "structure": "...", "emotion": "...",
    "keywords": ["..."], "summary": "..."
  }},
{style_json}{eng_json}
  "posts": [
{posts_example}
  ]
}}"""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/account_status")
def account_status():
    creds = _load_cookies()
    if creds:
        return jsonify({"active": 1, "username": creds.get("username", "")})
    return jsonify({"active": 0})


@app.route("/setup_account", methods=["POST"])
def setup_account():
    d          = request.get_json()
    auth_token = (d.get("auth_token") or "").strip()
    ct0        = (d.get("ct0") or "").strip()
    username   = (d.get("username") or "").strip().lstrip("@")
    if not auth_token or not ct0:
        return jsonify({"error": "auth_token と ct0 は必須です"}), 400
    _save_cookies(auth_token, ct0, username)
    return jsonify({"active": 1, "username": username})


@app.route("/fetch_user_posts", methods=["POST"])
def fetch_user_posts():
    d       = request.get_json()
    account = (d.get("account") or "").strip()
    if not account:
        return jsonify({"error": "アカウントを入力してください"}), 400
    try:
        posts = asyncio.run(_fetch_user_posts_async(account))
        return jsonify({"posts": posts, "count": len(posts)})
    except RuntimeError as e:
        if "NO_ACCOUNT" in str(e):
            return jsonify({"error": "no_account"}), 401
        return jsonify({"error": str(e)}), 500
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"取得エラー: {e}"}), 500


@app.route("/fetch_by_url", methods=["POST"])
def fetch_by_url():
    d         = request.get_json()
    tweet_url = (d.get("tweet_url") or "").strip()
    if not tweet_url:
        return jsonify({"error": "ツイートURLを入力してください"}), 400
    try:
        result = asyncio.run(_fetch_by_url_async(tweet_url))
        return jsonify(result)
    except RuntimeError as e:
        if "NO_ACCOUNT" in str(e):
            return jsonify({"error": "no_account"}), 401
        return jsonify({"error": str(e)}), 500
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"取得エラー: {e}"}), 500


@app.route("/analyze", methods=["POST"])
def analyze():
    data         = request.get_json()
    post_content = (data.get("post_content") or "").strip()
    if not post_content:
        return jsonify({"error": "ポスト内容を入力してください"}), 400

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY が設定されていません"}), 500

    client = anthropic.Anthropic(api_key=api_key)
    account_posts = data.get("account_posts") or None
    prompt = build_prompt(
        post_content,
        data.get("likes", ""),
        data.get("retweets", ""),
        data.get("replies", ""),
        data.get("impressions", ""),
        data.get("theme", ""),
        account_posts=account_posts,
    )

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        m   = re.search(r'\{[\s\S]+\}', raw)
        if not m:
            return jsonify({"error": "Claudeから有効なJSONが返りませんでした", "raw": raw}), 500
        return jsonify(json.loads(m.group()))
    except json.JSONDecodeError as e:
        return jsonify({"error": f"JSONパースエラー: {e}"}), 500
    except anthropic.APIError as e:
        return jsonify({"error": f"APIエラー: {e}"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    app.run(debug=False, host="0.0.0.0", port=port)
