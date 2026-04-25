"""
児島ボートレース 高期待値レース LINE 通知
==========================================
当日の各レースについて、boatrace.jp から直前情報・出走表を取得し、
過去4年分のヒストリ（kojima_results.csv）と類似条件で照合。
出現確率・平均払戻から期待値プラスと判定したレースを LINE Bot で push する。

毎回の判定:
  対象日: 当日（date.today()）
  対象レース: R1〜R12 のうち
    - raceresult が空（未確定）
    - beforeinfo が取得済み（直前情報配信済み = 発走間近）
    - notified.json に未記録
  類似条件: レース番号 / 風速バケット / 波高バケット / 潮位 / 1コース級別

通知判定（全て満たす場合のみ通知）:
  - 類似サンプル数 >= MIN_SAMPLES (50件)
  - 上位3連単の出現率 >= MIN_TOP_PROB (8.0%)
  - 上位5買い目のうち最大期待値 >= MIN_EV (100円ベットで期待100円以上)

LINE 認証情報は .env から読み込み:
  LINE_CHANNEL_ACCESS_TOKEN=...
  LINE_USER_ID=...

タスクスケジューラ登録例:
  毎時 0/15/30/45 分（10:00-21:00 のみ）に実行
"""

import csv
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import date, datetime

# 同じディレクトリの kojima_history_fetch をインポート
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kojima_history_fetch import (  # noqa: E402
    fetch_html, parse_racelist, parse_beforeinfo, parse_raceresult,
    estimate_tide_class, JYOJO,
)

ENV_PATH = os.path.join(HERE, ".env")
NOTIFIED_PATH = os.path.join(HERE, "notified.json")
HISTORY_CSV = os.path.join(HERE, "kojima_results.csv")
LOG_PATH = os.path.join(HERE, "alert.log")

MIN_SAMPLES = 50
MIN_TOP_PROB = 8.0
MIN_EV = 100
TOP_N = 5


# ---------- .env ローダ（依存ゼロ） ----------

def load_env():
    if not os.path.exists(ENV_PATH):
        return {}
    env = {}
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ---------- 通知履歴 ----------

def load_notified():
    if not os.path.exists(NOTIFIED_PATH):
        return set()
    try:
        with open(NOTIFIED_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_notified(notified):
    with open(NOTIFIED_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(notified), f, ensure_ascii=False, indent=2)


# ---------- LINE Push ----------

def send_line(env, text):
    token = env.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = env.get("LINE_USER_ID", "")
    if not token or not user_id:
        log("LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が .env に未設定")
        return False
    payload = json.dumps({
        "to": user_id,
        "messages": [{"type": "text", "text": text[:4900]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            log(f"LINE push OK: HTTP {res.status}")
            return True
    except Exception as e:
        log(f"LINE push error: {e}")
        return False


# ---------- ヒストリロード ----------

def load_history():
    if not os.path.exists(HISTORY_CSV):
        log(f"ヒストリCSVなし: {HISTORY_CSV}")
        return []
    rows = []
    with open(HISTORY_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


# ---------- 類似条件マッチ ----------

def wind_bucket(v):
    try:
        v = float(v)
    except Exception:
        return None
    if v <= 2: return "low"
    if v <= 4: return "mid"
    return "high"


def wave_bucket(v):
    try:
        v = float(v)
    except Exception:
        return None
    if v <= 2: return "low"
    if v <= 5: return "mid"
    return "high"


def filter_history(history, rno, wind, wave, tide, grade1):
    wb = wind_bucket(wind)
    vb = wave_bucket(wave)
    out = []
    for r in history:
        try:
            if int(r.get("レース番号", "0")) != rno:
                continue
            if wb is not None and wind_bucket(r.get("風速", "")) != wb:
                continue
            if vb is not None and wave_bucket(r.get("波高", "")) != vb:
                continue
            if tide and r.get("潮位区分", "") != tide:
                continue
            if grade1 and r.get("1艇_級別", "") != grade1:
                continue
            out.append(r)
        except Exception:
            continue
    return out


# ---------- 期待値判定 ----------

def evaluate(matched):
    """trifecta candidate list を返す: [(kaime, prob%, avg_pay, ev, count), ...]"""
    if not matched:
        return []
    counts = Counter()
    pays = {}
    for r in matched:
        p1, p2, p3 = r.get("1着", ""), r.get("2着", ""), r.get("3着", "")
        if not (p1 and p2 and p3):
            continue
        k = f"{p1}-{p2}-{p3}"
        counts[k] += 1
        try:
            pay = int(r.get("3連単払戻") or 0)
            if pay > 0:
                pays.setdefault(k, []).append(pay)
        except Exception:
            pass
    total = len(matched)
    out = []
    for k, c in counts.most_common(TOP_N):
        prob = c / total * 100
        plist = pays.get(k, [])
        avg = sum(plist) / len(plist) if plist else 0
        ev = (c / total) * avg
        out.append((k, prob, avg, ev, c))
    return out


# ---------- ログ ----------

def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------- メイン ----------

def main():
    env = load_env()
    today = date.today()
    today_str = today.strftime("%Y%m%d")
    today_label = today.strftime("%Y-%m-%d")
    tide = estimate_tide_class(today)

    notified = load_notified()
    history = load_history()
    if not history:
        log("ヒストリ未取得のため終了")
        return

    base = "https://www.boatrace.jp/owpc/pc/race"
    log(f"判定開始: {today_label} 潮位={tide} 通知済={len(notified)}")

    new_alerts = 0
    for rno in range(1, 13):
        key = f"{today_label}_R{rno}"
        if key in notified:
            continue

        # 既に確定したレースはスキップ
        result_html = fetch_html(f"{base}/raceresult?rno={rno}&jcd={JYOJO}&hd={today_str}")
        result = parse_raceresult(result_html)
        if result[0]:
            continue

        # 直前情報未配信ならスキップ
        before_html = fetch_html(f"{base}/beforeinfo?rno={rno}&jcd={JYOJO}&hd={today_str}")
        weather, _ = parse_beforeinfo(before_html)
        if not weather.get("wind"):
            continue

        # 出走表（級別取得用）
        racelist_html = fetch_html(f"{base}/racelist?rno={rno}&jcd={JYOJO}&hd={today_str}")
        racers = parse_racelist(racelist_html)
        if not racers:
            continue

        grade1 = racers[0].get("grade", "")
        wind = weather.get("wind", "")
        wave = weather.get("wave", "")

        matched = filter_history(history, rno, wind, wave, tide, grade1)
        if len(matched) < MIN_SAMPLES:
            log(f"R{rno} 類似サンプル不足: {len(matched)}件 (風{wind}m/波{wave}cm/{tide}/1コース{grade1})")
            continue

        candidates = evaluate(matched)
        if not candidates:
            continue

        top_prob = candidates[0][1]
        top_ev = max(c[3] for c in candidates)
        if top_prob < MIN_TOP_PROB or top_ev < MIN_EV:
            log(f"R{rno} 閾値未達: 最大確率{top_prob:.1f}% 最大期待値{top_ev:.0f}")
            continue

        # 通知メッセージ組み立て
        msg_lines = [
            f"児島 R{rno} 期待値プラス検知",
            f"条件: 風{wind}m / 波{wave}cm / {tide} / 1コース{grade1}",
            f"類似サンプル: {len(matched)}件",
            "",
            "買い目候補:",
        ]
        for k, prob, avg, ev, c in candidates:
            msg_lines.append(
                f"  {k} 出現{prob:.1f}% 平均{int(avg):,}円 EV={int(ev)} ({c}回)"
            )
        msg_lines.append("")
        msg_lines.append("https://www.boatrace.jp/owpc/pc/race/raceindex?jcd=16")
        msg = "\n".join(msg_lines)

        if send_line(env, msg):
            notified.add(key)
            save_notified(notified)
            new_alerts += 1
            log(f"通知送信: {key} (top_prob={top_prob:.1f}% top_ev={top_ev:.0f})")

    log(f"判定終了: 新規通知 {new_alerts} 件")


if __name__ == "__main__":
    main()
