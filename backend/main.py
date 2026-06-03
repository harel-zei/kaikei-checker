"""
会計データチェックシステム - FastAPI バックエンド
"""
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional
import json

from parsers.csv_parser import parse_csv, parse_trial_balance
from checkers.bs_checker import check_bs
from checkers.pl_checker import check_pl
from checkers.tax_checker import check_tax

app = FastAPI(title="会計データチェックシステム", version="1.1.0")

frontend_path = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


def _read_file(raw: bytes) -> str:
    """バイト列を文字コード自動判定してstr返す"""
    for enc in ["utf-8-sig", "utf-8", "shift_jis", "cp932"]:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("ファイルの文字コードを判定できませんでした")


@app.get("/", response_class=HTMLResponse)
async def root():
    return (frontend_path / "index.html").read_text(encoding="utf-8")


@app.post("/api/check")
async def check_accounting_data(
    file: UploadFile = File(...),
    trial_balance: Optional[UploadFile] = File(None),
):
    """
    仕訳CSVをアップロードして会計データチェックを実行
    trial_balance: 試算表CSV（期首残高取得用、任意）
    """
    # 仕訳CSV読み込み
    if not file.filename.lower().endswith((".csv", ".txt")):
        raise HTTPException(400, "CSVまたはTXTファイルをアップロードしてください")

    content = _read_file(await file.read())

    try:
        df, software = parse_csv(content)
    except Exception as e:
        raise HTTPException(400, f"CSVの解析に失敗しました: {e}")

    if df.empty:
        raise HTTPException(400, "データが読み込めませんでした。ファイルの形式を確認してください")

    # 期首残高（試算表CSV）
    opening_balances = {}
    if trial_balance and trial_balance.filename:
        try:
            tb_content = _read_file(await trial_balance.read())
            opening_balances = parse_trial_balance(tb_content)
        except Exception:
            pass  # 期首残高なしで継続

    # チェック実行
    all_issues = []
    all_issues.extend(check_bs(df, opening_balances))
    all_issues.extend(check_pl(df))
    all_issues.extend(check_tax(df))

    # 日付範囲
    valid_dates = df["date"].dropna()
    date_start = str(valid_dates.min().date()) if not valid_dates.empty else "不明"
    date_end   = str(valid_dates.max().date()) if not valid_dates.empty else "不明"

    software_labels = {
        "yayoi_raw": "弥生会計",
        "yayoi":     "弥生会計",
        "freee":     "freee",
        "moneyforward": "MoneyForward",
    }

    return JSONResponse({
        "summary": {
            "error":         len([i for i in all_issues if i["level"] == "error"]),
            "warning":       len([i for i in all_issues if i["level"] == "warning"]),
            "info":          len([i for i in all_issues if i["level"] == "info"]),
            "total_entries": len(df),
            "software":      software_labels.get(software, software),
            "has_opening_balances": bool(opening_balances),
            "date_range":    {"start": date_start, "end": date_end},
        },
        "issues": all_issues,
    })


@app.get("/api/sample")
async def get_sample_data():
    """サンプルデータでチェックを実行（デモ用）"""
    sample = '''"2111",1,"","R.07/04/01","消耗品費","","","課税仕入10%対価",5500,500,"普通預金","〇〇銀行","","対象外",5500,0,"文房具購入",,"",3,"","","0","0","no"
"2111",2,"","R.07/04/15","売掛金","A社","","対象外",110000,0,"製品売上高","","","課税売上10%",110000,0,"A社4月売上",,"",3,"","","0","0","no"
"2111",3,"","R.07/04/25","普通預金","〇〇銀行","","対象外",110000,0,"売掛金","A社","","対象外",110000,0,"A社入金",,"",3,"","","0","0","no"
"2111",4,"","R.07/05/01","仕入高","","","課税仕入10%対価",55000,5000,"買掛金","B社","","対象外",55000,0,"B社仕入5月分",,"",3,"","","0","0","no"
"2111",5,"","R.07/05/15","売掛金","A社","","対象外",220000,0,"製品売上高","","","課税売上10%",220000,0,"A社5月売上",,"",3,"","","0","0","no"
"2111",6,"","R.07/05/20","給料手当","","","対象外",300000,0,"普通預金","〇〇銀行","","対象外",300000,0,"5月給与",,"",3,"","","0","0","no"
"2111",7,"","R.07/05/25","買掛金","B社","","対象外",55000,0,"普通預金","〇〇銀行","","対象外",55000,0,"B社支払",,"",3,"","","0","0","no"
"2111",8,"","R.07/06/01","修繕費","","","課税仕入10%対価",650000,59090,"普通預金","〇〇銀行","","対象外",650000,0,"事務所修繕工事",,"",3,"","","0","0","no"
"2111",9,"","R.07/06/15","売掛金","A社","","対象外",55000,0,"製品売上高","","","課税売上10%",55000,0,"A社6月売上",,"",3,"","","0","0","no"
"2111",10,"","R.07/06/20","給料手当","","","対象外",300000,0,"普通預金","〇〇銀行","","対象外",300000,0,"6月給与",,"",3,"","","0","0","no"
"2111",11,"","R.07/07/01","仮払金","","","対象外",200000,0,"普通預金","〇〇銀行","","対象外",200000,0,"法人税支払",,"",3,"","","0","0","no"
"2111",12,"","R.07/07/15","売掛金","A社","","対象外",330000,0,"製品売上高","","","課税売上10%",330000,0,"A社7月売上",,"",3,"","","0","0","no"
"2111",13,"","R.07/07/20","給料手当","","","対象外",300000,0,"普通預金","〇〇銀行","","対象外",300000,0,"7月給与",,"",3,"","","0","0","no"
"2111",14,"","R.07/08/20","給料手当","","","対象外",600000,0,"普通預金","〇〇銀行","","対象外",600000,0,"8月給与（賞与含む）",,"",3,"","","0","0","no"
'''
    df, software = parse_csv(sample)
    all_issues = check_bs(df) + check_pl(df) + check_tax(df)
    valid_dates = df["date"].dropna()

    return JSONResponse({
        "summary": {
            "error":   len([i for i in all_issues if i["level"] == "error"]),
            "warning": len([i for i in all_issues if i["level"] == "warning"]),
            "info":    len([i for i in all_issues if i["level"] == "info"]),
            "total_entries": len(df),
            "software": "サンプルデータ（弥生形式）",
            "has_opening_balances": False,
            "date_range": {
                "start": str(valid_dates.min().date()) if not valid_dates.empty else "不明",
                "end":   str(valid_dates.max().date()) if not valid_dates.empty else "不明",
            },
        },
        "issues": all_issues,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
