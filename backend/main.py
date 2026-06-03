"""
会計データチェックシステム - FastAPI バックエンド v1.4
6ファイル一括アップロード＆自動振り分け対応
"""
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from typing import List, Optional

from parsers.csv_parser import parse_csv, parse_opening_balances
from parsers.file_detector import auto_classify_files
from checkers.bs_checker import check_bs, estimate_last_complete_month
from checkers.pl_checker import check_pl
from checkers.tax_checker import check_tax
from checkers.yoy_checker import check_yoy

app = FastAPI(title="会計データチェックシステム", version="1.4.0")
frontend_path = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


def _read(raw: bytes) -> str:
    for enc in ["utf-8-sig", "utf-8", "shift_jis", "cp932"]:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判定できませんでした")


def _merge_balances(*dicts) -> dict:
    merged = {}
    for d in dicts:
        if d:
            merged.update(d)
    return merged


@app.get("/", response_class=HTMLResponse)
async def root():
    return (frontend_path / "index.html").read_text(encoding="utf-8")


# ── 自動振り分けエンドポイント ─────────────────────────────
@app.post("/api/check-auto")
async def check_auto(files: List[UploadFile] = File(...)):
    """
    1〜6ファイルをまとめて受け取り、自動判定して振り分けた上でチェックを実行する。
    """
    if not files:
        raise HTTPException(400, "ファイルを1つ以上アップロードしてください")
    if len(files) > 6:
        raise HTTPException(400, "アップロードできるファイルは最大6つです")

    # 読み込み
    file_data = []
    for f in files:
        try:
            content = _read(await f.read())
            file_data.append((f.filename, content))
        except Exception as e:
            raise HTTPException(400, f"{f.filename} の読み込みに失敗しました: {e}")

    # 自動振り分け
    classified = auto_classify_files(file_data)

    # 仕訳帳（当期）は必須
    if not classified["journal_current"]:
        raise HTTPException(400,
            "仕訳帳ファイルが見つかりませんでした。"
            "弥生会計の仕訳帳CSVを含めてください。\n"
            f"振り分け結果: {chr(10).join(classified['log'])}"
        )

    return await _run_checks(classified)


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
async def _run_checks(c: dict) -> JSONResponse:
    # 当期仕訳帳
    try:
        df, software = parse_csv(c["journal_current"])
    except Exception as e:
        raise HTTPException(400, f"仕訳帳の解析に失敗しました: {e}")
    if df.empty:
        raise HTTPException(400, "仕訳帳からデータを読み込めませんでした")

    # 当期期首残高（主科目 + 補助をマージ）
    ob = _merge_balances(
        parse_opening_balances(c["balance_main_current"]) if c.get("balance_main_current") else {},
        parse_opening_balances(c["balance_sub_current"])  if c.get("balance_sub_current")  else {},
    )

    # 前期仕訳帳
    prior_df = None
    if c.get("journal_prior"):
        try:
            prior_df, _ = parse_csv(c["journal_prior"])
            if prior_df.empty:
                prior_df = None
        except Exception:
            prior_df = None

    # 前期首残高（主科目 + 補助をマージ）
    prior_ob = _merge_balances(
        parse_opening_balances(c["balance_main_prior"]) if c.get("balance_main_prior") else {},
        parse_opening_balances(c["balance_sub_prior"])  if c.get("balance_sub_prior")  else {},
    )

    # チェック実行
    last_month = estimate_last_complete_month(df)
    issues = []
    issues.extend(check_bs(df, ob))
    issues.extend(check_pl(df))
    issues.extend(check_tax(df))
    if prior_df is not None:
        issues.extend(check_yoy(df, prior_df, prior_ob or None, last_month))

    valid_dates = df["date"].dropna()
    sw_labels = {
        "yayoi_raw": "弥生会計", "yayoi": "弥生会計",
        "freee": "freee", "moneyforward": "MoneyForward",
    }

    return JSONResponse({
        "summary": {
            "error":   len([i for i in issues if i["level"] == "error"]),
            "warning": len([i for i in issues if i["level"] == "warning"]),
            "info":    len([i for i in issues if i["level"] == "info"]),
            "total_entries":     len(df),
            "software":          sw_labels.get(software, software),
            "has_ob":            bool(ob),
            "ob_accounts":       len(ob),
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
