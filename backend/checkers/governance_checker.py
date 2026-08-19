"""
カテゴリ5: 税務リスク・ガバナンスチェック
5-1: 役員給与の定期同額要件
5-2: 重複仕訳の検知
5-3: 金額の桁数ミス・逆仕訳
5-4: 交際費と会議費・福利厚生費の境界線
5-5: 源泉所得税の徴収漏れ
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Any
from checkers.check_utils import desc_safe, month_safe, date_safe, slip_safe

# 5-4: 交際費への疑いキーワード
KW_ENTERTAINMENT = [
    "贈答", "ゴルフ", "お中元", "お歳暮", "中元", "歳暮",
    "祝儀", "香典", "ギフト", "gift", "Gift",
]

# 5-5: 源泉徴収が必要な報酬キーワード
KW_WITHHOLDING = [
    "弁護士", "税理士", "司法書士", "行政書士", "社会保険労務士",
    "デザイン", "デザイナー", "講師", "原稿", "翻訳", "通訳",
    "著作", "イラスト", "写真家", "カメラマン", "コンサルタント",
]

ENTERTAINMENT_UNIT_LIMIT = 10_000  # 1人あたり1万円（2024改正後の会議費基準）
# 会議費は最低2名以上のため、2万円（1万円×2名）以下は指摘しない
KAIGI_MIN_FLAG = 20_000
DIGIT_ERROR_MULTIPLIER   = 5.0     # 平均の5倍以上/5分の1以下


def check_governance(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    issues.extend(_check_5_1_director_pay(df))
    issues.extend(_check_5_2_duplicate_entries(df))
    issues.extend(_check_5_3_digit_error(df))
    issues.extend(_check_5_4_entertainment(df))
    issues.extend(_check_5_5_withholding(df))
    issues.extend(_check_5_6_entertainment_limit(df))
    return issues


# ──────────────────────────────────────────────────────────
# 5-6: 交際費の年間損金算入限度額（中小法人800万円）への接近
# ──────────────────────────────────────────────────────────
ENTERTAINMENT_ANNUAL_LIMIT = 8_000_000  # 中小法人の定額控除限度額
ENTERTAINMENT_WARN_AT      = 6_000_000  # この額を超えたら接近を通知


def _check_5_6_entertainment_limit(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    交際費（接待交際費）の累計が、中小法人の損金算入限度額（年800万円）に
    接近・超過していないかを確認する。

    ※ データの期間＝事業年度とは限らないため、「アップロード期間の累計」で
      判定し、事業年度累計は別途確認を促す。
    """
    issues = []
    mask_d = df["debit_account"].fillna("").astype(str).str.contains("交際費", na=False)
    mask_c = df["credit_account"].fillna("").astype(str).str.contains("交際費", na=False)
    total = float(df[mask_d]["debit_amount"].sum()) - float(df[mask_c]["credit_amount"].sum())
    if total < ENTERTAINMENT_WARN_AT:
        return issues

    if total >= ENTERTAINMENT_ANNUAL_LIMIT:
        level, tag = "warning", "高"
        note = (
            "中小法人の定額控除限度額（年800万円）を超えています。"
            "超過部分は損金不算入となる可能性があります"
            "（接待飲食費の50%損金算入との有利選択も検討してください）。"
        )
    else:
        level, tag = "info", "低"
        note = (
            "中小法人の定額控除限度額（年800万円）に接近しています。"
            "残りの期間の支出ペースにご留意ください。"
        )

    issues.append({
        "level": level, "category": "5-6 交際費限度額",
        "check_id": "5-6", "account": "交際費", "month": "全期間",
        "message": (
            f"【5-6・{tag}】交際費のアップロード期間累計が {total:,.0f}円 です。{note}"
            "（事業年度の累計額は年度全体で別途ご確認ください）"
        ),
    })
    return issues


# ──────────────────────────────────────────────────────────
# 5-1: 役員給与の定期同額要件
# ──────────────────────────────────────────────────────────
def _check_5_1_director_pay(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    役員報酬の月次集計を行い、期首3ヶ月以降に金額変動があれば警告
    """
    issues = []
    mask = df["debit_account"].astype(str).str.contains("役員報酬|役員給与", na=False)
    entries = df[mask].copy()
    if entries.empty:
        return issues

    period = entries["date"].dt.to_period("M")
    monthly = entries.groupby(period)["debit_amount"].sum().sort_index()

    if len(monthly) < 4:
        return issues

    # 4ヶ月目以降の変動チェック
    # 基準額は「期首3ヶ月目の額」を使う。定期同額給与は期首3ヶ月以内の改定が
    # 認められており、正規の改定があった場合は改定後の額で以後同額となるべき。
    # （3ヶ月平均を基準にすると、正規改定後の全月が誤検知になる）
    base_amount = monthly.iloc[2]
    check_months = monthly.iloc[3:]

    # 変動した月をまとめて1件の指摘にする（月ごとに分けない）
    changed = [(p, val) for p, val in check_months.items() if abs(val - base_amount) > 1]
    if changed:
        detail = "、".join(f"{p}：{val:,.0f}円" for p, val in changed[:12])
        issues.append({
            "level": "error", "category": "5-1 役員給与定期同額",
            "check_id": "5-1", "account": "役員報酬",
            "month": str(changed[0][0]),
            "message": (
                f"【5-1・高】役員報酬が期首3ヶ月目の額（{base_amount:,.0f}円）から"
                f"変動している月があります（{len(changed)}ヶ月）: {detail}。"
                "定期同額給与は期首から3ヶ月以内の改定を除き、"
                "変動があると損金不算入となる場合があります。"
                "（期中改定が適正な手続きによるものであれば問題ありません）"
            ),
        })
    return issues


# ──────────────────────────────────────────────────────────
# 5-2: 重複仕訳の検知
# ──────────────────────────────────────────────────────────
# 5-2: 日常的に同額が多発する科目・摘要は重複チェック対象外
# リース料・賃借料は同額の契約を複数持つことが普通のため除外
DUP_EXCLUDE_ACCOUNTS = ["支払手数料", "支払利息", "雑費", "リース料", "賃借料", "地代家賃"]
DUP_EXCLUDE_KEYWORDS = ["振込手数料", "手数料", "利息", "振込料", "リース", "メンテナンス"]

# 定型単価の除外:
# ネット通販の送料（クリックポスト185円等）のように、同じ金額が期中ずっと
# 毎日何件も発生する取引は、科目名や摘要では除外しきれない。
# 判定は「同日に複数計上される日が何日あるか」で行う。
# 二重計上ミスは特定の1日だけ複数になるのに対し、定型単価は
# ほとんどの日で複数計上されるため、この違いで切り分けられる。
DUP_ROUTINE_MIN_COUNT      = 20  # 期中の総件数がこれ以上、かつ
DUP_ROUTINE_MIN_MULTI_DAYS = 5   # 同日複数計上の日数がこれ以上なら定型単価


def _check_5_2_duplicate_entries(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    金額・借方科目が同じで、日付が7日以内、摘要が類似している仕訳を検知。
    N件の重複がある場合、ペアをそれぞれ表示するのではなく
    グループ化して1件の指摘にまとめる。
    振込手数料・利息など同額が正常に多発する取引は除外する。
    """
    issues = []
    if df.empty:
        return issues

    work = df[df["debit_amount"] > 0].copy()
    # 手数料・利息系の科目を除外
    work = work[~work["debit_account"].fillna("").astype(str).apply(
        lambda x: any(a in x for a in DUP_EXCLUDE_ACCOUNTS)
    )]
    # 摘要が手数料・利息系のものを除外
    work = work[~work["description"].fillna("").astype(str).apply(
        lambda x: any(k in x for k in DUP_EXCLUDE_KEYWORDS)
    )]
    # 完全に同一日付の同額・同科目のみを重複候補とするため、日付が無い行は対象外
    work = work[work["date"].notna()]
    if len(work) < 2:
        return issues

    # DataFrame.iloc をループで多用すると激遅なので辞書レコードに変換して扱う。
    recs = work.to_dict("records")
    for idx, r in enumerate(recs):
        r["_i"] = idx
        r["_date_norm"] = pd.Timestamp(r["date"]).normalize()

    # 「同一日・同科目・同額」でまずグループ化する。
    # 重複候補は必ず同一グループに収まるので、総当たり(O(n^2))を避けられる。
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in recs:
        groups[(r["_date_norm"], r["debit_account"], r["debit_amount"])].append(r)

    # 定型単価（同一科目・同一金額が、多くの日で同日複数計上される）は重複候補から外す
    routine_count: dict = defaultdict(int)
    routine_multi: dict = defaultdict(int)
    for (_d, acc, amt), g in groups.items():
        routine_count[(acc, amt)] += len(g)
        if len(g) >= 2:
            routine_multi[(acc, amt)] += 1
    routine_keys = {
        k for k, cnt in routine_count.items()
        if cnt >= DUP_ROUTINE_MIN_COUNT
        and routine_multi[k] >= DUP_ROUTINE_MIN_MULTI_DAYS
    }

    for (_d, _acc, _amt), group in groups.items():
        if (_acc, _amt) in routine_keys:
            continue
        if len(group) < 2:
            continue

        # グループ内だけで摘要類似度などを見て重複ペアを結ぶ（グループは通常数件）
        by_i = {r["_i"]: r for r in group}
        adj: dict = {r["_i"]: set() for r in group}
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                ri, rj = group[a], group[b]
                # 補助科目が異なる場合は別取引 → 重複ではない
                sub_i = str(ri.get("debit_sub", "")).strip()
                sub_j = str(rj.get("debit_sub", "")).strip()
                if sub_i != sub_j and sub_i not in ("", "nan") and sub_j not in ("", "nan"):
                    continue

                # 摘要の類似度（簡易）― NaN対策でdesc_safeを使用
                # 両方とも空欄の場合は「区別する情報がない同日・同科目・同額仕訳」
                # として重複候補に含める（片方だけ空欄なら別取引とみなしスキップ）
                desc_i = desc_safe(ri)
                desc_j = desc_safe(rj)
                if desc_i or desc_j:
                    if _simple_similarity(desc_i, desc_j) <= 0.6:
                        continue

                # ── 除外ルール ──────────────────────────────
                # ① 往復交通費: 「A〜B」vs「B〜A」は行きと帰り
                if _is_round_trip(desc_i, desc_j):
                    continue
                # ② 番号・コード違い: 数字が含まれていてその数字が異なる場合は別取引
                if _has_different_numbers(desc_i, desc_j):
                    continue
                # ③ 固有名詞（人名等）の違い: 特定の語が異なる場合は別取引
                if _has_different_proper_nouns(desc_i, desc_j):
                    continue
                # ────────────────────────────────────────────

                adj[ri["_i"]].add(rj["_i"])
                adj[rj["_i"]].add(ri["_i"])

        # グループ内の連結成分（重複グループ）を求める
        visited: set = set()
        for start in adj:
            if start in visited or not adj[start]:
                visited.add(start)
                continue
            component = []
            queue = [start]
            while queue:
                node = queue.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                queue.extend(adj[node] - visited)
            if len(component) < 2:
                continue

            # グループ全体を1件の指摘にまとめる
            rows = [by_i[idx] for idx in sorted(component)]
            amount    = rows[0]["debit_amount"]
            account   = str(rows[0]["debit_account"])
            dates_str = "、".join(str(pd.Timestamp(r["date"]).date()) for r in rows)
            descs     = list(dict.fromkeys(desc_safe(r) for r in rows if desc_safe(r)))
            desc_part = "　摘要: " + "、".join(f"「{d[:20]}」" for d in descs[:4]) if descs else ""
            slips     = list(dict.fromkeys(slip_safe(r) for r in rows if slip_safe(r)))

            issues.append({
                "level": "warning", "category": "5-2 重複仕訳",
                "check_id": "5-2", "account": account,
                "month": str(pd.Timestamp(rows[0]["date"]).to_period("M")),
                "slip": "、".join(slips[:6]),
                # AI選別用の最小限データ（顧問先名などの識別情報は含めない）
                "detail": {
                    "amount": float(amount),
                    "count": len(component),
                    "account": account,
                    "dates": [str(pd.Timestamp(r["date"]).date()) for r in rows],
                    "descriptions": descs[:6],
                },
                "message": (
                    f"【5-2・高】重複仕訳の可能性があります: "
                    f"同額（{amount:,.0f}円）・同科目（{account}）の仕訳が "
                    f"{len(component)}件 あります。"
                    f"日付: {dates_str}"
                    f"{desc_part}"
                ),
            })

    return issues


def _is_round_trip(desc_i: str, desc_j: str) -> bool:
    """
    往復交通費の検出: 「A〜B」と「B〜A」のパターンを検知する。
    例: 「東京〜長岡」vs「長岡〜東京」→ True（除外）
    """
    import re
    pattern = re.compile(r'(.+?)〜(.+)')
    m_i = pattern.search(desc_i)
    m_j = pattern.search(desc_j)
    if not m_i or not m_j:
        return False
    from_i, to_i = m_i.group(1).strip(), m_i.group(2).strip()
    from_j, to_j = m_j.group(1).strip(), m_j.group(2).strip()
    # 方向が逆なら往復
    return (from_i == to_j and to_i == from_j)


def _has_different_numbers(desc_i: str, desc_j: str) -> bool:
    """
    番号・コード違いの検出: 両方の摘要に数字があり、その数字が異なる場合。
    例: 「リフト 2861」vs「リフト 2902」→ True（除外）
         「リフト B2t 9-え336」vs「リフト B1.8t 9-い1360」→ True（除外）
    """
    import re
    # 数字の塊（スペース区切りの単語内の数字を含む語）を抽出
    nums_i = re.findall(r'[A-Za-z0-9ぁ-ん゠-ヿ]*\d+[A-Za-z0-9ぁ-ん゠-ヿ]*', desc_i)
    nums_j = re.findall(r'[A-Za-z0-9ぁ-ん゠-ヿ]*\d+[A-Za-z0-9ぁ-ん゠-ヿ]*', desc_j)
    if not nums_i or not nums_j:
        return False
    # どちらかに含まれる数字トークンが完全一致しない → 別アイテム
    set_i = set(nums_i)
    set_j = set(nums_j)
    # 共通部分がなければ（全部違う）→ 別取引
    # 少なくとも1つ違う数字トークンがある → 別取引
    return bool(set_i.symmetric_difference(set_j))


def _has_different_proper_nouns(desc_i: str, desc_j: str) -> bool:
    """
    固有名詞（人名・機器名等）の違いを検出。
    同じ構造の摘要で「大中」「椎葉」のように特定の語だけ異なる場合は別取引。
    アプローチ: 共通の先頭部分を除いた残り部分が大きく異なる場合を検知。
    例: 「長期精動 大中 20年」vs「長期精動 椎葉 20年」→ True（除外）
    """
    # スペース区切りでトークン化
    tokens_i = desc_i.split()
    tokens_j = desc_j.split()
    if not tokens_i or not tokens_j:
        return False
    # 共通しないトークンが存在し、かつそれが純粋な数字でない（固有名詞らしい）場合
    only_in_i = set(tokens_i) - set(tokens_j)
    only_in_j = set(tokens_j) - set(tokens_i)
    import re
    def is_proper(token_set):
        return any(not re.match(r'^[\d\s\-\〜\.]+$', t) for t in token_set)
    # 両方に固有名詞らしいトークンの差分がある → 別取引
    return bool(only_in_i) and bool(only_in_j) and is_proper(only_in_i) and is_proper(only_in_j)


def _simple_similarity(s1: str, s2: str) -> float:
    """簡易文字列類似度（共通文字数比率）"""
    if not s1 or not s2:
        return 0.0
    s1, s2 = s1.lower(), s2.lower()
    if s1 == s2:
        return 1.0
    common = sum(1 for c in set(s1) if c in s2)
    return common / max(len(set(s1)), len(set(s2)), 1)


# ──────────────────────────────────────────────────────────
# 5-3: 金額の桁数ミス・逆仕訳
# ──────────────────────────────────────────────────────────
def _check_5_3_digit_error(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []
    target_accounts = ["水道光熱費", "通信費", "地代家賃", "保険料", "リース料"]

    for account in target_accounts:
        mask = df["debit_account"].astype(str).str.contains(account, na=False)
        entries = df[mask].copy()
        if entries.empty:
            continue

        period = df["date"].dt.to_period("M")
        monthly = entries.groupby(period)["debit_amount"].sum()
        if len(monthly) < 3:
            continue

        # 年払い等の大口計上が平均を歪めるため、基準は中央値を使う
        # （平均だと1回の年払いで毎月が「急減」と誤判定される）
        base = monthly.median()
        if base <= 0:
            continue

        spikes, drops = [], []
        for p, val in monthly.items():
            if val > base * DIGIT_ERROR_MULTIPLIER and abs(val - base) > 50000:
                spikes.append(f"{p}：{val:,.0f}円")
            elif val > 0 and val < base / DIGIT_ERROR_MULTIPLIER and base > 50000:
                drops.append(f"{p}：{val:,.0f}円")

        # 科目ごとに「急増」「急減」をそれぞれ1件にまとめる
        if spikes:
            issues.append({
                "level": "warning", "category": "5-3 桁数ミスの可能性",
                "check_id": "5-3", "account": account, "month": "全期間",
                "message": (
                    f"【5-3・中】{account} が中央値（{base:,.0f}円）の{DIGIT_ERROR_MULTIPLIER:.0f}倍超の月が "
                    f"{len(spikes)}ヶ月 あります（{'、'.join(spikes[:12])}）。"
                    "桁数ミスや二重計上の可能性があります。"
                ),
            })
        if drops:
            issues.append({
                "level": "warning", "category": "5-3 桁数ミスの可能性",
                "check_id": "5-3", "account": account, "month": "全期間",
                "message": (
                    f"【5-3・中】{account} が中央値（{base:,.0f}円）を大きく下回る月が "
                    f"{len(drops)}ヶ月 あります（{'、'.join(drops[:12])}）。"
                    "計上漏れの可能性があります（毎月計上でない保険等であれば問題ありません）。"
                ),
            })

    # 逆仕訳チェック（費用科目の貸方に大きな単発計上）
    expense_accounts = ["給料手当", "外注費", "広告宣伝費", "修繕費", "消耗品費"]
    for account in expense_accounts:
        cr_entries = df[
            df["credit_account"].astype(str).str.contains(account, na=False) &
            (df["credit_amount"] > 100000)
        ]
        for _, row in cr_entries.iterrows():
            desc = str(row.get("description", ""))
            # 正規の戻し仕訳キーワードを除外
            if any(kw in desc for kw in ["振替", "取消", "訂正", "修正", "戻し", "返還"]):
                continue
            issues.append({
                "level": "warning", "category": "5-3 逆仕訳",
                "check_id": "5-3R", "account": account,
                "month": str(row["date"].to_period("M")) if pd.notna(row["date"]) else "不明",
                "message": (
                    f"【5-3・中】費用科目「{account}」の貸方（逆仕訳）に {row['credit_amount']:,.0f}円 があります。"
                    "借貸の入力逆転や異常な戻し仕訳の可能性があります。"
                    f"摘要: 「{desc[:30]}」"
                ),
            })
    return issues


# ──────────────────────────────────────────────────────────
# 5-4: 交際費と会議費・福利厚生費の境界線
# ──────────────────────────────────────────────────────────
def _check_5_4_entertainment(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues = []

    # 会議費で2万円超（最低2名×1万円＝2万円までは通常範囲のため指摘しない）
    kaigi = df[
        df["debit_account"].astype(str).str.contains("会議費", na=False) &
        (df["debit_amount"] > KAIGI_MIN_FLAG)
    ]
    for _, row in kaigi.iterrows():
        issues.append({
            "level": "warning", "category": "5-4 交際費境界",
            "check_id": "5-4", "account": "会議費",
            "month": date_safe(row), "slip": slip_safe(row),
            "message": (
                f"【5-4・中】会議費 {row['debit_amount']:,.0f}円 が2万円を超えています。"
                "1人あたり1万円超の飲食は交際費（損金不算入の可能性）となる場合があります。"
                "参加人数（1人あたり単価）の確認が必要です。"
                + (f"（摘要: 「{desc_safe(row)}」）" if desc_safe(row) else "")
            ),
        })

    # 会議費・福利厚生費で交際費キーワード
    for acct in ["会議費", "福利厚生費"]:
        kw_entries = df[
            df["debit_account"].astype(str).str.contains(acct, na=False) &
            df.get("description", pd.Series(dtype=str)).fillna("").astype(str).apply(
                lambda x: bool(x) and any(k in x for k in KW_ENTERTAINMENT)
            )
        ]
        for _, row in kw_entries.iterrows():
            issues.append({
                "level": "warning", "category": "5-4 交際費境界",
                "check_id": "5-4", "account": acct,
                "month": date_safe(row), "slip": slip_safe(row),
                "message": (
                    f"【5-4・中】{acct} の摘要「{desc_safe(row)}」に"
                    "贈答・ゴルフ等のキーワードが含まれています。"
                    "交際費（損金不算入リスク）への該当性を確認してください。"
                ),
            })
    return issues


# ──────────────────────────────────────────────────────────
# 5-5: 源泉所得税の徴収漏れ
# ──────────────────────────────────────────────────────────
def _check_5_5_withholding(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    支払手数料・外注費で個人報酬キーワードがあるのに、
    同一伝票内に預り金（源泉税）の貸方仕訳がない場合を検知
    """
    issues = []

    target = df[
        (
            df["debit_account"].astype(str).str.contains("支払手数料|外注費", na=False)
        ) &
        df.get("description", pd.Series(dtype=str)).fillna("").astype(str).apply(
            lambda x: bool(x) and any(k in x for k in KW_WITHHOLDING)
        ) &
        (df["debit_amount"] > 50000)  # 少額は除外
    ]

    if target.empty:
        return issues

    # 同一伝票（slip_no）内に預り金の貸方があるか確認
    if "slip_no" in df.columns:
        withholding_slips = set(
            df[
                df["credit_account"].astype(str).str.contains("預り金|源泉", na=False)
            ]["slip_no"].dropna().astype(str)
        )
        missing = target[~target["slip_no"].astype(str).isin(withholding_slips)]
    else:
        # 同日の預り金仕訳があるか確認
        withholding_dates = set(
            df[
                df["credit_account"].astype(str).str.contains("預り金|源泉", na=False)
            ]["date"].dropna()
        )
        missing = target[~target["date"].isin(withholding_dates)]

    for _, row in missing.iterrows():
        issues.append({
            "level": "error", "category": "5-5 源泉徴収漏れ",
            "check_id": "5-5", "account": str(row["debit_account"]),
            "month": month_safe(row), "slip": slip_safe(row),
            "message": (
                f"【5-5・高】{row['debit_account']} {row['debit_amount']:,.0f}円"
                + (f"（摘要: 「{desc_safe(row)}」）" if desc_safe(row) else "")
                + "に個人報酬のキーワードが含まれていますが、"
                "同一伝票内に源泉所得税（預り金）の計上がありません。"
                "個人への報酬支払時は原則10.21%の源泉徴収が必要です。"
            ),
        })
    return issues
