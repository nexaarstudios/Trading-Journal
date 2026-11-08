from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")


class PnLError(ValueError):
    """Raised when a P&L cannot be computed from the given inputs."""


@dataclass(frozen=True)
class PnLResult:
    gross: Decimal            # account currency, before costs
    net: Decimal              # gross minus commission, swap and fees
    risk: Decimal | None      # money risked at the stop, account currency
    r_multiple: Decimal | None


def _money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _to_account_currency(amount, price, base, quote, account_currency, rate):
    """Convert an amount in the quote currency into the account currency."""
    if quote == account_currency:
        return amount
    if base == account_currency:
        if price <= 0:
            raise PnLError("Price must be above zero to convert this trade.")
        return amount / price
    if rate is None:
        raise PnLError(
            f"Missing rate: enter the {quote} to {account_currency} rate at close time."
        )
    return amount * rate


def compute_pnl(
    *,
    side,
    entry,
    exit_price,
    lots,
    contract_size,
    base_currency,
    quote_currency,
    account_currency,
    commission=ZERO,
    swap=ZERO,
    fees=ZERO,
    stop=None,
    quote_to_account_rate=None,
    broker_profit=None,
):
    if side == "long":
        direction = 1
    elif side == "short":
        direction = -1
    else:
        raise PnLError(f"Side must be 'long' or 'short', got {side!r}.")
    if lots <= 0:
        raise PnLError("Lots must be above zero.")

    # Rule 1: gross in the quote currency
    gross_quote = (exit_price - entry) * contract_size * lots * direction

    # Rules 2-5: convert, unless the broker's own profit figure was imported
    if broker_profit is not None:
        gross = broker_profit
    else:
        gross = _to_account_currency(
            gross_quote, exit_price, base_currency, quote_currency,
            account_currency, quote_to_account_rate,
        )
    gross = _money(gross)
    net = _money(gross - commission - swap - fees)

    # Rule 6: R-multiple = net P&L / money risked at the stop
    risk = r_multiple = None
    if stop is not None:
        try:
            risk_quote = abs(entry - stop) * contract_size * lots
            risk = _money(
                _to_account_currency(
                    risk_quote, stop, base_currency, quote_currency,
                    account_currency, quote_to_account_rate,
                )
            )
        except PnLError:
            # A broker-supplied profit can arrive with no FX rate. Leave R blank
            # instead of rejecting the trade.
            if broker_profit is None:
                raise
        else:
            if risk > 0:
                r_multiple = (net / risk).quantize(CENT, rounding=ROUND_HALF_UP)
                
    return PnLResult(gross=gross, net=net, risk=risk, r_multiple=r_multiple)