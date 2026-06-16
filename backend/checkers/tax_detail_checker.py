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
from checkers.check_utils import desc_safe, month_safe, date_safe, is_store_address, slip_safe

# ─── キーワードマスタ ───
# ※一文字の「茶」は地名（天下茶屋・茶屋町等）に誤反応するため使用しない
KW_REDUCED_TAX = [
    "弁当", "菓子", "食料品", "土産", "おにぎり", "サンドイッチ", "惣菜",
    "お茶", "緑茶", "麦茶", "茶葉", "茶菓",
]
# 摘要にこれらが含まれる場合は飲食料品ではない（地名・店名・施設名等）
KW_REDUCED_TAX_EXCLUDE = [
    "茶屋",       # 天下茶屋・茶屋町など地名
    "駐輪", "駐車", "交通", "切符", "乗車",
]

# 2-7: 精勤手当・永年勤続表彰（不課税）
KW_SERVICE_AWARD = [
    "精勤", "長期精勤", "皆勤", "永年勤続", "勤続表彰", "勤続",
    "表彰", "永年", "長期勤続",
]
# 上記に加えて「手当」「賞」「金」を後ろに含む語も対象とするため
# 関数内で複合判定する

# 2-8: 新聞代（軽減税率8%対象）
# ※ 電子版・デジタル版・IDサービス（日経ID等）は標準税率10%のため除外
KW_NEWSPAPER = [
    "新聞", "朝日新聞", "読売新聞", "毎日新聞", "産経新聞",
    "日本経済新聞", "中日新聞", "北海道新聞",
]
# 電子・デジタル・IDを含む場合は軽減税率チェック対象外
KW_NEWSPAPER_EXCLUDE = ["電子版", "電子", "デジタル", "ID", "オンライン", "Web", "web"]

# 2-9: 売掛金回収時の振込手数料（売上対価の返還等として処理すべき）
WIRE_FEE_AMOUNTS = [110, 220, 330, 440, 550, 660, 770, 880]
KW_CARD_FEE    = ["カード手数料", "EC決済", "決済手数料", "Amazon手数料", "楽天手数料",
                   "PayPay手数料", "クレジット手数料", "加盟店手数料"]
KW_GOVT_FEE    = ["印紙", "住民票", "登録免許税", "パスポート", "収入印紙",
                   "公証", "登記", "定款認証", "行政", "役所", "国税", "都税"]
# 「証明書」は残高証明書（銀行）など行政以外でも使われるため除外済み
# クレカ年会費の判定: 「年会費」単独では同業団体年会費（不課税が正しい）や
# 「新年会費」（新年会の会費）に誤反応するため、カードと特定できる語を必須とする
KW_CARD_BRAND = ["JCB", "VISA", "アメックス", "Amex", "AMEX",
                  "マスター", "Mastercard", "ダイナース", "カード", "クレカ"]
KW_CARD_ANNUAL_EXCLUDE = ["新年会", "忘年会", "懇親会", "歓迎会", "送別会"]
# 2-5: 適格請求書発行事業者として登録済みの主要海外ベンダー（指摘不要）
KW_OVERSEAS_REGISTERED = [
    "Google", "グーグル", "AWS", "Amazon", "アマゾン", "Meta", "Facebook",
    "Microsoft", "マイクロソフト", "ZOOM", "Zoom", "Adobe", "アドビ",
    "Dropbox", "Netflix", "Spotify", "Apple", "アップル", "Salesforce",
    "Slack", "OpenAI", "ChatGPT", "Midjourney",
]
# 登録状況が不明・未登録の可能性がある海外ベンダー（登録確認を促す）
KW_OVERSEAS_UNCERTAIN = [
    "GitHub", "Anthropic", "Claude", "HubSpot",
    "Shopify", "Canva", "Notion", "Figma", "Discord",
    "Wix", "Squarespace", "Mailchimp", "Zapier", "Stripe",
]
KW_OVERSEAS_TRAVEL = [
    "海外出張", "渡航", "航空券", "国際線", "海外ホテル", "海外現地", "海外",
    "USD", "EUR", "GBP", "CNY", "HKD", "SGD", "AUD", "外貨", "免税店",
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
    issues.extend(_check_2_7_service_award(df))
    issues.extend(_check_2_8_newspaper(df))
    issues.extend(_check_2_9_wire_fee_return(df))
    issues.extend(_check_2_10_residential_rent(df))
    issues.extend(_check_2_11_cashback(df))
    return issues


def _check_2_1_reduced_rate(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """軽減税率(8%)が疑われるのに10%が適用されている"""
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    targets = df[
        df["description"].fillna("").astype(str).apply(
            lambda x: _has_keyword(x, KW_REDUCED_TAX)
                      and not _has_keyword(x, KW_REDUCED_TAX_EXCLUDE)
        ) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "warning", "category": "2-1 軽減税率",
            "check_id": "2-1", "account": str(row["debit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【2-1・中】摘要「{desc_safe(row)}」は飲食料品の可能性がありますが、"
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
        df["description"].fillna("").astype(str).apply(lambda x: _has_keyword(x, KW_CARD_FEE)) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "error", "category": "2-2 決済手数料非課税",
            "check_id": "2-2", "account": "支払手数料",
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【2-2・高】摘要「{desc_safe(row)}」はクレジットカード・EC決済手数料と"
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
        df["description"].fillna("").astype(str).apply(_is_genuine_govt) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        d = desc_safe(row)
        issues.append({
            "level": "error", "category": "2-3 行政手数料",
            "check_id": "2-3", "account": str(row["debit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
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
        df["description"].fillna("").astype(str).apply(
            lambda x: _has_keyword(x, KW_CARD_BRAND)
                      and not _has_keyword(x, KW_CARD_ANNUAL_EXCLUDE)
        ) &
        df[col].astype(str).apply(_is_non_taxable)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "warning", "category": "2-4 諸会費課税漏れ",
            "check_id": "2-4", "account": "諸会費",
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【2-4・中】諸会費の摘要「{desc_safe(row)}」にクレジットカード年会費の"
                "キーワードが含まれていますが、税区分が不課税/対象外になっています。"
                "クレカ年会費は課税仕入（10%）となります。消費税控除漏れを確認してください。"
            ),
        })
    return issues


def _check_2_5_overseas_vendor(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    海外ベンダーへの支払が課税仕入になっている場合のチェック。

    - Google・Apple・Zoom等の適格請求書発行事業者に登録済みの大手は
      課税仕入で問題ないため指摘しない
    - 登録状況が不明な海外ベンダーは、適格請求書発行事業者かどうかの
      確認を促す（未登録なら経過措置80%/50%等への修正が必要）
    """
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    target_accounts = ["広告宣伝費", "通信費", "支払手数料", "諸会費", "ソフトウェア", "システム費"]
    acc_mask = df["debit_account"].fillna("").astype(str).apply(
        lambda x: bool(x) and any(a in x for a in target_accounts)
    )

    def _is_uncertain_overseas(text: str) -> bool:
        # 登録済み大手にマッチする場合は指摘不要
        if _has_keyword(text, KW_OVERSEAS_REGISTERED):
            return False
        return _has_keyword(text, KW_OVERSEAS_UNCERTAIN)

    targets = df[
        acc_mask &
        df["description"].fillna("").astype(str).apply(_is_uncertain_overseas) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "warning", "category": "2-5 海外ベンダー",
            "check_id": "2-5", "account": str(row["debit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【2-5・中】摘要「{desc_safe(row)}」は海外ベンダーへの支払と思われます。"
                "この事業者が適格請求書発行事業者に登録されているか確認してください。"
                "未登録の場合、課税仕入（10%）ではなく経過措置（80%控除等）の税区分への"
                "修正が必要です。"
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
    acc_mask = df["debit_account"].fillna("").astype(str).apply(
        lambda x: bool(x) and any(a in x for a in target_accounts)
    )

    def _is_overseas(text: str) -> bool:
        t = str(text)
        # 「国内」と明記されている場合は海外ではない（例: JAL国内線航空券）
        if "国内" in t:
            return False
        return _has_keyword(t, KW_OVERSEAS_TRAVEL)

    targets = df[
        acc_mask &
        df["description"].fillna("").astype(str).apply(_is_overseas) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "error", "category": "2-6 海外渡航費",
            "check_id": "2-6", "account": str(row["debit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【2-6・高】摘要「{desc_safe(row)}」は海外渡航・国際線関連と"
                "思われますが、税区分が課税（10%）になっています。"
                "海外現地の費用・国際線航空券は不課税（対象外）となります。"
            ),
        })
    return issues


def _check_2_7_service_award(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    2-7: 精勤手当・永年勤続表彰などの手当・表彰金が課税仕入になっている。
    従業員への手当・表彰は原則として消費税の課税対象外（不課税）。
    摘要に「精勤」「永年勤続」「表彰」等のキーワードがあり、税区分が課税10%の場合を検知。
    """
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    def _is_award(text: str) -> bool:
        t = str(text)
        # 通勤手当・残業手当等の給与系手当は課税仕入で正しいため対象外
        if any(k in t for k in ["通勤", "残業", "住宅手当", "家族手当", "資格手当"]):
            return False
        # 精勤・永年勤続・表彰系キーワード
        if any(k in t for k in KW_SERVICE_AWARD):
            return True
        # 「〇〇手当」パターン（精勤・功労等の明確な語との組み合わせのみ）
        if "手当" in t and any(k in t for k in ["精勤", "皆勤", "功労", "表彰", "永年"]):
            return True
        return False

    targets = df[
        df["description"].fillna("").astype(str).apply(_is_award) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "error", "category": "2-7 手当・表彰金",
            "check_id": "2-7", "account": str(row["debit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【2-7・高】摘要「{desc_safe(row)}」は精勤手当・永年勤続表彰などと"
                "思われますが、税区分が課税（10%）になっています。"
                "従業員への手当・表彰金は消費税の課税対象外（不課税）です。"
                "税区分を「対象外」に修正してください。"
            ),
        })
    return issues


def _check_2_8_newspaper(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    2-8: 新聞代が10%になっている（軽減税率8%の対象）。
    定期購読の紙の新聞は軽減税率（8%）の対象。
    ただし電子版・デジタル版・IDサービスは標準税率10%のため除外。
    """
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    def _is_paper_newspaper(text: str) -> bool:
        t = str(text)
        # 新聞キーワードを含む
        if not _has_keyword(t, KW_NEWSPAPER):
            return False
        # 電子・デジタル・IDを含む場合は除外（これらは10%が正しい）
        if any(k.lower() in t.lower() for k in KW_NEWSPAPER_EXCLUDE):
            return False
        return True

    targets = df[
        df["description"].fillna("").astype(str).apply(_is_paper_newspaper) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "warning", "category": "2-8 新聞軽減税率",
            "check_id": "2-8", "account": str(row["debit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【2-8・中】摘要「{desc_safe(row)}」は紙の新聞代と思われますが、"
                "税区分が課税（10%）になっています。"
                "定期購読の紙新聞は軽減税率（8%）の対象です。税区分を8%に修正してください。"
                "（電子版・デジタル版は10%のため対象外）"
            ),
        })
    return issues


def _check_2_9_wire_fee_return(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    2-9: 売掛金回収時の振込手数料が課税仕入（10%）になっている。
    取引先が振込手数料を差し引いて入金してきた場合、その手数料は
    「売上対価の返還等（10%）」として処理すべきで、課税仕入ではない。

    検知パターン:
    - 勘定科目が「通信費」または「支払手数料」
    - 金額が振込手数料相当（110〜880円）
    - 税区分が課税（10%）
    - 同一伝票または同日に「売掛金」の貸方仕訳がある
    """
    issues = []
    col = "debit_tax" if "debit_tax" in df.columns else None
    if not col:
        return issues

    # 振込手数料候補：通信費・支払手数料で少額かつ課税10%
    fee_mask = (
        df["debit_account"].astype(str).str.contains("通信費|支払手数料", na=False) &
        df["debit_amount"].isin(WIRE_FEE_AMOUNTS) &
        df[col].astype(str).apply(_has_tax_10)
    )
    fee_entries = df[fee_mask].copy()

    # 摘要から「売上金回収」「売上振込」などのキーワードで絞り込む
    # （退職金支払・前渡金支払など売掛金回収と無関係なものを除外）
    KW_AR_COLLECTION = ["売上金回収", "売上振込", "売掛金回収", "掛代金回収", "回収"]
    KW_EXCLUDE_FEE   = ["前渡金", "退職金", "接待", "消耗品", "損害保険", "大阪商工",
                        "証明書", "インターネット", "支払分"]

    def _is_ar_wire_fee(desc: str) -> bool:
        d = str(desc)
        # 明らかに売上回収と無関係なものは除外
        if any(k in d for k in KW_EXCLUDE_FEE):
            return False
        # 「振込手数料」かつ「回収」関連 → 対象
        if "振込手数料" in d and any(k in d for k in KW_AR_COLLECTION):
            return True
        # 「振込手数料/〇〇　〇月売上」パターン
        if "振込手数料/" in d:
            return True
        return False

    # 摘要キーワードで絞り込んだ候補
    fee_entries = fee_entries[
        fee_entries["description"].astype(str).apply(_is_ar_wire_fee)
    ]

    # 該当行のインデックスを集める（件数が多くなるため最後に1件へ集約）
    hit_idx = set()
    for idx, row in fee_entries.iterrows():
        has_ar = False
        if "slip_no" in df.columns:
            slip = str(row.get("slip_no", ""))
            same_slip = df[df["slip_no"].astype(str) == slip]
            has_ar = same_slip["credit_account"].astype(str).str.contains("売掛金", na=False).any()
        if not has_ar:
            ar_credit_dates = set(df[df["credit_account"].astype(str).str.contains("売掛金", na=False)]["date"].dropna())
            has_ar = pd.notna(row["date"]) and row["date"] in ar_credit_dates
        if has_ar:
            hit_idx.add(idx)

    # ── 直接相殺パターン: (支払手数料)××（売掛金）×× ──
    # 同一行で借方が手数料・貸方が売掛金の仕訳は、金額にかかわらず売返とすべき
    offset_mask = (
        df["debit_account"].fillna("").astype(str).str.contains("通信費|支払手数料", na=False) &
        df["credit_account"].fillna("").astype(str).str.contains("売掛金", na=False) &
        df[col].astype(str).apply(_has_tax_10)
    )
    hit_idx |= set(df[offset_mask].index)

    if not hit_idx:
        return issues

    hits = df.loc[sorted(hit_idx)]
    total = hits["debit_amount"].sum()
    cnt = len(hits)

    def _line(row):
        d = row["date"]
        ds = f"{d.year}/{d.month}/{d.day}" if pd.notna(d) else "日付不明"
        slip = f" 伝票No.{slip_safe(row)}" if slip_safe(row) else ""
        return f"{ds}{slip} {row['debit_amount']:,.0f}円"

    lines = "、".join(_line(r) for _, r in hits.head(20).iterrows())
    suffix = f" 他{cnt-20}件" if cnt > 20 else ""

    issues.append({
        "level": "error", "category": "2-9 振込手数料返還",
        "check_id": "2-9", "account": "支払手数料",
        "month": "全期間",
        "message": (
            f"【2-9・高】売掛金回収時に差し引かれた振込手数料と思われる仕訳が "
            f"{cnt}件（合計 {total:,.0f}円）あります。"
            "税区分を「課税仕入（10%）」から「売上対価の返還等（売返・10%）」に"
            "変更してください（1万円未満の売上返還はインボイス交付義務が免除）。"
            f"対象: {lines}{suffix}"
        ),
    })
    return issues


def _check_2_10_residential_rent(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    2-10: 受取家賃が課税売上になっている。
    居住用の家賃収入は消費税の非課税取引。事業用（店舗・事務所等）は課税で正しいため、
    確認を促すレベルで指摘する。
    """
    issues = []
    col = "credit_tax" if "credit_tax" in df.columns else None
    if not col:
        return issues

    targets = df[
        (
            df["credit_account"].fillna("").astype(str).str.contains("受取家賃|家賃収入", na=False) |
            (
                df["credit_account"].fillna("").astype(str).str.contains("雑収入", na=False) &
                df["description"].fillna("").astype(str).str.contains("家賃", na=False)
            )
        ) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "warning", "category": "2-10 受取家賃",
            "check_id": "2-10", "account": str(row["credit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【2-10・中】受取家賃 {row['credit_amount']:,.0f}円"
                + (f"（摘要: {desc_safe(row)}）" if desc_safe(row) else "")
                + f"の税区分が課税（{row[col]}）になっています。"
                "居住用の家賃収入は非課税取引です。"
                "事業用（店舗・事務所・駐車場等）であれば課税のままで問題ありません。"
            ),
        })
    return issues


def _check_2_11_cashback(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    2-11: キャッシュバック・ポイント還元等の収入が課税売上になっている。
    キャッシュバックは対価性のない収入のため消費税の課税対象外（不課税）。
    """
    issues = []
    col = "credit_tax" if "credit_tax" in df.columns else None
    if not col:
        return issues

    KW_CASHBACK = ["キャッシュバック", "ｷｬｯｼｭﾊﾞｯｸ", "ポイント還元", "ポイント付与", "還元金"]

    targets = df[
        df["description"].fillna("").astype(str).apply(lambda x: any(k in x for k in KW_CASHBACK)) &
        (df["credit_amount"] > 0) &
        df[col].astype(str).apply(_has_tax_10)
    ]
    for _, row in targets.iterrows():
        issues.append({
            "level": "error", "category": "2-11 キャッシュバック",
            "check_id": "2-11", "account": str(row["credit_account"]),
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【2-11・高】摘要「{desc_safe(row)}」{row['credit_amount']:,.0f}円 の"
                f"税区分が課税売上（{row[col]}）になっています。"
                "キャッシュバック・ポイント還元は対価性のない収入のため"
                "課税対象外（不課税）です。税区分を修正してください。"
            ),
        })
    return issues
