"""
児島ボートレース 過去4年分ヒストリ取得スクリプト
================================================

boatrace.jp から児島（jcd=16）の過去4年分（既定: 2022-04-25 〜 今日）の
全レース（R1〜R12）について以下4ページを並列取得し、CSV3本にまとめる。

  - racelist     : 出走表（選手名・級別・勝率・今節成績・モーター2連率・F/L）
  - beforeinfo   : 直前情報（風速・風向・波高・気温・水温・展示タイム・展示ST）
  - odds3t       : 3連単直前オッズ
  - raceresult   : 結果（着順・3連単払戻・人気）

出力CSV:
  - kojima_results.csv : 1行=1レース。結果＋気象＋6艇分の選手情報を横展開
  - kojima_odds.csv    : 1行=1買い目。3連単直前オッズ
  - kojima_tide.csv    : 1行=1日。児島港の潮位（簡易版・日付の月齢ベース）

特徴:
  - 開催日のみ取得。休場日・データなしレースは自動でスキップ
  - 並列ワーカー 16
  - リトライ3回、タイムアウト30s
  - 既存CSVがあれば差分のみ取得（日付ベース）

使い方:
  python kojima_history_fetch.py            # 既定: 2022-04-25 から今日まで
  python kojima_history_fetch.py 2024-01-01 # 開始日指定
"""

import csv
import os
import re
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_CSV = os.path.join(HERE, "kojima_results.csv")
ODDS_CSV = os.path.join(HERE, "kojima_odds.csv")
TIDE_CSV = os.path.join(HERE, "kojima_tide.csv")

JYOJO = "16"  # 児島競艇場コード
MAX_WORKERS = 16
DEFAULT_START = date.today() - timedelta(days=365 * 4)
END = date.today()

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]

# 1レース分のヘッダ（1行=1レース、横展開）
RESULTS_HEADER = (
    [
        "日付", "曜日", "レース番号",
        "1着", "2着", "3着", "3連単払戻", "人気",
        "風速", "風向", "波高", "気温", "水温",
        "水質", "潮位区分",
    ]
    + [
        f"{i}艇_{col}"
        for i in range(1, 7)
        for col in (
            "選手名", "級別", "全国勝率", "全国2連率", "当地勝率",
            "今節成績", "モーター2連率", "ボート2連率",
            "展示タイム", "展示ST", "F数", "L数",
        )
    ]
)

ODDS_HEADER = ["日付", "レース番号", "買い目", "オッズ"]
TIDE_HEADER = ["日付", "潮位区分"]


# ---------- 共通 ----------

def fetch_html(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == retries - 1:
                print(f"  FETCH FAIL {url}: {e}", file=sys.stderr, flush=True)
                return ""
            time.sleep(1)
    return ""


def norm(s):
    return unicodedata.normalize("NFKC", (s or "").strip())


# ---------- racelist パース ----------

def parse_racelist(html):
    """6艇分の選手情報を返す。 racers[i] = dict (i=0..5)。データなしは []."""
    if not html or "データがありません" in html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
        # 出走表テーブルは tbody.is-fs12 が艇数分並ぶ
        tbodies = soup.select("tbody.is-fs12")
        racers = []
        for tbody in tbodies[:6]:
            first_tr = tbody.find("tr")
            if not first_tr:
                racers.append({})
                continue
            tds = first_tr.find_all("td")
            if len(tds) < 8:
                racers.append({})
                continue

            # td[2]: 4桁登番 / 級別、選手名、出身/支部、年齢/体重
            info_td = tds[2]
            info_text = norm(info_td.get_text("\n", strip=True))
            grade = ""
            m_grade = re.search(r"\b(A1|A2|B1|B2)\b", info_text)
            if m_grade:
                grade = m_grade.group(1)
            name = ""
            a = info_td.find("a", href=re.compile(r"racersearch/profile"))
            if a:
                name = re.sub(r"[\s　]+", "", a.get_text(strip=True))

            # td[3]: F数/L数/平均ST （改行区切り）
            fl_text = norm(tds[3].get_text("\n", strip=True))
            f_num = l_num = avg_st = ""
            m_f = re.search(r"F\s*(\d+)", fl_text)
            m_l = re.search(r"L\s*(\d+)", fl_text)
            m_st = re.search(r"(\d+\.\d+)", fl_text)
            if m_f: f_num = m_f.group(1)
            if m_l: l_num = m_l.group(1)
            if m_st: avg_st = m_st.group(1)

            # td[4]: 全国勝率/全国2連率/全国3連率
            zen_text = norm(tds[4].get_text("\n", strip=True))
            zen_nums = re.findall(r"\d+\.\d+", zen_text)
            zenkoku_win = zen_nums[0] if len(zen_nums) >= 1 else ""
            zenkoku_2 = zen_nums[1] if len(zen_nums) >= 2 else ""

            # td[5]: 当地勝率/当地2連率/当地3連率
            tou_text = norm(tds[5].get_text("\n", strip=True))
            tou_nums = re.findall(r"\d+\.\d+", tou_text)
            touchi_win = tou_nums[0] if len(tou_nums) >= 1 else ""

            # td[6]: モーター番号/モーター2連率/モーター3連率
            mot_text = norm(tds[6].get_text("\n", strip=True))
            mot_nums = re.findall(r"\d+\.\d+", mot_text)
            motor_2 = mot_nums[0] if len(mot_nums) >= 1 else ""

            # td[7]: ボート番号/ボート2連率/ボート3連率
            bo_text = norm(tds[7].get_text("\n", strip=True))
            bo_nums = re.findall(r"\d+\.\d+", bo_text)
            boat_2 = bo_nums[0] if len(bo_nums) >= 1 else ""

            # 今節成績: 後続行の数字セルを連結。艇番セル（is-boatColor*）は除外
            kosetsu_cells = []
            for tr in tbody.find_all("tr"):
                for td in tr.find_all("td"):
                    cls = " ".join(td.get("class", []))
                    if "is-boatColor" in cls:
                        continue
                    txt = norm(td.get_text(strip=True))
                    if re.fullmatch(r"\d|\d{1,2}|F|L|K|S", txt or ""):
                        kosetsu_cells.append(txt)
            kosetsu = "/".join(kosetsu_cells[:12])

            racers.append({
                "name": name,
                "grade": grade,
                "zenkoku_win": zenkoku_win,
                "zenkoku_2": zenkoku_2,
                "touchi_win": touchi_win,
                "kosetsu": kosetsu[:30],
                "motor_2": motor_2,
                "boat_2": boat_2,
                "avg_st": avg_st,
                "f_num": f_num,
                "l_num": l_num,
            })
        return racers
    except Exception as e:
        print(f"  racelist parse error: {e}", file=sys.stderr, flush=True)
        return []


# ---------- beforeinfo パース ----------

def parse_beforeinfo(html):
    """(weather_dict, [exhibition_per_boat])."""
    if not html or "データがありません" in html:
        return {}, []
    try:
        soup = BeautifulSoup(html, "html.parser")
        weather = {"wind": "", "wind_dir": "", "wave": "", "temp": "", "water_temp": ""}

        w = soup.select_one(".weather1")
        if w:
            node = w.select_one(".is-wind .weather1_bodyUnitLabelData")
            if node:
                m = re.search(r"(\d+)", norm(node.get_text()))
                if m: weather["wind"] = m.group(1)
            node = w.select_one(".is-wave .weather1_bodyUnitLabelData")
            if node:
                m = re.search(r"(\d+)", norm(node.get_text()))
                if m: weather["wave"] = m.group(1)
            node = w.select_one(".is-windDirection .weather1_bodyUnitImage")
            if node:
                for cls in node.get("class", []):
                    m = re.match(r"is-wind(\d+)", cls)
                    if m:
                        weather["wind_dir"] = m.group(1)
                        break
            # 気温 / 水温
            for unit in w.select(".weather1_bodyUnit"):
                label = norm(unit.select_one(".weather1_bodyUnitLabelTitle").get_text()) if unit.select_one(".weather1_bodyUnitLabelTitle") else ""
                data = unit.select_one(".weather1_bodyUnitLabelData")
                if data:
                    m = re.search(r"([-\d.]+)", norm(data.get_text()))
                    if m:
                        if "気温" in label:
                            weather["temp"] = m.group(1)
                        elif "水温" in label:
                            weather["water_temp"] = m.group(1)

        # 展示タイム: table.is-w748 の各 tbody.is-fs12 から
        exhibitions = [{"ex_time": "", "ex_st": ""} for _ in range(6)]
        time_table = soup.select_one("table.is-w748")
        if time_table:
            tbodies = time_table.select("tbody.is-fs12")
            for i, tbody in enumerate(tbodies[:6]):
                first_tr = tbody.find("tr")
                if not first_tr:
                    continue
                tds = first_tr.find_all("td")
                # td[0]=艇番, [1]=写真, [2]=選手名, [3]=体重, [4]=展示タイム
                if len(tds) >= 5:
                    m = re.search(r"\d+\.\d+", norm(tds[4].get_text(strip=True)))
                    if m:
                        exhibitions[i]["ex_time"] = m.group(0)

        # 展示ST: table.is-w238 の進入順テーブル
        st_table = soup.select_one("table.is-w238")
        if st_table:
            for row in st_table.select("tbody tr"):
                num_node = row.select_one(".table1_boatImage1Number")
                time_node = row.select_one(".table1_boatImage1Time")
                if not (num_node and time_node):
                    continue
                # 艇番を class is-typeN から取得（DOMテキストは進入順表示用）
                cls = " ".join(num_node.get("class", []))
                m_b = re.search(r"is-type(\d+)", cls)
                if not m_b:
                    m_b_text = re.search(r"\d", num_node.get_text())
                    if m_b_text:
                        boat = int(m_b_text.group(0))
                    else:
                        continue
                else:
                    boat = int(m_b.group(1))
                if not (1 <= boat <= 6):
                    continue
                st_text = norm(time_node.get_text(strip=True))
                # ".08" → "0.08", "F.05" → "F0.05"
                if st_text.startswith("."):
                    st_text = "0" + st_text
                elif st_text.startswith("F."):
                    st_text = "F0" + st_text[1:]
                exhibitions[boat - 1]["ex_st"] = st_text

        return weather, exhibitions
    except Exception as e:
        print(f"  beforeinfo parse error: {e}", file=sys.stderr, flush=True)
        return {}, []


# ---------- odds3t パース ----------

def _build_3t_patterns():
    """3連単120通りの (1着,2着,3着) を、boatrace.jpの表DOM順で返す。
    DOM順は: 行r(0..19)の各列c(0..5)について、1着=c+1、
    その列内では (2着,3着) が「2着 ∈ others, 3着 ∈ others-2着」を辞書順で20通り。"""
    cols = []
    for c in range(6):
        ichi = c + 1
        others = [b for b in range(1, 7) if b != ichi]
        seq = []
        for ni in others:
            for san in [b for b in range(1, 7) if b != ichi and b != ni]:
                seq.append((ichi, ni, san))
        cols.append(seq)
    return cols  # cols[c][r] = (1,2,3)

_PATTERNS_3T = _build_3t_patterns()


def parse_odds3t(html):
    """3連単オッズ全120通り。 list of (kaime, odds)."""
    if not html or "データがありません" in html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
        odds_cells = soup.select("td.oddsPoint")
        if len(odds_cells) < 120:
            return []
        results = []
        for r in range(20):
            for c in range(6):
                idx = r * 6 + c
                txt = norm(odds_cells[idx].get_text(strip=True))
                ichi, ni, san = _PATTERNS_3T[c][r]
                results.append((f"{ichi}-{ni}-{san}", txt))
        return results
    except Exception as e:
        print(f"  odds3t parse error: {e}", file=sys.stderr, flush=True)
        return []


# ---------- raceresult パース ----------

def parse_raceresult(html):
    """(p1, p2, p3, pay3t, ninki). データなしは全て''."""
    if not html or "データがありません" in html:
        return ("", "", "", "", "")
    try:
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.select("table.is-w495")
        p1 = p2 = p3 = pay = ninki = ""
        if tables:
            for tr in tables[0].select("tbody tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    rnk = norm(tds[0].get_text())
                    boat = tds[1].get_text(strip=True)
                    if rnk == "1": p1 = boat
                    elif rnk == "2": p2 = boat
                    elif rnk == "3": p3 = boat
        if len(tables) >= 3:
            for tr in tables[2].select("tbody tr"):
                tds = tr.find_all("td")
                if len(tds) >= 3 and "3連単" in tds[0].get_text():
                    pay = (
                        tds[2].get_text(strip=True)
                        .replace("¥", "").replace(",", "").replace("円", "").strip()
                    )
                    if len(tds) >= 4:
                        ninki = norm(tds[3].get_text())
                    break
        return (p1, p2, p3, pay, ninki)
    except Exception as e:
        print(f"  raceresult parse error: {e}", file=sys.stderr, flush=True)
        return ("", "", "", "", "")


# ---------- 1レース分まとめ取得 ----------

def fetch_race(args):
    date_str, rno = args
    base = f"https://www.boatrace.jp/owpc/pc/race"
    racelist = parse_racelist(fetch_html(f"{base}/racelist?rno={rno}&jcd={JYOJO}&hd={date_str}"))
    weather, exhibitions = parse_beforeinfo(fetch_html(f"{base}/beforeinfo?rno={rno}&jcd={JYOJO}&hd={date_str}"))
    odds = parse_odds3t(fetch_html(f"{base}/odds3t?rno={rno}&jcd={JYOJO}&hd={date_str}"))
    result = parse_raceresult(fetch_html(f"{base}/raceresult?rno={rno}&jcd={JYOJO}&hd={date_str}"))
    return (date_str, rno, racelist, weather, exhibitions, odds, result)


# ---------- 潮位簡易計算（児島港・月齢ベース） ----------

def estimate_tide_class(d):
    """日付から大潮/中潮/小潮/長潮/若潮を概算（旧暦月齢ベース）。
    児島港の正確な潮位は気象庁データを別途取得すべきだが、
    まずは月齢ベースの区分のみ提供。"""
    # 1900-01-01 が新月だった概算で月齢を求める（誤差±1日）
    ref = date(2000, 1, 6)  # 2000-01-06は新月
    days = (d - ref).days
    age = days % 29.530588  # 月齢
    if age < 0:
        age += 29.530588
    # 0,15:大潮 / 7,22:小潮 / 8:長潮 / 9:若潮 / その他:中潮
    a = round(age)
    if a in (0, 1, 2, 14, 15, 16, 28, 29):
        return "大潮"
    if a in (7, 8, 22, 23):
        return "小潮"
    if a == 9:
        return "長潮"
    if a == 10:
        return "若潮"
    return "中潮"


# ---------- CSV I/O ----------

def read_existing_dates(csv_path, key_cols=("日付", "レース番号")):
    if not os.path.exists(csv_path):
        return set()
    keys = set()
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keys.add(tuple(row.get(c, "") for c in key_cols))
    return keys


def append_results(rows):
    new_file = not os.path.exists(RESULTS_CSV)
    with open(RESULTS_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(RESULTS_HEADER)
        for row in rows:
            writer.writerow(row)


def append_odds(rows):
    new_file = not os.path.exists(ODDS_CSV)
    with open(ODDS_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(ODDS_HEADER)
        for row in rows:
            writer.writerow(row)


def append_tide(rows):
    new_file = not os.path.exists(TIDE_CSV)
    with open(TIDE_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(TIDE_HEADER)
        for row in rows:
            writer.writerow(row)


# ---------- 1レース → CSV行に展開 ----------

def race_to_results_row(date_label, rno, racers, weather, exhibitions, result, tide_class):
    weekday = WEEKDAYS[datetime.strptime(date_label, "%Y-%m-%d").weekday()]
    p1, p2, p3, pay, ninki = result
    row = [
        date_label, weekday, rno,
        p1, p2, p3, pay, ninki,
        weather.get("wind", ""), weather.get("wind_dir", ""),
        weather.get("wave", ""), weather.get("temp", ""), weather.get("water_temp", ""),
        "海水", tide_class,
    ]
    for i in range(6):
        r = racers[i] if i < len(racers) else {}
        ex = exhibitions[i] if i < len(exhibitions) else {}
        row += [
            r.get("name", ""), r.get("grade", ""),
            r.get("zenkoku_win", ""), r.get("zenkoku_2", ""), r.get("touchi_win", ""),
            r.get("kosetsu", ""), r.get("motor_2", ""), r.get("boat_2", ""),
            ex.get("ex_time", ""), ex.get("ex_st", ""),
            r.get("f_num", ""), r.get("l_num", ""),
        ]
    return row


# ---------- メイン ----------

def main():
    start = DEFAULT_START
    if len(sys.argv) > 1:
        start = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()

    print(f"児島ボートレース ヒストリ取得", flush=True)
    print(f"期間: {start} 〜 {END}", flush=True)

    existing_results = read_existing_dates(RESULTS_CSV)
    existing_tide = read_existing_dates(TIDE_CSV, key_cols=("日付",))
    print(f"既存結果データ: {len(existing_results)}レース  既存潮位: {len(existing_tide)}日", flush=True)

    # 取得対象日生成
    all_dates = []
    d = start
    while d <= END:
        all_dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    # 既存と差分の (日付, レース番号) を取得対象に
    tasks = []
    for dl in all_dates:
        ds = dl.replace("-", "")
        for rno in range(1, 13):
            if (dl, str(rno)) not in existing_results:
                tasks.append((ds, rno))

    print(f"取得タスク: {len(tasks)}レース (各4ページ並列, ワーカー{MAX_WORKERS})", flush=True)
    if not tasks:
        print("取得対象なし", flush=True)
        # 潮位だけ追加
        update_tide(all_dates, existing_tide)
        return

    start_t = time.time()
    results_buf = []
    odds_buf = []
    done = 0
    skipped_empty = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for ds, rno, racers, weather, exhibitions, odds, result in ex.map(fetch_race, tasks):
            done += 1
            dl = f"{ds[0:4]}-{ds[4:6]}-{ds[6:8]}"

            # 結果も選手情報も空 → 開催なし
            if not result[0] and not racers:
                skipped_empty += 1
            else:
                tide_class = estimate_tide_class(datetime.strptime(dl, "%Y-%m-%d").date())
                results_buf.append(race_to_results_row(dl, rno, racers, weather, exhibitions, result, tide_class))
                for kaime, odds_val in odds:
                    odds_buf.append([dl, rno, kaime, odds_val])

            # 100件ごとに進捗 + バッファflush
            if done % 100 == 0 or done == len(tasks):
                elapsed = time.time() - start_t
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(tasks) - done) / rate if rate > 0 else 0
                print(f"  [{done}/{len(tasks)}] elapsed={elapsed:.0f}s rate={rate:.1f}/s eta={eta:.0f}s skip_empty={skipped_empty}", flush=True)
                if results_buf:
                    append_results(results_buf)
                    results_buf = []
                if odds_buf:
                    append_odds(odds_buf)
                    odds_buf = []

    if results_buf:
        append_results(results_buf)
    if odds_buf:
        append_odds(odds_buf)

    print(f"完了: {time.time()-start_t:.0f}s 開催なしスキップ={skipped_empty}", flush=True)
    update_tide(all_dates, existing_tide)


def update_tide(all_dates, existing_tide):
    new_rows = []
    for dl in all_dates:
        if (dl,) in existing_tide:
            continue
        d = datetime.strptime(dl, "%Y-%m-%d").date()
        new_rows.append([dl, estimate_tide_class(d)])
    if new_rows:
        append_tide(new_rows)
        print(f"潮位データ追加: {len(new_rows)}日", flush=True)


if __name__ == "__main__":
    main()
