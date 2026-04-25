"""
児島ボートレース LINE 通知（2モード）
======================================

【リアルタイムモード】（引数なし）
  当日の各レースについて、直前情報配信済み & 結果未確定のものを対象に
  過去類似条件と照合し、期待値プラスと判定した瞬間に LINE push。
  タスクスケジューラ kojima_alert で 10:00-21:00 / 15分毎 に起動。

【モーニングダイジェストモード】（--morning）
  当日の出走表を取得して、潮位＋レース番号＋1コース級別 で過去類似条件を抽出し、
  期待値プラスのレースを 1通の LINE message にまとめて配信。
  kojima_daily.py 完了直後に呼び出される（毎朝 08:05 頃）。
  通知内容: レース番号・買い目・期待値・回収率・推奨購入額。

LINE 認証情報は .env から読み込み:
  LINE_CHANNEL_ACCESS_TOKEN=...
  LINE_USER_ID=...
"""

import csv
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import date, datetime

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

# 共通閾値
MIN_SAMPLES = 50
MIN_TOP_PROB = 8.0
MIN_EV = 100
TOP_N = 5

# モーニング用閾値
MORNING_MIN_SAMPLES = 30
MORNING_MIN_EV = 100
MORNING_TOP_N = 3      # 1レースあたり提示する買い目数

# 推奨購入額（1点あたり）
BET_AMOUNTS = [
    (200, 100),   # EV>=200 で 1点1,000円
    (150, 500),
    (130, 300),
    (110, 200),
    (100, 100),
]


def env_get():
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


def evaluate(matched, top_n=TOP_N):
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
    for k, c in counts.most_common(top_n):
        prob = c / total * 100
        plist = pays.get(k, [])
        avg = sum(plist) / len(plist) if plist else 0
        ev = (c / total) * avg
        out.append((k, prob, avg, ev, c))
    return out


def bet_per_point(top_ev):
    for thresh, amount in BET_AMOUNTS:
        if top_ev >= thresh:
            return amount
    return 100


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------- リアルタイムモード ----------

def run_realtime():
    env = env_get()
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
    log(f"[realtime] 判定開始: {today_label} 潮位={tide} 通知済={len(notified)}")
    new_alerts = 0

    for rno in range(1, 13):
        key = f"{today_label}_R{rno}"
        if key in notified:
            continue
        result_html = fetch_html(f"{base}/raceresult?rno={rno}&jcd={JYOJO}&hd={today_str}")
        result = parse_raceresult(result_html)
        if result[0]:
            continue
        before_html = fetch_html(f"{base}/beforeinfo?rno={rno}&jcd={JYOJO}&hd={today_str}")
        weather, _ = parse_beforeinfo(before_html)
        if not weather.get("wind"):
            continue
        racelist_html = fetch_html(f"{base}/racelist?rno={rno}&jcd={JYOJO}&hd={today_str}")
        racers = parse_racelist(racelist_html)
        if not racers:
            continue

        grade1 = racers[0].get("grade", "")
        wind = weather.get("wind", "")
        wave = weather.get("wave", "")
        wb = wind_bucket(wind)
        vb = wave_bucket(wave)

        matched = []
        for r in history:
            try:
                if int(r.get("レース番号", "0")) != rno:
                    continue
                if wb and wind_bucket(r.get("風速", "")) != wb:
                    continue
                if vb and wave_bucket(r.get("波高", "")) != vb:
                    continue
                if r.get("潮位区分", "") != tide:
                    continue
                if r.get("1艇_級別", "") != grade1:
                    continue
                matched.append(r)
            except Exception:
                continue

        if len(matched) < MIN_SAMPLES:
            continue
        candidates = evaluate(matched, TOP_N)
        if not candidates:
            continue
        top_prob = candidates[0][1]
        top_ev = max(c[3] for c in candidates)
        if top_prob < MIN_TOP_PROB or top_ev < MIN_EV:
            continue

        bet = bet_per_point(top_ev)
        msg_lines = [
            f"児島 R{rno} 期待値プラス（直前情報）",
            f"条件: 風{wind}m/波{wave}cm/{tide}/1コース{grade1}",
            f"類似サンプル: {len(matched)}件",
            "",
            f"買い目候補（各{bet}円 計{bet * len(candidates):,}円）:",
        ]
        for k, p, ap, e, _c in candidates:
            msg_lines.append(f"  {k} 出現{p:.1f}% 平均{int(ap):,}円 EV={int(e)}")
        msg_lines.append("")
        msg_lines.append("https://www.boatrace.jp/owpc/pc/race/raceindex?jcd=16")
        msg = "\n".join(msg_lines)

        if send_line(env, msg):
            notified.add(key)
            save_notified(notified)
            new_alerts += 1
            log(f"通知送信: {key} top_ev={top_ev:.0f}")

    log(f"[realtime] 終了: 新規通知 {new_alerts} 件")


# ---------- モーニングダイジェストモード ----------

def run_morning():
    env = env_get()
    today = date.today()
    today_str = today.strftime("%Y%m%d")
    today_label = today.strftime("%Y-%m-%d")
    tide = estimate_tide_class(today)

    history = load_history()
    if not history:
        log("[morning] ヒストリ未取得のため終了")
        return

    base = "https://www.boatrace.jp/owpc/pc/race"
    log(f"[morning] 判定開始: {today_label} 潮位={tide}")

    digest = []  # 当日のEVプラスレース
    not_open = 0

    for rno in range(1, 13):
        racelist_html = fetch_html(f"{base}/racelist?rno={rno}&jcd={JYOJO}&hd={today_str}")
        if not racelist_html or "データがありません" in racelist_html:
            not_open += 1
            continue
        racers = parse_racelist(racelist_html)
        if not racers:
            not_open += 1
            continue

        grade1 = racers[0].get("grade", "")
        # モーニング判定: 風波は当朝不明なので条件外、レース番号 + 1コース級別 + 潮位 でマッチ
        matched = []
        for r in history:
            try:
                if int(r.get("レース番号", "0")) != rno:
                    continue
                if r.get("潮位区分", "") != tide:
                    continue
                if r.get("1艇_級別", "") != grade1:
                    continue
                matched.append(r)
            except Exception:
                continue

        if len(matched) < MORNING_MIN_SAMPLES:
            continue
        candidates = evaluate(matched, MORNING_TOP_N)
        if not candidates:
            continue

        top_ev = max(c[3] for c in candidates)
        if top_ev < MORNING_MIN_EV:
            continue

        # 回収率 = 候補買い目を等額購入したときの平均回収率
        # 戦略: 候補を各 {bet} 円ずつ購入
        bet = bet_per_point(top_ev)
        n_points = len(candidates)
        # 1レースあたりの理論期待回収率（出現率×平均払戻 を全候補について合算 / 投資額）
        total_ev_yen = sum(p / 100 * ap for _k, p, ap, _e, _c in candidates) * bet
        cost = bet * n_points
        ret_rate = (total_ev_yen / cost) * 100 if cost else 0

        digest.append({
            "rno": rno,
            "grade1": grade1,
            "samples": len(matched),
            "candidates": candidates,
            "top_ev": top_ev,
            "bet": bet,
            "n_points": n_points,
            "ret_rate": ret_rate,
            "total_cost": cost,
        })

    if not digest:
        log(f"[morning] EVプラスレースなし。未開催/データなし {not_open}件")
        return

    # メッセージ組み立て
    lines = [
        f"児島 {today_label} 朝の予測",
        f"潮位: {tide}",
        f"対象レース: {len(digest)}件",
        "",
    ]
    for d in digest:
        lines.append(f"━━━ R{d['rno']} 1コース{d['grade1']} ━━━")
        lines.append(f"類似サンプル {d['samples']}件 / 想定回収率 {d['ret_rate']:.1f}%")
        lines.append(f"推奨: 各{d['bet']}円 × {d['n_points']}点 = 計{d['total_cost']:,}円")
        for k, p, ap, e, _c in d["candidates"]:
            lines.append(f"  {k}  出現{p:.1f}% 平均{int(ap):,}円 EV={int(e)}")
        lines.append("")
    lines.append("https://www.boatrace.jp/owpc/pc/race/raceindex?jcd=16")
    msg = "\n".join(lines)

    if send_line(env, msg):
        log(f"[morning] 通知送信: {len(digest)}レース")


def main():
    if "--morning" in sys.argv:
        run_morning()
    else:
        run_realtime()


if __name__ == "__main__":
    main()
