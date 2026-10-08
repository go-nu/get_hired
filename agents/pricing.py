"""모델별 토큰 단가로 예상 비용을 계산한다.

단가는 100만 토큰당 미국 달러 (입력, 출력), 2026년 10월 기준. 요금이 바뀌거나 새 모델을
쓰면 여기를 고친다. 웹 검색 요금과 캐시·배치 할인은 반영하지 않는 어림값이다.
원화는 고정 환율(KRW_PER_USD)로 바꾼 값이라, 환율이 많이 움직이면 함께 고친다.
"""

from decimal import Decimal

from django.db.models import Count, Sum

PRICES = {
    "claude-opus-5-5": ("4.00", "20.00"),
    "claude-sonnet-5-5": ("2.00", "10.00"),
    "claude-haiku-4-5": ("1.00", "5.00"),
    "gemini-3.5-flash-lite": ("0.30", "2.50"),
}

MILLION = Decimal(1_000_000)
KRW_PER_USD = Decimal(1340)  # 2026-10-08 기준 1달러 ≈ 1,339원


def estimate_cost(model, input_tokens, output_tokens):
    """예상 비용(달러). 단가를 모르는 모델이면 None."""
    if model not in PRICES:
        return None
    input_price, output_price = map(Decimal, PRICES[model])
    return (input_tokens * input_price + output_tokens * output_price) / MILLION


def to_krw(usd):
    """달러를 원으로. None 이면 None."""
    return None if usd is None else usd * KRW_PER_USD


def cost_rows(runs):
    """AgentRun 쿼리셋을 모델별로 묶은 (행 목록, 합계). 관리자 페이지의 예상 비용 표."""
    rows = list(
        runs.order_by()
        .values("model")
        .annotate(runs=Count("id"), input=Sum("input_tokens"), output=Sum("output_tokens"))
        .order_by("model")
    )
    for row in rows:
        row["cost"] = estimate_cost(row["model"], row["input"], row["output"])
        row["cost_krw"] = to_krw(row["cost"])
    total = sum((row["cost"] for row in rows if row["cost"] is not None), Decimal(0))
    return rows, total
