"""
会計データチェックシステム - FastAPI バックエンド
"""
import io
import json
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import pandas as pd

from parsers.csv_parser import parse_csv
from checkers.bs_checker import check_bs
from checkers.pl_checker import check_pl
from checkers.tax_checker import check_tax
from reports.monthly_report import build_monthly_report

app = FastAPI(title="会計データチェックシステム", version="1.0.0")

# フロントエンドを静的ファイルとして配信
frontend_path = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    index_file = frontend_path / "index.html"
    return index_file.read_text(encoding="utf-8")


@app.post("/api/check")
async def check_accounting_data(file: UploadFile = File(...)):
    """CSVファイルをアップロードして会計データチェックを実行"""

    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="CSVファイルをアップロードしてください")

    # ファイル読み込み（文字コード自動判定）
    raw = await file.read()
    content = None
    for encoding in ["utf-8-sig", "utf-8", "shift_jis", "cp932"]:
        try:
            content = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if content is None:
        raise HTTPException(status_code=400, detail="ファイルの文字コードを判定できませんでした")

    # パース
    try:
        df, software = parse_csv(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"CSVの解析に失敗しました: {str(e)}")

    if df.empty:
        raise HTTPException(status_code=400, detail="データが読み込めませんでした。CSVの形式を確認してください")

    # チェック実行
    all_issues = []
    all_issues.extend(check_bs(df))
    all_issues.extend(check_pl(df))
    all_issues.extend(check_tax(df))

    # サマリー集計
    summary = {
        "error": len([i for i in all_issues if i["level"] == "error"]),
        "warning": len([i for i in all_issues if i["level"] == "warning"]),
        "info": len([i for i in all_issues if i["level"] == "info"]),
        "total_entries": len(df),
        "software": software,
        "date_range": {
            "start": str(df["date"].dropna().min().date()) if not df["date"].dropna().empty else "不明",
            "end": str(df["date"].dropna().max().date()) if not df["date"].dropna().empty else "不明",
        }
    }

    return JSONResponse({
        "summary": summary,
        "issues": all_issues,
        "monthly_report": build_monthly_report(df),
    })


@app.get("/api/sample")
async def get_sample_data():
    """サンプルデータでチェックを実行（デモ用）"""
    sample_csv = """日付,借方勘定科目,借方補助科目,借方税区分,借方金額,貸方勘定科目,貸方補助科目,貸方税区分,貸方金額,摘要
2024/04/01,消耗品費,,課税10%,5500,現金,,,5500,文房具購入
2024/04/15,売掛金,A社,,110000,売上高,,,110000,A社売上4月分
2024/04/25,普通預金,,,110000,売掛金,A社,,110000,A社入金
2024/05/01,仕入高,,課税10%,55000,買掛金,B社,,55000,B社仕入5月分
2024/05/15,売掛金,A社,,220000,売上高,,,220000,A社売上5月分
2024/05/20,給与,,対象外,300000,普通預金,,,300000,5月給与
2024/05/25,買掛金,B社,,55000,普通預金,,,55000,B社支払
2024/06/01,修繕費,,課税10%,650000,普通預金,,,650000,事務所修繕工事
2024/06/15,売掛金,A社,,55000,売上高,,,55000,A社売上6月分
2024/06/20,給与,,対象外,300000,普通預金,,,300000,6月給与
2024/07/01,仮払金,,,200000,普通預金,,,200000,法人税支払
2024/07/15,売掛金,A社,,330000,売上高,,,330000,A社売上7月分
2024/07/20,給与,,対象外,300000,普通預金,,,300000,7月給与
2024/08/01,雑費,,課税10%,3000,現金,,,3000,雑費
2024/08/05,雑費,,課税10%,5000,現金,,,5000,雑費
2024/08/10,雑費,,課税10%,2000,現金,,,2000,雑費
2024/08/15,売掛金,A社,,110000,売上高,,,110000,A社売上8月分
2024/08/20,給与,,対象外,600000,普通預金,,,600000,8月給与（賞与含む）
"""

    df, software = parse_csv(sample_csv)
    all_issues = []
    all_issues.extend(check_bs(df))
    all_issues.extend(check_pl(df))
    all_issues.extend(check_tax(df))

    summary = {
        "error": len([i for i in all_issues if i["level"] == "error"]),
        "warning": len([i for i in all_issues if i["level"] == "warning"]),
        "info": len([i for i in all_issues if i["level"] == "info"]),
        "total_entries": len(df),
        "software": "サンプルデータ（弥生形式）",
        "date_range": {
            "start": str(df["date"].dropna().min().date()),
            "end": str(df["date"].dropna().max().date()),
        }
    }

    return JSONResponse({
        "summary": summary,
        "issues": all_issues,
        "monthly_report": build_monthly_report(df),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
