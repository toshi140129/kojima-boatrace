# kojima-boatrace

児島競艇場（jcd=16）の過去レースデータを boatrace.jp から取得し、
CSV に蓄積する自動化スクリプト群。蓄積したデータは別リポジトリ
`kojima-webapp` の Next.js アプリから参照される。

## セットアップ

```bash
pip install -r requirements.txt
```

## 過去4年分の一括取得

```bash
python kojima_history_fetch.py
```

引数で開始日を指定可:

```bash
python kojima_history_fetch.py 2025-01-01
```

## 毎朝の自動取得

```bash
python kojima_daily.py
```

タスクスケジューラに登録すると、前日分の全レース結果を自動で
取得し GitHub に push する。

## 出力CSV

| ファイル              | 内容                                    |
|---------------------|---------------------------------------|
| kojima_results.csv  | 1行=1レース。結果・気象・6艇分の選手情報   |
| kojima_odds.csv     | 1行=1買い目。3連単直前オッズ              |
| kojima_tide.csv     | 1行=1日。月齢ベースの潮位区分            |
