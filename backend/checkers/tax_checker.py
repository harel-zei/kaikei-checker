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
    issues.extend(_check_overseas_travel(df))

    return issues


def _check_non_taxable_accounts(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """資産・負債科目に課税区分が設定されていないか確認"""
    issues = []

    for account in NON_TAXABLE_ACCOUNTS:
        # 借方チェック
        entries = df[
            df["debit_account"].astype(str).str.contains(account, na=False)
        ]

        if "debit_tax" in df.columns:
            taxed = entries[
                entries["debit_tax"].astype(str).str.contains(r"課税|10%|8%", na=False)
            ]
            if not taxed.empty:
                issues.append({
                    "level": "error",
                    "category": "消費税",
                    "account": account,
                    "month": "全期間",
                    "message": f"【要修正】{account} に課税区分が設定されている仕訳が {len(taxed)}件 あります。資産・負債科目は基本的に「対象外」とすべきです。",
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


def _check_overseas_travel(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    海外出張費が課税仕入になっているチェック。

    修正方針:
    - 課税仕入（10%）になっている場合のみ指摘（非課税・不課税はOK）
    - 摘要から「明らかに海外」と判断できるもののみ対象
      × 曖昧: 「JAL」「航空」「HOTEL」単独 → 国内の可能性あり
      ○ 明確: 「海外出張」「海外ホテル」「USD」「EUR」など明示的なもの
    - 1件ずつ個別に表示
    """
    issues = []

    if "description" not in df.columns:
        return issues

    if "debit_tax" not in df.columns:
        return issues

    # 「明らかに海外」と判断できるキーワード（曖昧なものは除外）
    KW_CLEARLY_OVERSEAS = [
        "海外出張", "海外ホテル", "海外現地", "海外",
        "USD", "EUR", "GBP", "CNY", "HKD", "SGD", "AUD",
        "外貨", "国際線", "渡航",
    ]

    target_accounts = ["旅費交通費", "接待交際費", "会議費"]
    # fillna("") でNaNを空文字に変換してから判定
    acc_mask = df["debit_account"].fillna("").astype(str).apply(
        lambda x: bool(x) and any(a in x for a in target_accounts)
    )

    def _is_clearly_overseas(text: str) -> bool:
        t = str(text)
        return any(k in t for k in KW_CLEARLY_OVERSEAS)

    def _is_taxable_purchase(tax_val: str) -> bool:
        """課税仕入（10%）かどうかを判定。非課税・不課税・免税はFalse"""
        s = str(tax_val)
        # 不課税・非課税・対象外・免税はOK（指摘しない）
        if any(k in s for k in ["不課税", "非課税", "対象外", "免税"]):
            return False
        # 課税（10%）の場合のみTrue
        if any(k in s for k in ["課税", "10%"]):
            return True
        return False

    targets = df[
        acc_mask &
        df["description"].fillna("").astype(str).apply(_is_clearly_overseas) &
        df["debit_tax"].astype(str).apply(_is_taxable_purchase)
    ]

    for _, row in targets.iterrows():
        from checkers.check_utils import desc_safe, month_safe
        issues.append({
            "level": "warning",
            "category": "消費税",
            "account": str(row["debit_account"]),
            "month": month_safe(row),
            "message": (
                f"【要確認】摘要「{desc_safe(row)}」は海外出張関連と思われますが、"
                f"税区分が課税（{row['debit_tax']}）になっています。"
                "海外での支出は消費税の課税対象外（不課税）となります。税区分を確認してください。"
            ),
        })

    return issues
