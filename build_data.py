#!/usr/bin/env python3
"""
build_data.py — 暗号通貨トレジャリー企業 mNAV 算出バッチ

crypto_config.json(レイヤー2/3 手動変数)を読み込み、
yfinance でレイヤー1(BTC価格/為替/株価)を取得して mNAV を計算し、
data.json を書き出す。sbg-nav/build_data.py の構造を踏襲。
"""

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yfinance as yf

# Windows の cp932 端末でも Unicode 文字を安全に出力する
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass  # Python < 3.7 / reconfigure 非対応環境では無視

# =========================================================================
# 定数
# =========================================================================
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "crypto_config.json"
DATA_PATH   = SCRIPT_DIR / "data.json"

JST = timezone(timedelta(hours=9))
MAX_RETRIES    = 5
RETRY_INTERVAL = 10  # 秒（リトライ間隔）

# 異常変動しきい値（株価・為替）
STOCK_FX_CHANGE_THRESHOLD_PCT = 20.0  # ±20%

# BTC の前日比チェックは無効化する。
# 理由: BTC は単日 ±20% 超の値動きが歴史上複数回あり（2017・2021等）、
#       このしきい値で stale 扱いにすると正常な大幅変動を誤って弾く。
#       0以下など明らかに不正な値のチェック（下記）は別途行う。
BTC_CHANGE_VALIDATION_ENABLED = False

# タイムスタンプ鮮度チェックのしきい値（警告のみ・算出は続行）
# BTC/為替: 24/7・24/5 資産なので 1.5日超で異常とみなす
# 株価(MSTR): 米国市場は最長3日連休（土日+祝月）→ 4日超で警告
# 株価(3350.T): 日本は連休が長い（GW等5日+）→ 5日超で警告
FRESHNESS_MAX_DAYS_BTC_FX    = 1.5
FRESHNESS_MAX_DAYS_US_STOCK  = 4.0
FRESHNESS_MAX_DAYS_JP_STOCK  = 5.0


# =========================================================================
# ユーティリティ
# =========================================================================
def now_jst() -> datetime:
    return datetime.now(JST)


def to_iso_jst(dt: datetime) -> str:
    """datetime を JST ISO8601 文字列 (±09:00) に変換。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_jst = dt.astimezone(JST)
    return dt_jst.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def prev_market_value(prev_data: dict, key: str):
    """前回 data.json から market_data[key].value を取得（stale フォールバック用）。"""
    try:
        return prev_data["market_data"][key]["value"]
    except (KeyError, TypeError):
        return None


# =========================================================================
# レイヤー1 取得（リトライ付き）
# =========================================================================
def fetch_close(ticker_str: str) -> tuple:
    """
    yfinance でティッカーの直近終値・タイムスタンプ・前日比を返す。
    日米混在問題（片方がNaN）は period="5d" + dropna() で対処。
    Returns: (close: float|None, as_of: str|None, change_pct: float|None)
    5 回リトライ全滅時は (None, None, None) を返す。
    """
    for attempt in range(MAX_RETRIES):
        try:
            t = yf.Ticker(ticker_str)
            hist = t.history(period="5d")
            if hist.empty:
                raise ValueError(f"{ticker_str}: empty history")

            closes = hist["Close"].dropna()
            if closes.empty:
                raise ValueError(f"{ticker_str}: all NaN closes")

            last_val = float(closes.iloc[-1])
            last_ts  = closes.index[-1]

            # pandas Timestamp → datetime
            if hasattr(last_ts, "to_pydatetime"):
                ts_dt = last_ts.to_pydatetime()
            else:
                ts_dt = last_ts

            as_of = to_iso_jst(ts_dt)  # naive の場合は UTC と見なして JST 変換

            # 前日比
            change_pct = None
            if len(closes) >= 2:
                prev_val = float(closes.iloc[-2])
                if prev_val and prev_val != 0:
                    change_pct = round((last_val - prev_val) / abs(prev_val) * 100, 4)

            return last_val, as_of, change_pct

        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_INTERVAL)

    return None, None, None


# =========================================================================
# バリデーション
# =========================================================================
def check_abnormal_change(change_pct: float | None, label: str, alerts: list) -> bool:
    """前日比が ±STOCK_FX_CHANGE_THRESHOLD_PCT を超えたら True を返し alerts に積む。"""
    if change_pct is None:
        return False
    if abs(change_pct) > STOCK_FX_CHANGE_THRESHOLD_PCT:
        alerts.append(
            f"[警告] {label}: 前日比 {change_pct:+.2f}% が閾値 ±{STOCK_FX_CHANGE_THRESHOLD_PCT:.0f}% を超過。"
            f" 異常変動の可能性があるため stale 扱いに変更します。"
        )
        return True
    return False


def check_timestamp_freshness(as_of_iso: str | None, label: str, max_days: float, alerts: list):
    """
    取得値の基準日が max_days より古ければ alerts に警告を積む。
    算出・ステータスは変更しない（警告のみ）。
    連休・長期休場は正常なので、極端に古い場合（取得不具合等）だけ検知する。
    """
    if as_of_iso is None:
        return
    try:
        ts = datetime.fromisoformat(as_of_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        age_days = (now_utc - ts.astimezone(timezone.utc)).total_seconds() / 86400
        if age_days > max_days:
            alerts.append(
                f"[鮮度警告] {label}: 取得値が {age_days:.1f}日前 (閾値 {max_days}日超)。"
                f" 取得不具合の可能性。as_of={as_of_iso}"
            )
    except Exception:
        pass  # パース失敗は無視（as_of の形式が想定外の場合）


# =========================================================================
# mNAV 計算（1社分）
# =========================================================================
def calc_company(
    company_cfg: dict,
    btc_price,       # float|None (MSTRはUSD、メタプラはJPY)
    btc_price_key: str,
    stock_price,     # float|None
    stock_price_key: str,
    usdjpy_val,      # float|None
    btc_failed: bool,
    stock_status: str,
    usdjpy_status: str,
    btc_usd_as_of: str | None,
    stock_as_of: str | None,
) -> dict:
    cid      = company_cfg.get("id", "")
    currency = company_cfg.get("base_currency", "")
    l2  = company_cfg.get("layer2", {})
    l3  = company_cfg.get("layer3", {})
    ref = company_cfg.get("reference_only", {})

    btc_holdings       = l2.get("btc_holdings", {}).get("value")
    shares_outstanding = l2.get("shares_outstanding", {}).get("value")
    debt               = l2.get("interest_bearing_debt", {}).get("value")
    preferred_equity   = l2.get("preferred_equity", {}).get("value")  # None → 0 扱い
    cash               = l3.get("cash_and_equivalents", {}).get("value")

    # --- inputs_snapshot ---
    def snap_l2(field):
        d = l2.get(field, {})
        return {"value": d.get("value"), "as_of": d.get("as_of"),
                "source_label": d.get("source_label"), "source_url": d.get("source_url"), "layer": 2}

    def snap_l3(field):
        d = l3.get(field, {})
        return {"value": d.get("value"), "as_of": d.get("as_of"),
                "source_label": d.get("source_label"), "source_url": d.get("source_url"),
                "fiscal_period": d.get("fiscal_period"), "layer": 3}

    inputs_snapshot = {
        "btc_holdings":          snap_l2("btc_holdings"),
        "shares_outstanding":    snap_l2("shares_outstanding"),
        "interest_bearing_debt": snap_l2("interest_bearing_debt"),
        "preferred_equity":      snap_l2("preferred_equity"),
        "cash_and_equivalents":  snap_l3("cash_and_equivalents"),
        btc_price_key:           {"value": btc_price,   "as_of": btc_usd_as_of, "layer": 1},
        stock_price_key:         {"value": stock_price, "as_of": stock_as_of,   "layer": 1},
    }

    # --- company_status ---
    if btc_failed:
        company_status = "error"
    elif stock_status == "stale" or usdjpy_status == "stale":
        company_status = "partial"
    else:
        company_status = "ok"

    # --- calc formula テンプレート ---
    def null_calc():
        formulas = {
            "btc_value":          {"currency": currency, "formula": f"btc_holdings × {btc_price_key}"},
            "preferred_equity":   {"currency": currency, "formula": "from layer2 (senior claim)",
                                   "note": "シニア・クレーム控除項目。None→0として算出。"},
            "senior_claims_total":{"currency": currency, "formula": "interest_bearing_debt + preferred_equity",
                                   "note": "シニア・クレーム合計（負債＋優先株）。"},
            "nav_total":          {"currency": currency, "formula": "btc_value + cash - debt - preferred_equity"},
            "nav_per_share":      {"currency": currency, "formula": "nav_total / shares_outstanding"},
            "market_cap":         {"currency": currency, "formula": f"{stock_price_key} × shares_outstanding"},
            "mnav_premium":       {"unit": "x",  "formula": "market_cap / nav_total",
                                   "note": "1超=プレミアム、1未満=ディスカウント。"},
            "premium_pct":        {"unit": "%",  "formula": "(mnav_premium - 1) × 100"},
        }
        return {k: {"value": None, **v} for k, v in formulas.items()}

    def null_secondary():
        if cid == "mstr":
            return {
                "nav_total_jpy":  {"value": None, "formula": "nav_total × usdjpy"},
                "market_cap_jpy": {"value": None, "formula": "market_cap × usdjpy"},
            }
        else:
            return {
                "nav_total_usd":  {"value": None, "formula": "nav_total / usdjpy"},
                "market_cap_usd": {"value": None, "formula": "market_cap / usdjpy"},
            }

    # --- 算出不可チェック ---
    # btc_holdings・shares_outstanding が null の場合は意味のある mNAV を計算できない。
    # cash・debt は null → 0 扱いで続行（未更新の場合に計算を止めない）。
    cannot_calc = (
        btc_failed
        or btc_price is None
        or btc_holdings is None
        or shares_outstanding is None
        or shares_outstanding == 0
    )

    if cannot_calc:
        calc = null_calc()
        display_secondary = null_secondary()
    else:
        debt_v  = debt             or 0
        cash_v  = cash             or 0
        pref_v  = preferred_equity or 0  # None → 0（メタプラは 0 が入っている）
        stock_v = stock_price      or 0

        btc_value           = round(btc_holdings * btc_price, 2)
        senior_claims_total = round(debt_v + pref_v, 2)
        nav_total           = round(btc_value + cash_v - debt_v - pref_v, 2)
        nav_per_share       = round(nav_total / shares_outstanding, 6)
        market_cap          = round(stock_v * shares_outstanding, 2)

        mnav_premium = None
        premium_pct  = None
        if nav_total and nav_total != 0 and market_cap:
            mnav_premium = round(market_cap / nav_total, 6)
            premium_pct  = round((mnav_premium - 1) * 100, 4)

        calc = {
            "btc_value":          {"value": btc_value,           "currency": currency, "formula": f"btc_holdings × {btc_price_key}"},
            "preferred_equity":   {"value": pref_v,              "currency": currency, "formula": "from layer2 (senior claim)",
                                   "note": "シニア・クレーム控除項目。None→0として算出。"},
            "senior_claims_total":{"value": senior_claims_total, "currency": currency, "formula": "interest_bearing_debt + preferred_equity",
                                   "note": "シニア・クレーム合計（負債＋優先株）。"},
            "nav_total":          {"value": nav_total,           "currency": currency, "formula": "btc_value + cash - debt - preferred_equity"},
            "nav_per_share":      {"value": nav_per_share,       "currency": currency, "formula": "nav_total / shares_outstanding"},
            "market_cap":         {"value": market_cap,          "currency": currency, "formula": f"{stock_price_key} × shares_outstanding"},
            "mnav_premium":       {"value": mnav_premium,        "unit": "x",          "formula": "market_cap / nav_total",
                                   "note": "1超=プレミアム、1未満=ディスカウント。"},
            "premium_pct":        {"value": premium_pct,         "unit": "%",          "formula": "(mnav_premium - 1) × 100"},
        }

        if cid == "mstr":
            ntj = round(nav_total  * usdjpy_val, 0) if usdjpy_val else None
            mcj = round(market_cap * usdjpy_val, 0) if usdjpy_val else None
            display_secondary = {
                "nav_total_jpy":  {"value": ntj, "formula": "nav_total × usdjpy"},
                "market_cap_jpy": {"value": mcj, "formula": "market_cap × usdjpy"},
            }
        else:
            ntu = round(nav_total  / usdjpy_val, 2) if usdjpy_val else None
            mcu = round(market_cap / usdjpy_val, 2) if usdjpy_val else None
            display_secondary = {
                "nav_total_usd":  {"value": ntu, "formula": "nav_total / usdjpy"},
                "market_cap_usd": {"value": mcu, "formula": "market_cap / usdjpy"},
            }

    return {
        "id":           cid,
        "display_name": company_cfg.get("display_name", ""),
        "ticker":       company_cfg.get("ticker", ""),
        "base_currency": currency,
        "status":       company_status,
        "inputs_snapshot":            inputs_snapshot,
        "calc":                       calc,
        "display_secondary_currency": display_secondary,
        "reference_only": {
            "btc_avg_cost": {
                "value":    ref.get("btc_avg_cost", {}).get("value"),
                "currency": currency,
            }
        },
    }


# =========================================================================
# メイン
# =========================================================================
def build_data():
    generated_at = now_jst()
    alerts: list[str] = []

    config    = load_json(CONFIG_PATH)
    prev_data = load_json(DATA_PATH)

    companies_by_id = {c["id"]: c for c in config.get("companies", [])}

    # -----------------------------------------------------------------------
    # レイヤー1 取得
    # -----------------------------------------------------------------------

    # BTC-USD ---------------------------------------------------------------
    btc_usd_val, btc_usd_as_of, btc_usd_chg = fetch_close("BTC-USD")
    if btc_usd_val is None:
        btc_status = "failed"
        alerts.append(
            "[エラー] BTC価格(BTC-USD)の取得に5回リトライ後も失敗しました。"
            " mNAV算出を中止します（BTCは前回値代用不可）。"
        )
    elif btc_usd_val <= 0:
        btc_status = "failed"
        btc_usd_val = None
        alerts.append("[エラー] BTC価格が不正値(0以下)です。mNAV算出を中止します。")
    else:
        btc_status = "ok"
        # BTC_CHANGE_VALIDATION_ENABLED=False のため前日比チェックをスキップ（上部コメント参照）
        check_timestamp_freshness(btc_usd_as_of, "BTC-USD", FRESHNESS_MAX_DAYS_BTC_FX, alerts)

    # USDJPY=X --------------------------------------------------------------
    usdjpy_val, usdjpy_as_of, usdjpy_chg = fetch_close("USDJPY=X")
    if usdjpy_val is None or usdjpy_val <= 0:
        fallback = prev_market_value(prev_data, "usdjpy")
        usdjpy_val    = fallback
        usdjpy_status = "stale"
        usdjpy_as_of  = None
        usdjpy_chg    = None
        if fallback:
            alerts.append(f"[警告] USDJPY取得失敗。前回値({fallback:.2f})を保持して続行します。")
        else:
            alerts.append("[警告] USDJPY取得失敗。前回値もなし。JPY換算値が null になります。")
    else:
        usdjpy_status = "ok"
        check_timestamp_freshness(usdjpy_as_of, "USDJPY=X", FRESHNESS_MAX_DAYS_BTC_FX, alerts)
        if check_abnormal_change(usdjpy_chg, "USDJPY", alerts):
            fallback = prev_market_value(prev_data, "usdjpy")
            if fallback:
                usdjpy_val = fallback
                alerts.append(f"  → 前回値({fallback:.2f})を保持します。")
            usdjpy_status = "stale"

    # BTC-JPY（派生値） -----------------------------------------------------
    if btc_status == "failed" or btc_usd_val is None:
        btc_jpy_val, btc_jpy_status = None, "failed"
    elif usdjpy_val is None:
        btc_jpy_val, btc_jpy_status = None, "stale"
    else:
        btc_jpy_val   = round(btc_usd_val * usdjpy_val, 0)
        btc_jpy_status = "ok"

    # MSTR ------------------------------------------------------------------
    mstr_val, mstr_as_of, mstr_chg = fetch_close("MSTR")
    if mstr_val is None or mstr_val <= 0:
        fallback    = prev_market_value(prev_data, "mstr_price_usd")
        mstr_val    = fallback
        mstr_status = "stale"
        mstr_as_of  = None
        mstr_chg    = None
        if fallback:
            alerts.append(f"[警告] MSTR株価取得失敗。前回値(${fallback:.2f})を保持して続行します。")
        else:
            alerts.append("[警告] MSTR株価取得失敗。前回値もなし。MSTRの calc が null になります。")
    else:
        mstr_status = "ok"
        check_timestamp_freshness(mstr_as_of, "MSTR", FRESHNESS_MAX_DAYS_US_STOCK, alerts)
        if check_abnormal_change(mstr_chg, "MSTR", alerts):
            fallback = prev_market_value(prev_data, "mstr_price_usd")
            if fallback:
                mstr_val = fallback
                alerts.append(f"  → 前回値(${fallback:.2f})を保持します。")
            mstr_status = "stale"

    # 3350.T（メタプラネット）-----------------------------------------------
    meta_val, meta_as_of, meta_chg = fetch_close("3350.T")
    if meta_val is None or meta_val <= 0:
        fallback    = prev_market_value(prev_data, "metaplanet_price_jpy")
        meta_val    = fallback
        meta_status = "stale"
        meta_as_of  = None
        meta_chg    = None
        if fallback:
            alerts.append(f"[警告] メタプラネット株価取得失敗。前回値(¥{fallback:.0f})を保持して続行します。")
        else:
            alerts.append("[警告] メタプラネット株価取得失敗。前回値もなし。メタプラの calc が null になります。")
    else:
        meta_status = "ok"
        check_timestamp_freshness(meta_as_of, "3350.T", FRESHNESS_MAX_DAYS_JP_STOCK, alerts)
        if check_abnormal_change(meta_chg, "3350.T", alerts):
            fallback = prev_market_value(prev_data, "metaplanet_price_jpy")
            if fallback:
                meta_val = fallback
                alerts.append(f"  → 前回値(¥{fallback:.0f})を保持します。")
            meta_status = "stale"

    # -----------------------------------------------------------------------
    # overall_status
    # -----------------------------------------------------------------------
    if btc_status == "failed":
        overall_status = "failed"
    elif any(s == "stale" for s in [usdjpy_status, mstr_status, meta_status]):
        overall_status = "partial"
    else:
        overall_status = "complete"

    # -----------------------------------------------------------------------
    # mNAV 計算
    # -----------------------------------------------------------------------
    mstr_cfg = companies_by_id.get("mstr", {})
    meta_cfg = companies_by_id.get("metaplanet", {})

    mstr_result = calc_company(
        mstr_cfg,
        btc_price=btc_usd_val, btc_price_key="btc_price_usd",
        stock_price=mstr_val,  stock_price_key="mstr_price_usd",
        usdjpy_val=usdjpy_val,
        btc_failed=(btc_status == "failed"),
        stock_status=mstr_status, usdjpy_status=usdjpy_status,
        btc_usd_as_of=btc_usd_as_of, stock_as_of=mstr_as_of,
    )
    meta_result = calc_company(
        meta_cfg,
        btc_price=btc_jpy_val, btc_price_key="btc_price_jpy",
        stock_price=meta_val,  stock_price_key="metaplanet_price_jpy",
        usdjpy_val=usdjpy_val,
        btc_failed=(btc_status == "failed"),
        stock_status=meta_status, usdjpy_status=usdjpy_status,
        btc_usd_as_of=btc_usd_as_of, stock_as_of=meta_as_of,
    )

    # -----------------------------------------------------------------------
    # comparison
    # -----------------------------------------------------------------------
    mstr_mnav = mstr_result["calc"].get("mnav_premium", {}).get("value")
    meta_mnav  = meta_result["calc"].get("mnav_premium", {}).get("value")
    spread_val = None
    if mstr_mnav is not None and meta_mnav is not None:
        spread_val = round(mstr_mnav - meta_mnav, 4)

    comparison = {
        "_comment": "mnav_premium は無次元(倍率)なので日米直接比較可能。",
        "mnav_premium_mstr":       mstr_mnav,
        "mnav_premium_metaplanet": meta_mnav,
        "spread": {
            "value":   spread_val,
            "unit":    "pt",
            "formula": "mstr - metaplanet",
            "note":    "mNAV倍率のポイント差(倍率同士の引き算)であって%ではない。例:MSTR 1.50x・メタプラ 1.20x なら 0.30pt。",
        },
    }

    # -----------------------------------------------------------------------
    # 出力
    # -----------------------------------------------------------------------
    ref_time = generated_at.replace(hour=8, minute=0, second=0, microsecond=0)
    output = {
        "_meta": {
            "schema_version": "1.0",
            "description": "暗号通貨トレジャリー企業 mNAV 算出結果。バッチが毎朝(JST 8時着地)生成。",
            "generated_at":        to_iso_jst(generated_at),
            "reference_time_jst":  to_iso_jst(ref_time),
            "reference_time_note": (
                "毎朝JST8時着地。BTC価格・為替・MSTR株価は当日の米国市場終値ベース、"
                "メタプラ株価は前日の東京終値ベース（時点ズレを明示）。"
            ),
            "overall_status": overall_status,
            "_status_vocabulary": {
                "overall_status":  "complete=全項目正常 / partial=一部項目が前回値保持で警告つき表示 / failed=BTC価格取得失敗等によりmNAV算出不可",
                "per_item_status": "ok=正常取得 / stale=取得失敗し前回値保持中 / failed=取得失敗かつ代用もしない(BTC価格のみ)",
                "company_status":  "ok=算出成功 / partial=非クリティカル項目がstaleだが算出は実行 / error=BTC価格failedにより算出不可",
                "retry_policy":    f"全レイヤー1項目は項目ごとに最大{MAX_RETRIES}回リトライ({RETRY_INTERVAL}秒間隔)。株価・為替はリトライ全滅でstale(前回値保持)。BTC価格のみfailed(代用せず算出不可)。",
            },
        },
        "market_data": {
            "_comment": "レイヤー1。バッチがAPI取得した実値。",
            "btc_price_usd": {
                "value": btc_usd_val, "as_of": btc_usd_as_of, "change_pct": btc_usd_chg,
                "status": btc_status, "source": "yfinance:BTC-USD",
                "_failure_policy": "リトライ全滅時は前回値代用なし。status=failed、両社calc=null、overall_status=failed。",
            },
            "usdjpy": {
                "value": usdjpy_val, "as_of": usdjpy_as_of, "change_pct": usdjpy_chg,
                "status": usdjpy_status, "source": "yfinance:USDJPY=X",
            },
            "btc_price_jpy": {
                "value": btc_jpy_val, "as_of": btc_usd_as_of,
                "status": btc_jpy_status, "note": "btc_price_usd × usdjpy で算出（派生値）。",
            },
            "mstr_price_usd": {
                "value": mstr_val, "as_of": mstr_as_of, "change_pct": mstr_chg,
                "status": mstr_status, "market": "US", "price_basis": "当日米国終値",
                "source": "yfinance:MSTR",
            },
            "metaplanet_price_jpy": {
                "value": meta_val, "as_of": meta_as_of, "change_pct": meta_chg,
                "status": meta_status, "market": "JP", "price_basis": "前日東京終値",
                "source": "yfinance:3350.T",
            },
        },
        "companies": [mstr_result, meta_result],
        "comparison": comparison,
        "alerts": {
            "_comment": "overall_status が complete 以外のとき UI に表示する。",
            "messages": alerts,
        },
    }

    save_json(DATA_PATH, output)
    label = {"complete": "OK", "partial": "WARN", "failed": "FAIL"}.get(overall_status, overall_status)
    print(f"[{label}] data.json 書き出し完了。overall_status={overall_status}  generated_at={to_iso_jst(generated_at)}")
    if alerts:
        print("--- alerts ---")
        for a in alerts:
            print(" ", a)


if __name__ == "__main__":
    build_data()
