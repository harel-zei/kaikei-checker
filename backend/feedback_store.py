"""
チェック結果に対する担当者の判断（フィードバック）を蓄積する。

目的:
  使うほど精度が上がる仕組みの土台。担当者が指摘に対して下した判断を貯め、
  次回以降のAI選別（ai_reviewer）の判断材料として渡す。

蓄積する2種類:
  1. 判定フィードバック … 出た指摘が「妥当だった / 不要だった」
     → 不要が続くパターンを抑制し、ノイズを減らす
  2. 見逃しフィードバック … システムが拾えなかったが担当者が見つけた指摘
     → 次回以降、AIが同種の論点を探すための手掛かりにする

保存先は client_store と同じ方式（Supabase or ローカル）。
  ローカル: backend/client_data/_feedback/{クライアント名}.json
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

# Supabaseは任意依存。未設定・未インストールでもローカル保存で動作させる
# （サーバー内保存が標準のため、requests等が無い環境でも壊れないようにする）
try:
    import supabase_store as _sb
    _USE_SB = _sb.is_enabled()
except Exception:
    _sb = None
    _USE_SB = False

DATA_DIR = Path(__file__).parent / "client_data" / "_feedback"
_SB_PREFIX = "_feedback"

# 全クライアント共通の学習を入れる仮想クライアント名
GLOBAL_KEY = "_all"

VERDICT_USEFUL = "useful"   # 妥当な指摘だった
VERDICT_NOISE = "noise"     # 不要（誤検知）だった

_MAX_RECORDS = 500          # 1クライアントあたりの保持件数（古いものから捨てる）


def _safe_name(client_name: str) -> str:
    return "".join(c for c in (client_name or GLOBAL_KEY) if c not in r'\/:*?"<>|') or GLOBAL_KEY


def _load(client_name: str) -> dict:
    name = _safe_name(client_name)
    empty = {"verdicts": [], "missed": []}
    if _USE_SB:
        raw = _sb._get(f"{_SB_PREFIX}/{name}.json")
        return json.loads(raw) if raw else empty
    path = DATA_DIR / f"{name}.json"
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    data.setdefault("verdicts", [])
    data.setdefault("missed", [])
    return data


def _save(client_name: str, data: dict) -> None:
    name = _safe_name(client_name)
    # 件数が増えすぎないよう、新しいものを優先して保持する
    data["verdicts"] = data.get("verdicts", [])[-_MAX_RECORDS:]
    data["missed"] = data.get("missed", [])[-_MAX_RECORDS:]
    body = json.dumps(data, ensure_ascii=False, indent=2)
    if _USE_SB:
        _sb._put(f"{_SB_PREFIX}/{name}.json", body, "application/json")
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / f"{name}.json").write_text(body, encoding="utf-8")


# ──────────────────────────────────────────────────────────
# 記録
# ──────────────────────────────────────────────────────────
def record_verdict(client_name: Optional[str], issue: dict, verdict: str,
                   note: str = "") -> dict:
    """指摘に対する担当者の判断を記録する。

    顧問先の識別情報は保存しない（勘定科目・チェックID・メッセージ要約のみ）。
    """
    if verdict not in (VERDICT_USEFUL, VERDICT_NOISE):
        raise ValueError("verdict は useful か noise を指定してください")

    rec = {
        "check_id": str(issue.get("check_id") or issue.get("category") or ""),
        "category": str(issue.get("category") or ""),
        "account": str(issue.get("account") or ""),
        "level": str(issue.get("level") or ""),
        "summary": str(issue.get("message") or "")[:200],
        "verdict": verdict,
        "note": str(note or "")[:200],
        "at": datetime.now().strftime("%Y/%m/%d %H:%M"),
    }
    for target in {_safe_name(client_name), GLOBAL_KEY}:
        data = _load(target)
        data["verdicts"].append(rec)
        _save(target, data)
    return rec


def record_missed(client_name: Optional[str], account: str, description: str,
                  month: str = "") -> dict:
    """システムが拾えなかった指摘（担当者が手で見つけたもの）を記録する。"""
    if not description.strip():
        raise ValueError("指摘内容を入力してください")
    rec = {
        "account": str(account or "")[:60],
        "month": str(month or "")[:20],
        "description": description.strip()[:400],
        "at": datetime.now().strftime("%Y/%m/%d %H:%M"),
    }
    for target in {_safe_name(client_name), GLOBAL_KEY}:
        data = _load(target)
        data["missed"].append(rec)
        _save(target, data)
    return rec


# ──────────────────────────────────────────────────────────
# 参照（AI選別が使う）
# ──────────────────────────────────────────────────────────
def _pattern_key(check_id: str, account: str) -> str:
    return f"{check_id}|{account}"


def noise_patterns(client_name: Optional[str], min_count: int = 2,
                   min_ratio: float = 0.7) -> list:
    """「不要」と判断されることが多いパターンを返す。

    min_count 件以上の判断があり、そのうち min_ratio 以上が「不要」の
    （チェックID × 勘定科目）の組み合わせを、抑制候補として返す。
    """
    stats: dict = {}
    for rec in _load(client_name).get("verdicts", []):
        key = _pattern_key(rec.get("check_id", ""), rec.get("account", ""))
        s = stats.setdefault(key, {"useful": 0, "noise": 0,
                                   "check_id": rec.get("check_id", ""),
                                   "account": rec.get("account", "")})
        s[rec.get("verdict")] = s.get(rec.get("verdict"), 0) + 1

    out = []
    for key, s in stats.items():
        total = s["useful"] + s["noise"]
        if total < min_count:
            continue
        ratio = s["noise"] / total if total else 0
        if ratio >= min_ratio:
            out.append({
                "check_id": s["check_id"], "account": s["account"],
                "noise": s["noise"], "total": total, "ratio": round(ratio, 2),
            })
    return sorted(out, key=lambda x: -x["noise"])


def useful_examples(client_name: Optional[str], limit: int = 20) -> list:
    """「妥当だった」と判断された指摘の例（AIの判断基準として渡す）"""
    recs = [r for r in _load(client_name).get("verdicts", [])
            if r.get("verdict") == VERDICT_USEFUL]
    return recs[-limit:]


def missed_examples(client_name: Optional[str], limit: int = 30) -> list:
    """システムが拾えなかった指摘の例（AIが同種を探すための手掛かり）"""
    return _load(client_name).get("missed", [])[-limit:]


def stats(client_name: Optional[str]) -> dict:
    """学習状況の要約（画面表示用）"""
    data = _load(client_name)
    verdicts = data.get("verdicts", [])
    useful = sum(1 for r in verdicts if r.get("verdict") == VERDICT_USEFUL)
    noise = sum(1 for r in verdicts if r.get("verdict") == VERDICT_NOISE)
    return {
        "client": _safe_name(client_name),
        "total": len(verdicts),
        "useful": useful,
        "noise": noise,
        "missed": len(data.get("missed", [])),
        "suppressed_patterns": len(noise_patterns(client_name)),
    }
