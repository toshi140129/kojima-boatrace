# 児島ボートレース 予測・自動化システム

## 環境
- OS: Windows 11
- Python 3.14 / Node.js インストール済み
- リポジトリ: C:\Users\nagao\kojima-boatrace
- データ: kojima_results.csv / kojima_odds.csv / kojima_tide.csv
- タスクスケジューラで kojima_daily.py を毎朝自動実行

## 場の特性
- 児島競艇場 jcd=16
- 水質: 海水（潮の影響あり）
- 潮位: 月齢ベースの簡易推定（大潮/中潮/小潮/長潮/若潮）
  - 精密な潮位データが必要な場合は気象庁/海上保安庁の児島港データへ拡張

## ファイル構成
- kojima_history_fetch.py : 過去4年分一括取得（並列16ワーカー）
- kojima_daily.py         : 毎朝自動で前日分を取得して GitHub push
- kojima_results.csv      : 1行=1レース。結果＋気象＋6艇分の選手情報
- kojima_odds.csv         : 1行=1買い目（3連単直前オッズ）
- kojima_tide.csv         : 1行=1日（潮位区分）

## 取得データ13項目
1. 直前オッズ・人気順 → odds3t / raceresult
2. 選手の現在勝率・今節成績 → racelist
3. 選手の級別（A1/A2/B1/B2） → racelist
4. モーター2連率 → racelist
5. 風速・風向・波高 → beforeinfo
6. 枠番別コース成績（号艇勝率） → racelist
7. 展示タイム → beforeinfo
8. 展示ST → beforeinfo
9. F・L情報 → racelist
10. 前レースの結果（当日） → 同日 raceresult を結合参照
11. 曜日 → 日付計算
12. レース番号 → 既知
13. 児島場の特性（潮位・水質） → kojima_tide.csv / 固定値

## 使い方
```bash
pip install -r requirements.txt
python kojima_history_fetch.py            # 4年分一括（数時間）
python kojima_history_fetch.py 2025-01-01 # 開始日指定
python kojima_daily.py                    # 前日分のみ（毎朝）
```

## 出力ルール
- 出力はすべて日本語
- 手順は最後まで一気に出す（途中で止めない）
- コマンドはそのままコピペで動く形で出す
