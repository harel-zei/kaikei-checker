"""
会計データチェックシステム - FastAPI バックエンド v1.5
クライアント管理機能追加
"""
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Request, Body
from fastapi.responses import StreamingResponse
import io, csv as csv_module
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from typing import List, Optional

from parsers.csv_parser import (
    parse_csv, parse_opening_balances, parse_ending_balances,
    parse_opening_from_ledger, _is_ledger_format,
)
from parsers.file_detector import auto_classify_files
from checkers.bs_checker import check_bs, estimate_last_complete_month
from checkers.pl_checker import check_pl
from checkers.tax_checker import check_tax
from checkers.yoy_checker import check_yoy
from checkers.completeness_checker import check_completeness
from checkers.tax_detail_checker import check_tax_detail
from checkers.asset_checker import check_assets
from checkers.ar_ap_checker import check_ar_ap
from checkers.governance_checker import check_governance
from client_store import (
    list_clients, save_prior_files, load_prior_files,
    get_client_info, delete_prior_file, delete_client,
    get_client_settings, save_client_settings,
)

app = FastAPI(title="会計データチェックシステム", version="1.5.0")
frontend_path = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


def _read(raw: bytes) -> str:
    for enc in ["utf-8-sig", "utf-8", "shift_jis", "cp932"]:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判定できませんでした")


def _parse_balance_file(content: str, use_ending: bool = False) -> dict:
    """
    残高ファイルを適切な方法でパースする。
    補助元帳形式（[前期繰越行]）→ parse_opening_from_ledger
    試算表/補助残高一覧形式（[明細行]）→ parse_ending_balances or parse_opening_balances
    """
    if not content:
        return {}
    if _is_ledger_format(content):
        return parse_opening_from_ledger(content)
    elif use_ending:
        return parse_ending_balances(content)
    else:
        return parse_opening_balances(content)


def _merge_balances(*dicts) -> dict:
    merged = {}
    for d in dicts:
        if d:
            merged.update(d)
    return merged


@app.get("/", response_class=HTMLResponse)
async def root():
    return (frontend_path / "index.html").read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════
# クライアント管理 API
# ═══════════════════════════════════════════════════

@app.get("/api/clients")
async def api_list_clients():
    """保存済みクライアント一覧を返す"""
    return JSONResponse(list_clients())


@app.get("/api/clients/{client_name}")
async def api_get_client(client_name: str):
    """クライアントの保存情報を返す"""
    info = get_client_info(client_name)
    if info is None:
        raise HTTPException(404, f"クライアント '{client_name}' は見つかりません")
    return JSONResponse(info)


@app.post("/api/clients/{client_name}/save-prior")
async def api_save_prior(
    client_name: str,
    files: List[UploadFile] = File(...),
):
    """前期ファイルを自動判定してクライアントに保存する"""
    if not files:
        raise HTTPException(400, "ファイルを選択してください")

    file_data = []
    for f in files:
        try:
            file_data.append((f.filename, _read(await f.read())))
        except Exception as e:
            raise HTTPException(400, f"{f.filename} の読み込みに失敗: {e}")

    classified = auto_classify_files(file_data)

    def _fname(keyword, default):
        return next((n for n, _ in file_data if keyword in n), default)

    to_save = {}
    j = classified.get("journal_prior") or classified.get("journal_current")
    if j:
        to_save["prior_journal"] = (_fname("仕訳", "仕訳.txt"), j)

    bm = classified.get("balance_main_prior") or classified.get("balance_main_current")
    if bm:
        to_save["prior_bal_main"] = (_fname("残高", "残高.txt"), bm)

    bs = classified.get("balance_sub_prior") or classified.get("balance_sub_current")
    if bs:
        to_save["prior_bal_sub"] = (_fname("補助", "補助残高.txt"), bs)
    elif classified.get("balance_sub_current"):
        to_save["prior_bal_sub"] = (
            next((n for n, _ in file_data if "補助" in n), "補助残高.txt"),
            classified["balance_sub_current"]
        )

    if not to_save:
        raise HTTPException(400, "保存できるファイルが見つかりませんでした")

    result = save_prior_files(client_name, to_save)
    result["log"] = classified.get("log", [])
    return JSONResponse(result)


@app.delete("/api/clients/{client_name}/prior/{file_key}")
async def api_delete_prior_file(client_name: str, file_key: str):
    """特定の前期ファイルを削除する"""
    if delete_prior_file(client_name, file_key):
        return JSONResponse({"status": "deleted"})
    raise HTTPException(404, "ファイルが見つかりません")


@app.delete("/api/clients/{client_name}")
async def api_delete_client(client_name: str):
    """クライアントのデータをすべて削除する"""
    if delete_client(client_name):
        return JSONResponse({"status": "deleted"})
    raise HTTPException(404, "クライアントが見つかりません")


@app.get("/api/clients/{client_name}/settings")
async def api_get_settings(client_name: str):
    """クライアントのチェック設定を取得"""
    return JSONResponse(get_client_settings(client_name))


@app.post("/api/clients/{client_name}/settings")
async def api_save_settings(client_name: str, request: Request):
    """クライアントのチェック設定を保存"""
    body = await request.json()
    saved = save_client_settings(client_name, body)
    return JSONResponse({"status": "ok", "settings": saved})


# ── 自動振り分けエンドポイント ─────────────────────────────
@app.post("/api/check-auto")
async def check_auto(
    files:       List[UploadFile] = File(...),
    client_name: Optional[str]    = Form(None),
    check_until: Optional[str]    = Form(None),  # "2026-05" のような YYYY-MM 形式
):
    """
    1〜6ファイルをまとめて受け取り、自動判定して振り分けた上でチェックを実行する。
    client_name を指定すると、前期ファイルが未アップロードの場合に保存済みデータを自動補完する。
    """
    if not files:
        raise HTTPException(400, "ファイルを1つ以上アップロードしてください")
    if len(files) > 6:
        raise HTTPException(400, "アップロードできるファイルは最大6つです")

    file_data = []
    for f in files:
        try:
            file_data.append((f.filename, _read(await f.read())))
        except Exception as e:
            raise HTTPException(400, f"{f.filename} の読み込みに失敗しました: {e}")

    classified = auto_classify_files(file_data)

    # 前期データが不足している場合、保存済みクライアントデータで補完
    if client_name:
        stored = load_prior_files(client_name)
        filled = []
        for key in ["journal_prior", "balance_main_prior", "balance_sub_prior"]:
            store_key = key.replace("journal_prior", "prior_journal") \
                           .replace("balance_main_prior", "prior_bal_main") \
                           .replace("balance_sub_prior", "prior_bal_sub")
            if not classified.get(key) and stored.get(store_key):
                classified[key] = stored[store_key]
                filled.append(key)
        if filled:
            classified["log"].append(
                f"💾 クライアント「{client_name}」の保存済み前期データを自動読み込み: "
                + ", ".join(filled)
            )

    if not classified.get("journal_current"):
        raise HTTPException(400,
            "仕訳帳ファイルが見つかりませんでした。"
            "弥生会計の仕訳帳CSVを含めてください。"
        )

    # クライアント設定（除外科目）を読み込む
    client_settings = get_client_settings(client_name) if client_name else {}

    return await _run_checks(classified, check_until=check_until, client_settings=client_settings)


# ── 個別指定エンドポイント（従来の6スロット）──────────────────
@app.post("/api/check")
async def check_manual(
    file:           UploadFile        = File(...),
    ob_main:        Optional[UploadFile] = File(None),
    ob_sub:         Optional[UploadFile] = File(None),
    prior_journal:  Optional[UploadFile] = File(None),
    prior_bal_main: Optional[UploadFile] = File(None),
    prior_bal_sub:  Optional[UploadFile] = File(None),
):
    async def _opt(up):
        if not up or not up.filename:
            return None
        try:
            return _read(await up.read())
        except Exception:
            return None

    classified = {
        "journal_current":      _read(await file.read()),
        "balance_main_current": await _opt(ob_main),
        "balance_sub_current":  await _opt(ob_sub),
        "journal_prior":        await _opt(prior_journal),
        "balance_main_prior":   await _opt(prior_bal_main),
        "balance_sub_prior":    await _opt(prior_bal_sub),
        "log": ["手動指定モード"],
    }
    return await _run_checks(classified)


# ── チェック実行（共通処理）──────────────────────────────────
async def _run_checks(
    c: dict,
    check_until: Optional[str] = None,
    client_settings: dict = None,
) -> JSONResponse:
    # 当期仕訳帳
    try:
        df, software = parse_csv(c["journal_current"])
    except Exception as e:
        raise HTTPException(400, f"仕訳帳の解析に失敗しました: {e}")
    if df.empty:
        raise HTTPException(400, "仕訳帳からデータを読み込めませんでした")

    # ── 当期期首残高の導出 ──────────────────────────────────────────
    # 優先順位:
    # 1. 当期首残高ファイルが直接アップロードされていればそれを使う
    # 2. なければ、前期の試算表/補助残高の「期末残高」列から自動導出
    #    （前期末残高 = 当期首残高）
    ob_from_current = _merge_balances(
        _parse_balance_file(c["balance_main_current"]) if c.get("balance_main_current") else {},
        _parse_balance_file(c["balance_sub_current"])  if c.get("balance_sub_current")  else {},
    )
    ob_from_prior = _merge_balances(
        _parse_balance_file(c["balance_main_prior"], use_ending=True) if c.get("balance_main_prior") else {},
        _parse_balance_file(c["balance_sub_prior"],  use_ending=True) if c.get("balance_sub_prior")  else {},
    )
    # 当期首ファイルを優先、なければ前期末から導出
    ob = ob_from_current if ob_from_current else ob_from_prior
    ob_source = "当期首ファイル" if ob_from_current else ("前期末から自動導出" if ob_from_prior else "なし")

    # 前期仕訳帳
    prior_df = None
    if c.get("journal_prior"):
        try:
            prior_df, _ = parse_csv(c["journal_prior"])
            if prior_df.empty:
                prior_df = None
        except Exception:
            prior_df = None

    # 前期首残高（YoY比較用）: 前期試算表の「前期繰越」列から取得
    prior_ob = _merge_balances(
        _parse_balance_file(c["balance_main_prior"]) if c.get("balance_main_prior") else {},
        _parse_balance_file(c["balance_sub_prior"])  if c.get("balance_sub_prior")  else {},
    )

    # ── チェック対象期間の決定 ──
    last_month = estimate_last_complete_month(df)
    if check_until:
        try:
            import pandas as pd
            # "2026-05-20" (日付) または "2026-05" (月) の両形式に対応
            specified_ts = pd.Timestamp(check_until)
            specified_period = specified_ts.to_period("M")
            if specified_period <= last_month:
                last_month = specified_period
                # 日付指定の場合はdfもその日付以下に絞る
                df = df[df["date"] <= specified_ts].copy()
        except Exception:
            pass  # 不正な形式は無視

    # ── 除外科目・締め日の設定 ──
    exclude_accounts   = (client_settings or {}).get("exclude_accounts", [])
    fiscal_cutoff_day  = int((client_settings or {}).get("fiscal_cutoff_day", 1))
    issues = []
    issues.extend(check_bs(df, ob, exclude_accounts=exclude_accounts))
    issues.extend(check_pl(df))
    issues.extend(check_tax(df))
    # 追加チェック（カテゴリ1〜5）
    # last_month までのデータのみ対象
    df_current = df[df["date"].dt.to_period("M") <= last_month].copy() if not df.empty else df
    # 除外科目の行を除いたDataFrame
    if exclude_accounts:
        excl_mask = df_current["debit_account"].astype(str).apply(
            lambda x: any(e in x for e in exclude_accounts)
        ) | df_current["credit_account"].astype(str).apply(
            lambda x: any(e in x for e in exclude_accounts)
        )
        df_checked = df_current[~excl_mask].copy()
    else:
        df_checked = df_current

    try: issues.extend(check_completeness(df_checked, fiscal_cutoff_day))
    except Exception: pass
    try: issues.extend(check_tax_detail(df_checked))
    except Exception: pass
    try: issues.extend(check_assets(df_checked))
    except Exception: pass
    try: issues.extend(check_ar_ap(df_checked))
    except Exception: pass
    try: issues.extend(check_governance(df_checked))
    except Exception: pass
    if prior_df is not None:
        issues.extend(check_yoy(df, prior_df, prior_ob or None, last_month, ob or None))

    valid_dates = df["date"].dropna()
    sw_labels = {
        "yayoi_raw": "弥生会計", "yayoi": "弥生会計",
        "freee": "freee", "moneyforward": "MoneyForward",
    }
    if ob:
        c.setdefault("log", []).append(f"📊 当期首残高: {len(ob)}科目（{ob_source}）")
    if exclude_accounts:
        c.setdefault("log", []).append(f"🚫 チェック除外科目: {', '.join(exclude_accounts)}")
    if check_until:
        c.setdefault("log", []).append(f"📅 チェック対象期間: 〜{last_month}")

    return JSONResponse({
        "summary": {
            "error":   len([i for i in issues if i["level"] == "error"]),
            "warning": len([i for i in issues if i["level"] == "warning"]),
            "info":    len([i for i in issues if i["level"] == "info"]),
            "total_entries":     len(df),
            "software":          sw_labels.get(software, software),
            "has_ob":            bool(ob),
            "ob_source":         ob_source,
            "ob_accounts":       len(ob),
            "check_until":        str(last_month),
            "check_until_input":  check_until or "",   # ユーザー指定値（表示用）
            "exclude_accounts":   exclude_accounts,
            "has_prior_journal": prior_df is not None,
            "has_prior_bal":     bool(prior_ob),
            "date_range": {
                "start": str(valid_dates.min().date()) if not valid_dates.empty else "不明",
                "end":   str(valid_dates.max().date()) if not valid_dates.empty else "不明",
            },
            "classification_log": c.get("log", []),
        },
        "issues": issues,
    })


@app.post("/api/export-csv")
async def export_csv(request: Request):
    """
    チェック結果をExcelフォーマット（チェックシート形式）のCSVとして出力する。

    フォーマット（記入例シートに準拠）:
      行1: 会計データ確認シート
      行2: 対象月：YYYY年MM月分　　担当：
      行3: 勘定科目, 補助科目, 日付, 確認内容（Harel入力欄）, ご回答, 対応済, 備考
      行4+: チェック結果（同じ勘定科目は最初の行のみ表示）
    """
    body      = await request.json()
    issues    = body.get("issues", [])
    period    = body.get("period", "")      # "2026年5月分" 等
    client    = body.get("client_name", "")

    output = io.StringIO()
    writer = csv_module.writer(output)

    # タイトル行
    writer.writerow(["会計データ確認シート"])
    writer.writerow([f"対象月：{period}　　　　　　　　　　　　　　担当："])
    writer.writerow([])
    # 列ヘッダー
    writer.writerow(["勘定科目", "補助科目", "日付", "確認内容（Harel入力欄）", "ご回答", "対応済", "備考"])

    prev_account_key = None  # 前の行の親科目（同一科目は空白にする）

    for issue in issues:
        full_account = str(issue.get("account", ""))
        month        = str(issue.get("month", ""))
        message      = str(issue.get("message", ""))

        # 「勘定科目（補助科目）」形式を分解
        if "（" in full_account and full_account.endswith("）"):
            paren_pos   = full_account.index("（")
            base_acc    = full_account[:paren_pos]
            sub_acc     = full_account[paren_pos+1:-1]
        else:
            base_acc = full_account
            sub_acc  = ""

        # 同一勘定科目は2行目以降を空白に
        display_acc = base_acc if base_acc != prev_account_key else ""
        if base_acc:
            prev_account_key = base_acc

        # 日付列: 「YYYY-MM」形式 → 「YYYY年MM月」 / 「全期間」「不明」はそのまま
        date_str = month
        if len(month) == 7 and month[4] == "-":
            y, m = month.split("-")
            date_str = f"{y}年{int(m)}月"

        # メッセージから【〇〇】プレフィックスを除去してすっきりさせる
        import re
        clean_msg = re.sub(r'^【[^】]+】', '', message).strip()

        writer.writerow([display_acc, sub_acc, date_str, clean_msg, "", "", ""])

    csv_str = output.getvalue()

    # BOM付きUTF-8（Excelで文字化けしないよう）
    csv_bytes = b'\xef\xbb\xbf' + csv_str.encode("utf-8")

    filename = f"チェックシート_{client}_{period}.csv".replace(" ", "_").replace("　", "_")

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_url_encode(filename)}"}
    )


def _url_encode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


@app.get("/api/sample")
async def get_sample_data():
    sample = '''"2111",1,"","R.07/04/01","消耗品費","","","課税仕入10%対価",5500,500,"普通預金","〇〇銀行","","対象外",5500,0,"文房具購入",,"",3,"","","0","0","no"
"2111",2,"","R.07/04/15","売掛金","A社","","対象外",110000,0,"製品売上高","","","課税売上10%",110000,0,"A社4月売上",,"",3,"","","0","0","no"
"2111",3,"","R.07/04/25","普通預金","〇〇銀行","","対象外",110000,0,"売掛金","A社","","対象外",110000,0,"A社入金",,"",3,"","","0","0","no"
"2111",4,"","R.07/07/01","仮払金","","","対象外",200000,0,"普通預金","〇〇銀行","","対象外",200000,0,"法人税支払",,"",3,"","","0","0","no"
"2111",5,"","R.07/06/01","修繕費","","","課税仕入10%対価",650000,59090,"普通預金","〇〇銀行","","対象外",650000,0,"事務所修繕工事",,"",3,"","","0","0","no"
'''
    df, _ = parse_csv(sample)
    issues = check_bs(df) + check_pl(df) + check_tax(df)
    valid_dates = df["date"].dropna()
    return JSONResponse({
        "summary": {
            "error":   len([i for i in issues if i["level"] == "error"]),
            "warning": len([i for i in issues if i["level"] == "warning"]),
            "info":    len([i for i in issues if i["level"] == "info"]),
            "total_entries": len(df), "software": "サンプルデータ（弥生形式）",
            "has_ob": False, "ob_accounts": 0,
            "has_prior_journal": False, "has_prior_bal": False,
            "date_range": {
                "start": str(valid_dates.min().date()) if not valid_dates.empty else "不明",
                "end":   str(valid_dates.max().date()) if not valid_dates.empty else "不明",
            },
            "classification_log": ["サンプルデータ"],
        },
        "issues": issues,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
