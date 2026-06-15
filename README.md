# 暗号通貨トレジャリー企業 mNAVモニター

xdbdb.com の第2号コンテンツ。Strategy (MSTR) とメタプラネット (3350) の
mNAV（修正純資産価値）プレミアムを毎朝自動計算・表示する。

## ファイル構成

```
crypto-mnav/
├── build_data.py          # バッチ（yfinance取得 → mNAV計算 → data.json出力）
├── crypto_config.json     # 手動更新変数（レイヤー2/3）
├── data.json              # バッチが生成。フロントはこれを fetch するだけ
├── index.html             # フロントエンド（data.json を描画）
├── requirements.txt       # Python依存（yfinance）
└── .github/workflows/
    └── update.yml         # GitHub Actions（JST朝 5本cron）
```

## セットアップ

```bash
pip install -r requirements.txt
python build_data.py   # data.json を生成
```

生成された data.json と index.html を GitHub Pages 等で公開する。

## 手動更新が必要な変数（crypto_config.json）

| 変数 | トリガー |
|---|---|
| `btc_holdings` | BTC買い増し・売却の開示が出た当日 |
| `shares_outstanding` | 増資・新株予約権行使の開示が出た当日 |
| `interest_bearing_debt` | 社債発行・償還の開示が出た当日 |
| `cash_and_equivalents` | 決算発表時（四半期固定） |

開示が出たら `crypto_config.json` の該当社の `value` / `as_of` / `source_url` / `source_label` を更新してコミットする。
次回バッチ実行時に data.json に反映される。

## データレイヤー

| レイヤー | 内容 | 更新 |
|---|---|---|
| L1 | BTC価格(BTC-USD) / 為替(USDJPY=X) / MSTR株価 / メタプラ株価 | バッチが毎朝自動取得（yfinance） |
| L2 | BTC保有枚数 / 発行済株式数 / 有利子負債 | 開示があった当日に手動更新 |
| L3 | 手元現金等 | 決算発表時に手動更新 |

## mNAV 計算式

```
mNAV = (BTC時価評価額 + 手元現金 − 有利子負債) ÷ 発行済株式数

前提:
  - 本業価値ゼロ評価
  - Gross NAV（繰延税金負債を引かない）
  - 希薄化非考慮（転換社債は額面負債として計上）

mnav_premium = 時価総額 / NAV合計（倍率。1超=プレミアム）
spread       = MSTR倍率 − メタプラ倍率（単位: pt）
```

## 失敗時ポリシー

- **BTC価格**：リトライ5回全滅 → 代用なし（`status=failed`）。両社のmNAV算出を停止。
- **株価・為替**：リトライ5回全滅 → 前回値を保持（`status=stale`）して算出続行。
- `overall_status`: `complete` / `partial`（staleあり） / `failed`（BTC失敗）
