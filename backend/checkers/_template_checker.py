"""
【チェッカー雛形（テンプレート）】

新しいチェックを作るときは、このファイルをコピーして使ってください。
　例）交際費のチェックを作る → このファイルを copy して
　　　backend/checkers/entertainment_checker.py という名前で保存

チェッカーの約束ごと（これさえ守れば全体に組み込めます）:
  1. 公開関数は  check_xxx(df) -> List[Dict]  の形にする
  2. 見つけた指摘は「issue（辞書）」を作って issues に追加し、最後に返す
  3. issue の辞書は下の EXAMPLE の形（キー）に合わせる
  4. df（仕訳データ）の列は下の「使える列」を参照

最後に backend/main.py の _checker_jobs に1行追加すると、実際に動きます（下部の手順参照）。
"""
import pandas as pd
from typing import List, Dict, Any

# 摘要・伝票番号・日付を安全に取り出す共通ヘルパー（そのまま使えます）
from checkers.check_utils import desc_safe, month_safe, date_safe, slip_safe


# ──────────────────────────────────────────────────────────
# df（仕訳データ）で使える主な列
#   date            … 日付（pandas の日時。月は df["date"].dt.to_period("M")）
#   debit_account   … 借方勘定科目（例「消耗品費」）
#   debit_sub       … 借方補助科目（例「A商事」）
#   debit_amount    … 借方金額（数値）
#   debit_tax       … 借方税区分（例「課対仕入10%」）
#   credit_account  … 貸方勘定科目
#   credit_sub      … 貸方補助科目
#   credit_amount   … 貸方金額（数値）
#   credit_tax      … 貸方税区分
#   description     … 摘要（メモ書き）
#   slip_no         … 伝票番号（仕訳番号）
# ──────────────────────────────────────────────────────────


# ここに「判定に使う設定値」をまとめて書いておくと、あとで直しやすいです
TARGET_ACCOUNTS = ["交際費", "接待交際費"]   # ← 対象にしたい勘定科目（部分一致）
THRESHOLD = 50_000                          # ← しきい値の例（5万円）


def check_template(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    このチェッカーの入口。複数の観点があるときは _check_x_y に分けて呼び出す。
    （まずは1つでOK。慣れてきたら分割してください）
    """
    issues: List[Dict[str, Any]] = []
    issues.extend(_check_example(df))
    return issues


def _check_example(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    例）対象科目（交際費など）に、しきい値以上の計上があれば「確認してください」と出す。
    ※ これはあくまでサンプルです。実際の判定条件に置き換えてください。
    """
    issues: List[Dict[str, Any]] = []

    # ① 対象の行だけに絞り込む（借方勘定科目が対象科目を含む行）
    #    fillna("").astype(str) で欠損・数値まじりでも安全に文字列比較できます
    mask = df["debit_account"].fillna("").astype(str).apply(
        lambda x: any(a in x for a in TARGET_ACCOUNTS)
    )
    target = df[mask & (df["debit_amount"] >= THRESHOLD)]

    # ② 該当する行ごとに issue（指摘）を作る
    for _, row in target.iterrows():
        d = desc_safe(row)                       # 摘要（無ければ ""）
        desc_part = f"（摘要: {d}）" if d else ""

        issues.append({
            # level は3種類:  "error"=要修正(高) / "warning"=要確認(中) / "info"=参考(低)
            "level":    "warning",
            "category": "T-1 交際費チェック（例）",  # 画面での分類名
            "check_id": "T-1",                       # チェック番号（重複しない番号を付ける）
            "account":  str(row["debit_account"]),   # 関係する勘定科目
            "month":    date_safe(row),              # 日付（YYYY-MM-DD）。月だけなら month_safe(row)
            "slip":     slip_safe(row),              # 伝票番号（無ければ ""）
            "message": (                             # 担当者が読む説明文
                f"【T-1・中】「{row['debit_account']}」に "
                f"{row['debit_amount']:,.0f}円 の計上があります{desc_part}。"
                "内容をご確認ください。"
            ),
        })

    return issues


# ──────────────────────────────────────────────────────────
# 【組み込み手順】この雛形を実際に動かすには（コピーして名前を変えた後）
#
# 1. 関数名を分かりやすく変える
#      check_template → check_entertainment など
#
# 2. backend/main.py の先頭の import に1行追加
#      from checkers.entertainment_checker import check_entertainment
#
# 3. backend/main.py の _checker_jobs リストに1行追加
#      ("交際費", lambda: check_entertainment(df_checked)),
#    （↑ ここに入れると、除外科目・対象期間が反映された df で自動的に呼ばれます）
#
# 4. 動作確認（合成データで）
#      - 実データは使わず、ダミーの仕訳で「意図どおり指摘が出るか」を確認する
#      - Claude に「このチェッカーを合成データでテストして」と頼むのが簡単です
#
# 5. ブランチ → PR → レビュー → マージ（CONTRIBUTING.md 参照）
# ──────────────────────────────────────────────────────────
