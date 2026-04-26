"""
児島ボートレース 期待値プラスレース LINE 通知
=================================================
全13項目（レース番号・曜日・潮位・1コース級別・風速・風向・波高・モーター2連率・
1コース勝率・展示タイム・展示ST・チルト・展示進入・F/L）を使った
マルチ条件パターン探索を行い、回収率100%超のレースを LINE Bot で push 通知する。

【マッチング戦略】
階層的グリーディマッチング:
  Tier 1（必須）: レース番号 / 1コース級別 / 潮位
  Tier 2（気象）: 風速バケット / 波高バケット
  Tier 3（1コース実力）: モーター2連率 / 全国勝率
  Tier 4（直前情報）: 展示タイム / 展示ST / チルト / 展示進入
  Tier 5（その他）: 曜日 / 風向 / F/L

各 Tier の各条件について「適用後もサンプル数 >= MIN_SAMPLES」なら
フィルタを採用、不足するならスキップ。最終的に最も具体的でサンプル充分な
パターンが選ばれる。

【判定】
  - 類似サンプル数 >= MIN_SAMPLES (50件)
  - 上位3買い目の合成回収率 >= MIN_RET_RATE (100%)
  - 最大期待値 >= MIN_EV (100)

【モード】
  引数なし     : リアルタイムモード（直前情報配信済みかつ未確定のレース）
  --morning    : モーニングダイジェスト（出走表のみ・気象/直前情報なし）

【.env 設定】
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

# 判定閾値
MIN_SAMPLES = 50
MIN_RET_RATE = 100.0   # 合成回収率（%）。100超で期待値プラス
MIN_EV = 100           # 単点最大期待値（100円ベットあたり）
TOP_N = 3              # 提示する買い目数

# モーニング判定はサンプル数の制約を緩める（条件項目が少ないので）
MORNING_MIN_SAMPLES = 30

# 推奨購入額（1点あたり、回収率に応じてスライド）
BET_BY_RET = [
    (140, 1000),
    (125, 500),
    (115, 300),
    (105, 200),
    (100, 100),
]

WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


# ---------- .env / 通知履歴 / LINE ----------

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


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


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


# ---------- バケッティング ----------

def to_float(s):
    try:
        return float(s)
    except Exception:
        return None


def bucket_wind(s):
    v = to_float(s)
    if v is None: return None
    if v <= 2: return "低(0-2m)"
    if v <= 4: return "中(3-4m)"
    return "高(5m+)"


def bucket_wave(s):
    v = to_float(s)
    if v is None: return None
    if v <= 2: return "低(0-2cm)"
    if v <= 5: return "中(3-5cm)"
    return "高(6cm+)"


def bucket_motor(s):
    v = to_float(s)
    if v is None: return None
    if v < 30: return "弱(<30%)"
    if v < 40: return "中(30-40%)"
    if v < 50: return "強(40-50%)"
    return "超強(50%+)"


def bucket_ex_time(s):
    v = to_float(s)
    if v is None: return None
    if v < 6.7: return "速(<6.70)"
    if v < 6.85: return "中(6.70-6.85)"
    if v < 7.0: return "並(6.85-7.00)"
    return "鈍(7.00+)"


def bucket_ex_st(s):
    if not s: return None
    if s.startswith("F"): return "F"
    v = to_float(s)
    if v is None: return None
    if v < 0.10: return "速(<.10)"
    if v < 0.17: return "普(.10-.17)"
    return "鈍(.17+)"


def bucket_tilt(s):
    v = to_float(s)
    if v is None: return None
    if v <= -0.5: return "マイナス"
    if v == 0.0: return "0度"
    if v <= 0.5: return "0.5度"
    return "1度+"


def bucket_zen_win(s):
    v = to_float(s)
    if v is None: return None
    if v < 5.0: return "弱(<5.0)"
    if v < 6.0: return "並(5.0-6.0)"
    if v < 7.0: return "強(6.0-7.0)"
    return "超強(7.0+)"


def bucket_fl(f, l):
    try:
        f = int(f or 0)
        l = int(l or 0)
    except Exception:
        return None
    if f == 0 and l == 0: return "クリーン"
    if f >= 1: return f"F{f}"
    return f"L{l}"


# ---------- 条件マッチング ----------

def cond_matches(row, cname, cvalue):
    if cvalue is None or cvalue == "":
        return True
    try:
        if cname == "rno":
            return int(row.get("レース番号", "0")) == int(cvalue)
        if cname == "weekday":
            return row.get("曜日", "") == cvalue
        if cname == "tide":
            return row.get("潮位区分", "") == cvalue
        if cname == "grade1":
            return row.get("1艇_級別", "") == cvalue
        if cname == "wind_bucket":
            return bucket_wind(row.get("風速", "")) == cvalue
        if cname == "wave_bucket":
            return bucket_wave(row.get("波高", "")) == cvalue
        if cname == "wind_dir":
            return row.get("風向", "") == cvalue
        if cname == "motor1_bucket":
            return bucket_motor(row.get("1艇_モーター2連率", "")) == cvalue
        if cname == "ex_time1_bucket":
            return bucket_ex_time(row.get("1艇_展示タイム", "")) == cvalue
        if cname == "ex_st1_bucket":
            return bucket_ex_st(row.get("1艇_展示ST", "")) == cvalue
        if cname == "tilt1_bucket":
            return bucket_tilt(row.get("1艇_チルト", "")) == cvalue
        if cname == "ex_iri1":
            return row.get("1艇_展示進入", "") == str(cvalue)
        if cname == "zenkoku_win1_bucket":
            return bucket_zen_win(row.get("1艇_全国勝率", "")) == cvalue
        if cname == "fl1":
            return bucket_fl(row.get("1艇_F数", "0"), row.get("1艇_L数", "0")) == cvalue
    except Exception:
        return False
    return False


def filter_by(history, cname, cvalue):
    if cvalue is None or cvalue == "":
        return history
    return [r for r in history if cond_matches(r, cname, cvalue)]


# ---------- 階層グリーディ条件選択 ----------

def build_tiers(c, mode):
    """conditions dict から Tier 別の条件リストを構築。
    mode='realtime' は全条件、'morning' は気象/直前情報を除外。"""
    tier1 = [
        ("rno", c.get("rno")),
        ("grade1", c.get("grade1")),
        ("tide", c.get("tide")),
    ]
    tier3 = [
        ("motor1_bucket", bucket_motor(c.get("motor1", ""))),
        ("zenkoku_win1_bucket", bucket_zen_win(c.get("zenkoku_win1", ""))),
    ]
    tier5 = [
        ("weekday", c.get("weekday")),
        ("fl1", bucket_fl(c.get("f_num1", "0"), c.get("l_num1", "0"))),
    ]
    if mode == "morning":
        return [tier1, tier3, tier5]
    tier2 = [
        ("wind_bucket", bucket_wind(c.get("wind", ""))),
        ("wave_bucket", bucket_wave(c.get("wave", ""))),
    ]
    tier4 = [
        ("ex_time1_bucket", bucket_ex_time(c.get("ex_time1", ""))),
        ("ex_st1_bucket", bucket_ex_st(c.get("ex_st1", ""))),
        ("tilt1_bucket", bucket_tilt(c.get("tilt1", ""))),
        ("ex_iri1", c.get("ex_iri1")),
    ]
    tier5_full = tier5 + [("wind_dir", c.get("wind_dir"))]
    return [tier1, tier2, tier3, tier4, tier5_full]


def find_pattern(history, conditions, mode, min_samples):
    """グリーディに条件を適用し、最も具体的かつサンプル数充分な
    フィルタとそこでの3連単候補を返す。"""
    matched = list(history)
    applied = []
    tiers = build_tiers(conditions, mode)
    for tier in tiers:
        for cname, cvalue in tier:
            if cvalue is None or cvalue == "":
                continue
            test = filter_by(matched, cname, cvalue)
            if len(test) >= min_samples:
                matched = test
                applied.append((cname, cvalue))
            # else: skip — too restrictive
    if len(matched) < min_samples:
        return None
    candidates = evaluate(matched, TOP_N)
    if not candidates:
        return None
    return matched, applied, candidates


# ---------- 期待値計算 ----------

def evaluate(matched, top_n):
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


def compute_return_rate(candidates):
    if not candidates:
        return 0.0
    total = sum(prob * avg for _k, prob, avg, _ev, _c in candidates)
    return total / len(candidates)


def bet_per_point(ret_rate):
    for thresh, amount in BET_BY_RET:
        if ret_rate >= thresh:
            return amount
    return 100


# ---------- 条件ラベル ----------

def cond_label(cname, cvalue):
    labels = {
        "rno": ("レース", lambda v: f"R{v}"),
        "weekday": ("曜日", lambda v: v),
        "tide": ("潮位", lambda v: v),
        "grade1": ("1コース級別", lambda v: v),
        "wind_bucket": ("風速", lambda v: v),
        "wave_bucket": ("波高", lambda v: v),
        "wind_dir": ("風向", lambda v: v),
        "motor1_bucket": ("1コースモーター", lambda v: v),
        "ex_time1_bucket": ("1コース展示タイム", lambda v: v),
        "ex_st1_bucket": ("1コース展示ST", lambda v: v),
        "tilt1_bucket": ("1コースチルト", lambda v: v),
        "ex_iri1": ("1コース展示進入", lambda v: v),
        "zenkoku_win1_bucket": ("1コース勝率", lambda v: v),
        "fl1": ("1コースF/L", lambda v: v),
    }
    label, fmt = labels.get(cname, (cname, str))
    return f"{label}: {fmt(cvalue)}"


# ---------- 当日情報の収集 ----------

def gather_conditions(rno, mode, today, today_str):
    """rno のレースについて、現在取得可能な条件を返す。
    mode='realtime' なら beforeinfo も取得、'morning' は出走表のみ。"""
    base = "https://www.boatrace.jp/owpc/pc/race"
    racelist_html = fetch_html(f"{base}/racelist?rno={rno}&jcd={JYOJO}&hd={today_str}")
    racers = parse_racelist(racelist_html)
    if not racers or not racers[0].get("grade"):
        return None

    weekday = WEEKDAYS[today.weekday()]
    tide = estimate_tide_class(today)
    boat1 = racers[0]
    cond = {
        "rno": rno,
        "weekday": weekday,
        "tide": tide,
        "grade1": boat1.get("grade", ""),
        "zenkoku_win1": boat1.get("zenkoku_win", ""),
        "motor1": boat1.get("motor_2", ""),
        "f_num1": boat1.get("f_num", "0"),
        "l_num1": boat1.get("l_num", "0"),
    }

    if mode == "realtime":
        before_html = fetch_html(f"{base}/beforeinfo?rno={rno}&jcd={JYOJO}&hd={today_str}")
        weather, exhibitions = parse_beforeinfo(before_html)
        if not weather.get("wind"):
            return None  # 直前情報未配信
        ex1 = exhibitions[0] if exhibitions else {}
        cond.update({
            "wind": weather.get("wind", ""),
            "wind_dir": weather.get("wind_dir", ""),
            "wave": weather.get("wave", ""),
            "ex_time1": ex1.get("ex_time", ""),
            "ex_st1": ex1.get("ex_st", ""),
            "tilt1": ex1.get("tilt", ""),
            "ex_iri1": ex1.get("ex_iri", ""),
        })
    return cond


def is_race_finished(rno, today_str):
    base = "https://www.boatrace.jp/owpc/pc/race"
    result_html = fetch_html(f"{base}/raceresult?rno={rno}&jcd={JYOJO}&hd={today_str}")
    result = parse_raceresult(result_html)
    return bool(result[0])


# ---------- メッセージ組み立て ----------

def format_message(rno, applied, candidates, total_samples, ret_rate, mode):
    bet = bet_per_point(ret_rate)
    n = len(candidates)
    cost = bet * n
    top_ev = max(c[3] for c in candidates)

    title = "リアルタイム" if mode == "realtime" else "朝の予測"
    lines = [
        f"児島 R{rno} 期待値プラス検知 [{title}]",
        "",
        "【条件】",
    ]
    for cname, cvalue in applied:
        lines.append(f"  {cond_label(cname, cvalue)}")

    lines += [
        "",
        f"【類似サンプル】 {total_samples}件",
        f"【合成回収率】 {ret_rate:.1f}% (>100% = 期待値+)",
        f"【最大期待値】 EV={top_ev:.0f}",
        "",
        f"【買い目】各{bet}円 × {n}点 = 計{cost:,}円",
    ]
    for k, prob, avg, ev, c in candidates:
        lines.append(f"  {k} 出現{prob:.1f}% 平均{int(avg):,}円 EV={int(ev)} ({c}回)")
    lines += [
        "",
        f"https://www.boatrace.jp/owpc/pc/race/raceindex?jcd=16",
    ]
    return "\n".join(lines)


# ---------- リアルタイムモード ----------

def run_realtime():
    env = env_get()
    today = date.today()
    today_str = today.strftime("%Y%m%d")
    today_label = today.strftime("%Y-%m-%d")
    history = load_history()
    if not history:
        log("[realtime] ヒストリ未取得のため終了")
        return
    notified = load_notified()
    log(f"[realtime] 判定開始 {today_label} 通知済={len(notified)}")
    new_alerts = 0

    for rno in range(1, 13):
        key = f"{today_label}_R{rno}_realtime"
        if key in notified:
            continue
        if is_race_finished(rno, today_str):
            continue
        cond = gather_conditions(rno, "realtime", today, today_str)
        if cond is None:
            continue
        result = find_pattern(history, cond, "realtime", MIN_SAMPLES)
        if result is None:
            continue
        matched, applied, candidates = result
        ret_rate = compute_return_rate(candidates)
        top_ev = max(c[3] for c in candidates)
        if ret_rate < MIN_RET_RATE or top_ev < MIN_EV:
            continue

        msg = format_message(rno, applied, candidates, len(matched), ret_rate, "realtime")
        if send_line(env, msg):
            notified.add(key)
            save_notified(notified)
            new_alerts += 1
            log(f"[realtime] 通知 R{rno} ret={ret_rate:.1f}% ev={top_ev:.0f}")

    log(f"[realtime] 終了: 新規通知 {new_alerts} 件")


# ---------- モーニングダイジェストモード ----------

def run_morning():
    env = env_get()
    today = date.today()
    today_str = today.strftime("%Y%m%d")
    today_label = today.strftime("%Y-%m-%d")
    history = load_history()
    if not history:
        log("[morning] ヒストリ未取得のため終了")
        return
    log(f"[morning] 判定開始 {today_label}")

    digest_blocks = []
    for rno in range(1, 13):
        cond = gather_conditions(rno, "morning", today, today_str)
        if cond is None:
            continue
        result = find_pattern(history, cond, "morning", MORNING_MIN_SAMPLES)
        if result is None:
            continue
        matched, applied, candidates = result
        ret_rate = compute_return_rate(candidates)
        top_ev = max(c[3] for c in candidates)
        if ret_rate < MIN_RET_RATE or top_ev < MIN_EV:
            continue
        digest_blocks.append({
            "rno": rno,
            "applied": applied,
            "candidates": candidates,
            "samples": len(matched),
            "ret_rate": ret_rate,
            "top_ev": top_ev,
        })

    if not digest_blocks:
        log("[morning] 期待値プラスのレースなし")
        return

    # ダイジェストを1通にまとめる
    lines = [
        f"児島 {today_label} 朝のEVプラス予測 ({len(digest_blocks)}件)",
        "",
    ]
    for d in digest_blocks:
        bet = bet_per_point(d["ret_rate"])
        n = len(d["candidates"])
        cost = bet * n
        lines.append(f"━━━ R{d['rno']} ━━━")
        lines.append(f"類似{d['samples']}件 / 回収率{d['ret_rate']:.1f}% / 最大EV={d['top_ev']:.0f}")
        cond_str = " / ".join(cond_label(cn, cv) for cn, cv in d["applied"])
        lines.append(f"条件: {cond_str}")
        lines.append(f"推奨: 各{bet}円×{n}点 = 計{cost:,}円")
        for k, prob, avg, ev, _c in d["candidates"]:
            lines.append(f"  {k} 出現{prob:.1f}% 平均{int(avg):,}円 EV={int(ev)}")
        lines.append("")
    lines.append("https://www.boatrace.jp/owpc/pc/race/raceindex?jcd=16")
    msg = "\n".join(lines)

    if send_line(env, msg):
        log(f"[morning] 通知送信 {len(digest_blocks)}レース")


def main():
    if "--morning" in sys.argv:
        run_morning()
    else:
        run_realtime()


if __name__ == "__main__":
    main()
