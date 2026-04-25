"""
児島ボートレース 日次自動取得
================================
前日分の全レース（R1〜R12）を boatrace.jp から取得し、
kojima_results.csv / kojima_odds.csv / kojima_tide.csv に追記して
GitHub に自動 push する。

タスクスケジューラから毎朝起動する想定。
"""

import os
import subprocess
from datetime import date, datetime, timedelta

from kojima_history_fetch import (
    RESULTS_CSV, ODDS_CSV, TIDE_CSV,
    fetch_race, parse_racelist, parse_beforeinfo, parse_odds3t, parse_raceresult,
    estimate_tide_class, race_to_results_row,
    append_results, append_odds, append_tide,
    read_existing_dates,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def run_yesterday():
    target = date.today() - timedelta(days=1)
    dl = target.strftime("%Y-%m-%d")
    ds = target.strftime("%Y%m%d")

    existing = read_existing_dates(RESULTS_CSV)
    if any(dl == d for (d, _) in existing):
        print(f"{dl} はすでに取得済み")
        return False

    print(f"取得対象: {dl}")
    results_buf = []
    odds_buf = []
    got = 0
    for rno in range(1, 13):
        _, _, racers, weather, exhibitions, odds, result = fetch_race((ds, rno))
        if not result[0] and not racers:
            continue
        tide_class = estimate_tide_class(target)
        results_buf.append(race_to_results_row(dl, rno, racers, weather, exhibitions, result, tide_class))
        for kaime, odds_val in odds:
            odds_buf.append([dl, rno, kaime, odds_val])
        got += 1

    if got == 0:
        print(f"{dl} は開催なし")
        return False

    append_results(results_buf)
    append_odds(odds_buf)
    append_tide([[dl, estimate_tide_class(target)]])
    print(f"{dl}: {got}レース追記")
    return True


def git_push():
    try:
        subprocess.run(["git", "-C", HERE, "add",
                        "kojima_results.csv", "kojima_odds.csv", "kojima_tide.csv"], check=True)
        subprocess.run(["git", "-C", HERE, "commit", "-m",
                        f"auto update {datetime.now().strftime('%Y-%m-%d')}"], check=True)
        subprocess.run(["git", "-C", HERE, "push", "origin", "main"], check=True)
        print("GitHub push 完了")
    except subprocess.CalledProcessError as e:
        print(f"git push 失敗: {e}")


if __name__ == "__main__":
    if run_yesterday():
        git_push()
