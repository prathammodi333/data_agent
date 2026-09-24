"""Map explicit payment method questions onto values stored in the demo DB."""

import re
from typing import NamedTuple


class PaymentIntent(NamedTuple):
    methods: tuple[str, ...]
    count: bool
    label: str


_ACTION = r"(?:filter|show(?: me)?|list|find|display|which|what|how many|count|number of)"
_METHOD = r"(?:credit[_ -]?cards?|debit[_ -]?cards?|pay[_ -]?pal|cards?|credit (?:and|or) debit cards?)"
_AFTER_PAYMENT = re.compile(
    rf"\A{_ACTION} (?:the )?payments?"
    rf"(?: (?:that )?(?:were )?made)?"
    rf" (?:by (?:(?:the )?payment )?method (?:of )?|using |with |via |by |for )"
    rf"(?:a |the )?(?P<method>{_METHOD})\Z",
    re.IGNORECASE,
)
_BEFORE_PAYMENT = re.compile(
    rf"\A{_ACTION} (?:the )?(?P<method>{_METHOD}) payments?\Z",
    re.IGNORECASE,
)


def payment_intent(question: str) -> PaymentIntent | None:
    """Recognize an entire supported question; never interpolate visitor text into SQL."""
    cleaned = " ".join(question.strip().split()).rstrip("?.!")
    match = _AFTER_PAYMENT.fullmatch(cleaned) or _BEFORE_PAYMENT.fullmatch(cleaned)
    if not match:
        return None
    method = match.group("method").lower().replace("-", "_").replace(" ", "_")
    if method in {"credit_card", "credit_cards"}:
        methods, label = ("credit_card",), "credit card"
    elif method in {"debit_card", "debit_cards"}:
        methods, label = ("debit_card",), "debit card"
    elif method in {"pay_pal", "paypal"}:
        methods, label = ("paypal",), "PayPal"
    else:
        methods, label = ("credit_card", "debit_card"), "credit or debit card"
    count = bool(re.match(r"\A(?:how many|count|number of)\b", cleaned, re.IGNORECASE))
    return PaymentIntent(methods, count, label)
