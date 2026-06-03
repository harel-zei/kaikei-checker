"""
会計データチェックシステム - FastAPI バックエンド v1.2
"""
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional

from parsers.csv_parser import parse_csv, parse_opening_balances
from checkers.bs_checker import check_bs
from checkers.pl_checker import check_pl
from checkers.tax_checker import check_tax
from checkers.yoy_checker import check_yoy

app = FastAPI(title="会計データチェックシステム", version="1.2.0")
frontend_path = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


def _read(raw: bytes) -> str:
    for enc in ["utf-8-sig", "utf-8", "shift_jis", "cp932"]:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字コードを判定できませんでした")


@app.get("/", response_class=HTMLResponse)
async def root():
    return (frontend_path / "index.html").read_text(encoding="utf-8")


@app.post("/api/check")
async def check_accounting_data(
    file:            UploadFile = File(...),
    opening_balance: Optional[UploadFile] = File(None),  # 試算表/補助残高CSV
    prior_year:      Optional[UploadFile] = File(None),  # 前期仕訳帳CSV
):
    """
    仕訳CSVをアップロードして会計データチェックを実行。
    opening_balance: 期首残高CSV（試算表または補助残高一覧）― 任意
    prior_year:      前期仕訳帳CSV ― 任意（前年同月比較用）
    """
    # 仕訳CSV
    if not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(400, "CSVまたはTXTファイルをアップロードしてください")
    content = _read(await file.read())
    try:
        df, software = parse_csv(content)
    except Exception as e:
        raise HTTPException(400, f"CSVの解析に失敗: {e}")
    if df.empty:
        raise HTTPException(400, "データが読み込めませんでした")

    # 期首残高CSV（任意）
    ob: dict = {}
    if opening_balance and opening_balance.filename:
        try:
            ob = parse_opening_balances(_read(await opening_balance.read()))
        except Exception:
            pass

    # 前期仕訳帳CSV（任意）
    prior_df = None
    if prior_year and prior_year.filename:
        try:
            prior_content = _read(await prior_year.read())
            prior_df, _ = parse_csv(prior_content)
        except Exception:
            pass

    # チェック実行
    issues = []
    issues.extend(check_bs(df, ob))
    issues.extend(check_pl(df))
    issues.extend(check_tax(df))
    if prior_df is not None and not prior_df.empty:
        issues.extend(check_yoy(df, prior_df))

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
            "total_entries": len(df),
            "software":      sw_labels.get(software, software),
            "has_opening_balances": bool(ob),
            "has_prior_year":       prior_df is not None,
            "date_range": {
                "start": str(valid_dates.min().date()) if not valid_dates.empty else "不明",
                "end":   str(valid_dates.max().date()) if not valid_dates.empty else "不明",
            },
        },
        "issues": issues,
    })


@app.get("/api/sample")
async def get_sample_data():
    sample = '''"2111",1,"","R.07/04/01","消耗品費","","","課税仕入10%対価",5500,500,"普通預金","〇〇銀行","","対象外",5500,0,"文房具購入",,"",3,"","","0","0","no"
"2111",2,"","R.07/04/15","売掛金","A社","","対象外",110000,0,"製品売上高","","","課税売上10%",110000,0,"A社4月売上",,"",3,"","","0","0","no"
"2111",3,"","R.07/04/25","普通預金","〇〇銀行","","対象外",110000,0,"売掛金","A社","","対象外",110000,0,"A社入金",,"",3,"","","0","0","no"
"2111",4,"","R.07/05/01","仕入高","","","課税仕入10%対価",55000,5000,"買掛金","B社","","対象外",55000,0,"B社仕入5月分",,"",3,"","","0","0","no"
"2111",5,"","R.07/05/15","売掛金","A社","","対象外",220000,0,"製品売上高","","","課税売上10%",220000,0,"A社5月売上",,"",3,"","","0","0","no"
"2111",6,"","R.07/05/20","給料手当","","","対象外",300000,0,"普通預金","〇〇銀行","","対象外",300000,0,"5月給与",,"",3,"","","0","0","no"
"2111",7,"","R.07/06/01","修繕費","","","課税仕入10%対価",650000,59090,"普通預金","〇〇銀行","","対象外",650000,0,"事務所修繕工事",,"",3,"","","0","0","no"
"2111",8,"","R.07/07/01","仮払金","","","対象外",200000,0,"普通預金","〇〇銀行","","対象外",200000,0,"法人税支払",,"",3,"","","0","0","no"
"2111",9,"","R.07/08/20","給料手当","","","対象外",600000,0,"普通預金","〇〇銀行","","対象外",600000,0,"8月給与（賞与含む）",,"",3,"","","0","0","no"
'''
    df, _ = parse_csv(sample)
    issues = check_bs(df) + check_pl(df) + check_tax(df)
    valid_dates = df["date"].dropna()
    return JSONResponse({
        "summary": {
            "error":   len([i for i in issues if i["level"] == "error"]),
            "warning": len([i for i in issues if i["level"] == "warning"]),
            "info":    len([i for i in issues if i["level"] == "info"]),
            "total_entries": len(df),
            "software": "サンプルデータ（弥生形式）",
            "has_opening_balances": False,
            "has_prior_year": False,
            "date_range": {
                "start": str(valid_dates.min().date()) if not valid_dates.empty else "不明",
                "end":   str(valid_dates.max().date()) if not valid_dates.empty else "不明",
            },
        },
        "issues": issues,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
