"""자동매매 실행 로그 → 짧은 텔레그램 요약(2026-11-23 인선).

인선: "말들이 너무 복잡해서 뭘 했는지 알아볼 수가 없어요. '오늘 무슨 일이 있어서, TQQQ 를 00주 매수(000원) 했다'
이런 식으로 알아보기 쉽게." — 그래서 계좌마다 '산 것·판 것'만 한 줄씩, 이유는 쉬운 말 한 줄로. 자세한 판단은 Actions 로그에 그대로 남는다.

    python telegram_summary.py kis_result.txt kiwoom_result.txt [kis_rc.txt kiwoom_rc.txt]
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ACCOUNT = {"v2": "한투 v2", "국내포트": "키움 국내포트"}
NAMES = {
    "v2": {"TQQQ": "TQQQ", "QLD": "QLD", "QQQ": "QQQM", "SCHD": "SCHD", "BRK": "버크셔B",
           "CC_DIV": "TIGER 배당커버드콜액티브(472150)", "CC_NAS": "TIGER 미국나스닥100타겟데일리커버드콜(486290)",
           "GOLD": "ACE KRX금현물(411060)"},
    "국내포트": {"TQ": "TIGER 미국나스닥100레버리지(합성)(418660)", "QL": "TIGER 미국나스닥100레버리지(합성)(418660)",
             "QQ": "KODEX 미국나스닥100(379810)", "DIV": "TIGER 미국배당다우존스(458730)",
             "CCD": "TIGER 배당커버드콜액티브(472150)", "CCN": "TIGER 미국나스닥100타겟데일리커버드콜(486290)",
             "GOLD": "ACE KRX금현물(411060)"},
}
TRADE = re.compile(r"^-> (v2|국내포트) (\S+) (매수|매도) (?:(\d[\d,]*)/)?(\d[\d,]*)주 @ (약 )?(\$[\d,.]+|[\d,.]+원)(?: \(dry-run, 주문 안 냄\))? — (.*)$")


def won(v: float) -> str:
    """원 금액을 '1억 2,345만 원'처럼."""
    v = round(v)
    eok, man = divmod(v, 100_000_000)
    man = round(man / 10_000)
    if eok and man:
        return f"{eok}억 {man:,}만 원"
    if eok:
        return f"{eok}억 원"
    return f"{man:,}만 원" if man else f"{v:,}원"


def why(key: str, side: str, note: str) -> str:
    n = note
    if "연말" in n:
        return "연말이라 정해 둔 비율로 다시 맞췄어요"
    if side == "매도":
        return "시장이 너무 들떠서 번 것을 조금 챙겼어요"
    if "공포 매수" in n:
        return "시장이 아주 무서워해서 샀어요. 다만 주가가 아직 비싼 편이라 많이 사지 않고 조금만 샀어요"
    if key == "GOLD" or "금 칸" in n:
        return "금은 한 달에 한 번씩 나눠 사요 — 이번 차례예요"
    if key.startswith("CC") or "커버드콜" in n or "평시" in n:
        return "커버드콜은 평소에 조금씩 모아요 — 이번 차례예요"
    if "1년" in n:
        return "1년이 돼서 남은 몫을 다 샀어요"
    if "한 달" in n:
        return "한 달 동안 안 사서 정해진 몫을 샀어요"
    if "사다리" in n or "기준가" in n or "RSI" in n and key in ("SCHD", "BRK", "DIV"):
        return "값이 내려와서 나눠 샀어요"
    if "곡선" in n or "목표" in n:
        return "시장이 무서워해서 정해 둔 만큼 더 담았어요"
    return n[:60]


def parse(path: Path):
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.exists() else []
    fg = cape_block = None; total = None; acct = None; trades = []; problems = []; skipped = None; market = None; resting = False; capewait = False
    for ln in lines:
        m = re.match(r"^(v2|국내포트) F&G ([\d.]+)", ln)
        if m:
            acct = acct or m.group(1); fg = float(m.group(2)); cape_block = "→ 막힘" in ln
        m = re.match(r"^(?:v2 새 버전 평가|국내포트 평가) ([\d,]+)원", ln)
        if m:
            total = float(m.group(1).replace(",", ""))
        if ln.startswith("최종 신호:"):
            if "건너뜁니다" in ln or "아무것도 하지 않습니다" in ln:
                skipped = ln.split(":", 1)[1].strip()
            market = "국내장" if "국내장" in ln else "미국장" if ("미국장" in ln or "해외" in ln) else market
        if ln.startswith("-> ") and "쉬는 중" in ln:
            resting = True
        if ln.startswith("-> ") and "대기" in ln and "CAPE 막기" in ln:
            capewait = True
        t = TRADE.match(ln)
        if t:
            a, key, side, filled, want, approx, px, note = t.groups()
            acct = acct or a
            q = int((filled or want).replace(",", ""))
            dry = "dry-run" in ln
            if px.startswith("$"):
                p = float(px[1:].replace(",", "")); price, amount = f"${p:,.2f}", f"${p * q:,.0f}"
            else:
                p = float(px[:-1].replace(",", "")); price, amount = f"{p:,.0f}원", won(p * q)
            trades.append({"name": NAMES.get(a, {}).get(key, key), "side": side, "qty": q, "price": price, "amount": amount,
                           "why": why(key, side, note), "dry": dry})
        elif ln.startswith("-> ") and ("체결되지 않음" in ln or "못 함" in ln or "오류" in ln):
            mm = re.match(r"^-> (v2|국내포트) (\S+) (.*)$", ln)
            if mm:
                problems.append(f"{NAMES.get(mm.group(1), {}).get(mm.group(2), mm.group(2))} {mm.group(3)}")
    return {"acct": acct, "fg": fg, "cape_block": cape_block, "total": total, "trades": trades, "problems": problems,
            "skipped": skipped, "market": market, "empty": not lines, "resting": resting, "capewait": capewait}


def mood(fg: float) -> str:
    return ("아주 무서워하는 날" if fg <= 25 else "무서워하는 날" if fg <= 45 else "보통인 날" if fg < 55
            else "들뜬 날" if fg < 75 else "아주 들뜬 날")


def main(argv):
    kis, kw = Path(argv[1]), Path(argv[2])
    rcs = [Path(p) for p in argv[3:5]]
    res = [("v2", parse(kis)), ("국내포트", parse(kw))]
    now = datetime.now(KST)
    market = next((r["market"] for _, r in res if r["market"]), None)
    out = [f"🤖 FA 자동매매 · {now.month}월 {now.day}일({'월화수목금토일'[now.weekday()]}) {now:%H:%M}" + (f" {market} 마감" if market else "")]
    for (a, r), rc in zip(res, rcs + [None] * (2 - len(rcs))):
        bad = rc is not None and rc.exists() and rc.read_text().strip() not in ("", "0")
        if bad or r["problems"]:
            out.insert(0, f"⚠️ [{ACCOUNT[a]}] 주문에 문제가 있어요 — 아래와 Actions 로그를 확인해 주세요")
    fg = next((r["fg"] for _, r in res if r["fg"] is not None), None)
    if fg is not None:
        out.append(f"오늘 시장: 공포 점수 {fg:.1f} — {mood(fg)}")
    for a, r in res:
        out.append("")
        head = f"[{ACCOUNT[a]}]" + (f" 평가 {won(r['total'])}" if r["total"] else "")
        out.append(head)
        if r["empty"]:
            out.append("실행 기록이 없어요 — Actions 로그를 확인해 주세요")
            continue
        if r["skipped"]:
            out.append("이번엔 실행할 시간이 아니라 건너뛰었어요")
            continue
        for p in r["problems"]:
            out.append(f"⚠️ {p}")
        if not r["trades"]:
            if r["resting"]:
                out.append("오늘은 사고판 것 없어요. 지난번에 산 뒤 며칠 쉬는 기간이에요.")
            elif r["capewait"] and fg is not None and fg <= 45:
                out.append("오늘은 사고판 것 없어요. 시장은 무서워하지만 주가가 아직 비싼 편이라 기다리는 중이에요.")
            else:
                out.append("오늘은 사고판 것 없어요.")
        for t in r["trades"]:
            tag = "🧪 (시험, 주문 안 냄) " if t["dry"] else ("✅ " if t["side"] == "매수" else "🔻 ")
            out.append(f"{tag}{t['name']} {t['qty']:,}주 {t['side']} — 1주 {t['price']}, 모두 {t['amount']}")
            out.append(f"   {t['why']}")
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv)
