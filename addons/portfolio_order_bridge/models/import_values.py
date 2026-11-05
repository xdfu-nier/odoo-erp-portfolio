"""Bounded decimal checks and safe diagnostics for quotation processing."""

import unicodedata
from decimal import ROUND_HALF_UP, Decimal, localcontext


class ImportBusinessError(Exception):
    """Only explicitly constructed, non-sensitive diagnostics enter the audit."""

    def __init__(self, code, path, detail):
        super().__init__(code)
        self.code, self.path, self.detail = code, path, detail


def retry_reason(value):
    if not isinstance(value, str):
        raise ImportBusinessError("retry_reason", "reason", "Enter a reason for the manual retry.")
    value = value.strip()
    if not value or len(value) > 512 or any(
        char == "\x00" or unicodedata.category(char) == "Cs" for char in value
    ):
        raise ImportBusinessError("retry_reason", "reason", "Use clean text of at most 512 characters.")
    return value


def check_precision(data, currency_rounding, discount_digits):
    with localcontext() as context:
        context.prec = 64
        price_step = Decimal(str(currency_rounding))
        discount_step = Decimal(1).scaleb(-discount_digits)
        for index, line in enumerate(data["lines"]):
            for name, step, code in (
                ("unit_price", price_step, "currency_precision"),
                ("discount", discount_step, "discount_precision"),
            ):
                if Decimal(line[name]) % step:
                    raise ImportBusinessError(
                        code, f"lines[{index}].{name}",
                        "The submitted value cannot be represented with the current precision.",
                    )


def exact_float(value, path):
    number = float(value)
    if Decimal(str(number)) != Decimal(value):
        raise ImportBusinessError("native_precision", path, "The native numeric field would lose precision.")
    return number


def rounded_amount(line, rounding):
    with localcontext() as context:
        context.prec = 64
        step = Decimal(str(rounding))
        amount = (Decimal(line["quantity"]) * Decimal(line["unit_price"])
                  * (Decimal(1) - Decimal(line["discount"]) / 100))
        return (amount / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step
