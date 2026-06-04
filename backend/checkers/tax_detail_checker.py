"""
カテゴリ2: 消費税区分の正確性チェック（科目・摘要ミスマッチ検知）
2-1: 軽減税率の適用誤り
2-2: 売上債権の譲渡手数料の非課税漏れ
2-3: 行政手数料・印紙代の課税処理誤り
2-4: 諸会費 + クレカ年会費の課税誤り
2-5: 海外ベンダー取引の課税誤り
2-6: 海外渡航費の課税誤り
"""
import pandas as pd
from typing import List, Dict, Any
from checkers.check_utils import desc_safe, month_safe, is_store_address

# ─── キーワードマスタ ───
KW_REDUCED_TAX = ["弁当", "茶", "菓子", "食料品", "土産", "お茶", "おにぎり", "サンドイッチ", "惣菜"]
KW_CARD_FEE    = ["カード手数料", "EC決済", "決済手数料", "Amazon手数料", "楽天手数料",
                   "PayPay手数料", "クレジット手数料", "加盟店手数料"]
KW_GOVT_FEE    = ["印紙", "住民票", "証明書", "登録免許税", "パスポート", "収入印紙",
                   "公証", "登記", "定款認証", "行政", "役所", "国税", "都税"]
KW_CARD_ANNUAL = ["年会費", "JCB", "VISA", "アメックス", "Amex", "AMEX",
                   "マスター", "Mastercard", "ダイナース", "カード"]
KW_OVERSEAS_VENDOR = [
    "Google", "AWS", "Amazon Web", "Meta", "Facebook", "Microsoft",
    "ZOOM", "Zoom", "Adobe", "Dropbox", "Slack", "GitHub", "Netflix",
    "Spotify", "Apple", "ChatGPT", "OpenAI", "Salesforce", "HubSpot",
]
KW_OVERSEAS_TRAVEL = [
    "海外出張", "渡航", "航空券", "国際線", "海外ホテル", "海外現地",
    "USD", "EUR", "GBP", "CNY", "外貨", "免税店",
]

TAX_10 = ["課税", "10%", "課税売上", "課税仕入"]
TAX_8  = ["軽減", "8%"]


def _has_tax_10(tax_val: str) -> bool:
    return any(k in str(tax_val) for k in TAX_10) and not any(k in str(tax_val) for k in TAX_8)


def _is_non_taxable(tax_val: str) -> bool:
    s = str(tax_val)
    return "対象外" in s or "不課税" in s or "非課税" in s or "免税" in s


def _has_keyword(text: str, keywords: list) -> bool:
    t = str(text).lower()
    return any(k.lower() in t for k in keywords)


def check_tax_detail(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    if "description" not in df.columns:
        return issues
    issues.extend(_check_2_1_reduced_rate(df))
    issues.extend(_check_2_2_card_fee(df))
    issues.extend(_check_2_3_govt_fee(df))
    issues.extend(_check_2_4_membership_fee(df))
    issues.extend(_check_2_5_overseas_vendor(df))
    issues.extend(_check_2_6_overseas_travel(df))
    return issues


def _check_2_1_reduced_rate(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """軽減税率(8%)が疑われるのに10%が適用されている"""
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    targets = df[
        df["description"].astype(str).apply(lambda x: _has_keyword(x, KW_REDUCED_TAX)) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "warning", "category": "2-1 軽減税率",
            "check_id": "2-1", "account": str(row["debit_account"]),
            "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
            "message": (
                f"【2-1・中】摘要「{str(row['description'])[:30]}」は飲食料品の疑いがありますが、"
                f"税区分が「{row[col]}」（10%）になっています。軽減税率8%を確認してください。"
            ),
        })
    return issues


def _check_2_2_card_fee(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """クレカ・EC決済手数料が非課税なのに課税になっている"""
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    targets = df[
        df["debit_account"].astype(str).str.contains("支払手数料", na=False) &
        df["description"].astype(str).apply(lambda x: _has_keyword(x, KW_CARD_FEE)) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "error", "category": "2-2 決済手数料非課税",
            "check_id": "2-2", "account": "支払手数料",
            "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
            "message": (
                f"【2-2・高】摘要「{str(row['description'])[:30]}」はクレジットカード・EC決済手数料と"
                "思われますが、税区分が課税（10%）になっています。"
                "売上債権の譲渡にかかる手数料は非課税となります。"
            ),
        })
    return issues


def _check_2_3_govt_fee(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    印紙・行政手数料が課税になっている。
    ただし「市役所前店」「区役所通り店」のように、
    店舗名・住所として使われているキーワードは除外する。
    """
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    # トリガーワードごとに店舗名除外チェックを行う
    GOVT_TRIGGER_STORE_CHECK = ["市役所", "区役所", "町役場", "村役場"]

    def _is_genuine_govt(text: str) -> bool:
        """店舗名・住所の一部ではなく、本当の行政機関への支払かを判定"""
        if not _has_keyword(text, KW_GOVT_FEE):
            return False
        # 店舗名として使われていそうなキーワードは除外
        for trigger in GOVT_TRIGGER_STORE_CHECK:
            if trigger in text and is_store_address(text, trigger):
                return False
        return True

    targets = df[
        df["description"].astype(str).apply(_is_genuine_govt) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        d = desc_safe(row)
        issues.append({
            "level": "error", "category": "2-3 行政手数料",
            "check_id": "2-3", "account": str(row["debit_account"]),
            "month": month_safe(row),
            "message": (
                f"【2-3・高】摘要「{d}」は印紙・行政手数料と"
                "思われますが、税区分が課税（10%）になっています。"
                "印紙税・行政手数料等は非課税・不課税となります。"
            ),
        })
    return issues


def _check_2_4_membership_fee(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """クレカ年会費が諸会費で不課税になっている（本来は課税）"""
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    targets = df[
        df["debit_account"].astype(str).str.contains("諸会費", na=False) &
        df["description"].astype(str).apply(lambda x: _has_keyword(x, KW_CARD_ANNUAL)) &
        df[col].astype(str).apply(_is_non_taxable)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "warning", "category": "2-4 諸会費課税漏れ",
            "check_id": "2-4", "account": "諸会費",
            "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
            "message": (
                f"【2-4・中】諸会費の摘要「{str(row['description'])[:30]}」にクレジットカード年会費の"
                "キーワードが含まれていますが、税区分が不課税/対象外になっています。"
                "クレカ年会費は課税仕入（10%）となります。消費税控除漏れを確認してください。"
            ),
        })
    return issues


def _check_2_5_overseas_vendor(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """海外ベンダー（Google・AWS等）への支払が課税になっている"""
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    target_accounts = ["広告宣伝費", "通信費", "支払手数料", "諸会費", "ソフトウェア", "システム費"]
    acc_mask = df["debit_account"].astype(str).apply(
        lambda x: any(a in x for a in target_accounts)
    )

    targets = df[
        acc_mask &
        df["description"].astype(str).apply(lambda x: _has_keyword(x, KW_OVERSEAS_VENDOR)) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "error", "category": "2-5 海外ベンダー",
            "check_id": "2-5", "account": str(row["debit_account"]),
            "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
            "message": (
                f"【2-5・高】摘要「{str(row['description'])[:30]}」は海外ベンダーへの支払と"
                "思われますが、税区分が課税（10%）になっています。"
                "国外事業者からの役務提供は原則として不課税となります。"
                "リバースチャージ対象かどうかを確認してください。"
            ),
        })
    return issues


def _check_2_6_overseas_travel(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """海外渡航・国際線が課税になっている"""
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    target_accounts = ["旅費交通費", "接待交際費", "会議費"]
    acc_mask = df["debit_account"].astype(str).apply(
        lambda x: any(a in x for a in target_accounts)
    )

    targets = df[
        acc_mask &
        df["description"].astype(str).apply(lambda x: _has_keyword(x, KW_OVERSEAS_TRAVEL)) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "error", "category": "2-6 海外渡航費",
            "check_id": "2-6", "account": str(row["debit_account"]),
            "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
            "message": (
                f"【2-6・高】摘要「{str(row['description'])[:30]}」は海外渡航・国際線関連と"
                "思われますが、税区分が課税（10%）になっています。"
                "海外現地の費用・国際線航空券は不課税（対象外）となります。"
            ),
        })
    return issues
