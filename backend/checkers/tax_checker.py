"""
消費税区分チェックモジュール
インボイス制度対応含む
"""
import pandas as pd
from typing import List, Dict, Any

# 資産・負債科目（基本的に消費税「対象外」であるべき）
NON_TAXABLE_ACCOUNTS = [
    "現金", "普通預金", "当座預金", "定期預金",
    "売掛金", "買掛金", "未払金", "未収入金",
    "短期借入金", "長期借入金",
    "給与", "賃金", "役員報酬",
    "社会保険料", "労働保険料",
    "源泉所得税", "住民税",
]

# 軽減税率が適用される可能性のある科目
REDUCED_TAX_ACCOUNTS = ["福利厚生費", "会議費", "交際費"]

# 課税取引であるべき主要科目
TAXABLE_ACCOUNTS = [
    "消耗品費", "事務用品費", "通信費", "水道光熱費",
    "修繕費", "広告宣伝費", "賃借料", "リース料",
]


def check_tax(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """消費税区分チェックを実行して指摘事項リストを返す"""
    issues = []

    if "debit_tax" not in df.columns and "credit_tax" not in df.columns:
        issues.append({
            "level": "info",
            "category": "消費税",
            "account": "全科目",
            "month": "全期間",
            "message": "CSVに税区分情報が含まれていません。会計ソフトから「科目別税区分表」を別途出力してチェックすることをお勧めします。",
        })
        return issues

    issues.extend(_check_non_taxable_accounts(df))
    issues.extend(_check_invoice_system(df))
    # 海外出張チェックは tax_detail_checker の 2-6（海外渡航費）に一本化
    # （両方で実行すると同じ仕訳が二重に指摘されるため）

    return issues


def _check_non_taxable_accounts(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """資産・負債科目に課税区分が設定されていないか確認（借方・貸方の両側）"""
    issues = []

    sides = []
    if "debit_tax" in df.columns:
        sides.append(("debit_account", "debit_tax", "借方"))
    if "credit_tax" in df.columns:
        sides.append(("credit_account", "credit_tax", "貸方"))

    for account in NON_TAXABLE_ACCOUNTS:
        for acc_col, tax_col, side_label in sides:
            entries = df[df[acc_col].astype(str).str.contains(account, na=False)]
            if entries.empty:
                continue
            taxed = entries[
                entries[tax_col].astype(str).str.contains(r"課税|10%|8%", na=False)
            ]
            if not taxed.empty:
                issues.append({
                    "level": "error",
                    "category": "消費税",
                    "account": account,
                    "month": "全期間",
                    "message": f"【要修正】{account}（{side_label}）に課税区分が設定されている仕訳が {len(taxed)}件 あります。資産・負債科目は基本的に「対象外」とすべきです。",
                })

    return issues


def _check_invoice_system(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """インボイス制度対応チェック（T番号未取得業者への支払い）"""
    issues = []

    if "description" not in df.columns:
        return issues

    # 摘要に「T番号なし」「適格請求書なし」などの記載を確認
    non_invoice_entries = df[
        df.get("description", pd.Series(dtype=str)).astype(str).str.contains(
            r"T番号なし|適格外|区分記載|経過措置", na=False
        )
    ]

    if not non_invoice_entries.empty:
        # 経過措置の税区分が適切か確認
        if "debit_tax" in df.columns:
            wrong_tax = non_invoice_entries[
                ~non_invoice_entries["debit_tax"].astype(str).str.contains(
                    r"経過措置|区分記載|80%|50%", na=False
                )
            ]
            if not wrong_tax.empty:
                issues.append({
                    "level": "error",
                    "category": "消費税",
                    "account": "仕入・外注費等",
                    "month": "全期間",
                    "message": f"【要修正】インボイス未登録事業者への支払いで経過措置が適用されていない可能性のある仕訳が {len(wrong_tax)}件 あります。「区分記載入力8%（経過措置）」等の税区分に修正してください。",
                })

    return issues


# 海外出張費のチェックは tax_detail_checker の 2-6（海外渡航費）に一本化済み
