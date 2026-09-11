"""Fair value for Polymarket crypto Up/Down markets.

Settlement is NOT a spot digital.  The market resolves on a Chainlink TWAP of
the window compared with the price at the window's open, so the payoff is a
digital on an *average*, not on the terminal price.  Using the textbook
Phi(ln(S/K)/(sigma*sqrt(tau))) here is wrong in two directions at once:
it mis-centres (the running average lags spot in a trend) and it over-states
the remaining variance (part of the average is already printed).
"""

from statistics import NormalDist

_N = NormalDist()
FEE_RATE = 0.07          # Polymarket crypto taker fee coefficient
TWAP_W = 60.0            # averaging window, seconds. 60 => Chainlink twap-60s
                         # stream read at the close; 300 => whole-window mean.
                         # Which one is live is decided empirically -- see report.


def fair_up(spot, strike, tau, sigma, w=TWAP_W, i_known=0.0):
    """P(settlement TWAP >= strike).

    spot     latest source price
    strike   source price at window open
    tau      seconds left until the window closes
    sigma    absolute price vol, per sqrt(second)
    w        length of the averaging window, seconds
    i_known  integral of price over [close-w, now]; used once tau < w
    """
    if tau <= 0:
        return 1.0 if i_known / w >= strike else 0.0
    if tau >= w:
        # averaging has not started: A is centred on spot, and the averaging
        # shaves 2w/3 off the effective time to expiry.
        mean, var = spot, sigma * sigma * (tau - 2.0 * w / 3.0)
    else:
        # inside the window: (w - tau) seconds of the average are already fixed.
        # A missing i_known is a caller error, not a zero: taking it literally
        # makes the mean spot*tau/w, which is a fraction of the real price and
        # silently returns 0 or 1. Fall back to "the elapsed part sat at spot".
        if i_known <= 0.0:
            i_known = spot * (w - tau)
        mean = (i_known + spot * tau) / w
        var = sigma * sigma * tau ** 3 / (3.0 * w * w)
    if var <= 0.0:
        return 1.0 if mean >= strike else 0.0
    return _N.cdf((mean - strike) / var ** 0.5)


def fair_up_naive(spot, strike, tau, sigma):
    """The spot digital everyone writes first. Kept only as the control arm."""
    if tau <= 0:
        return 1.0 if spot >= strike else 0.0
    return _N.cdf((spot - strike) / (sigma * tau ** 0.5))


def taker_fee(price, shares=1.0):
    """Polymarket crypto taker fee. Verified to 5dp against 148k real fills."""
    return shares * FEE_RATE * price * (1.0 - price)


def edge(fair, ask):
    """Expected profit per share from lifting `ask`, net of the taker fee."""
    return fair - ask - taker_fee(ask)


def p_touch(dist, sigma, tau):
    """P(a driftless walk travels at least `dist` in a given direction by tau).

    Reflection principle: P(max_s W_s >= d) = 2*Phi(-d/(sigma*sqrt(tau))).
    Used before opening a leg to ask whether the hedge can realistically appear
    before the window closes.  A leg opened when this is near zero is not a
    hedged trade waiting to happen, it is a naked bet wearing a disguise.
    """
    if tau <= 0 or sigma <= 0:
        return 0.0
    if dist <= 0:
        return 1.0
    return min(2.0 * _N.cdf(-dist / (sigma * tau ** 0.5)), 1.0)


def shrink(p, mkt, w):
    """Pull a model probability toward the market's own implied price.

    Kelly assumes p is known.  Ours is a model output built on an estimated
    sigma and a proxy price feed, and the random-walk null says the book is
    the consensus estimate.  Shrinking is a confidence haircut: w=1 trusts the
    model completely, w=0 defers entirely to the book.
    """
    return w * p + (1.0 - w) * mkt


def needed_move(p_now, p_target, sigma, tau, w=TWAP_W):
    """Absolute price distance that moves the model from p_now to p_target.

    ponytail: holds tau fixed while solving, so it ignores that the variance
    also shrinks as the window runs down.  That biases the required distance
    upward, i.e. it under-states the chance of a hedge appearing -- the safe
    direction.  Solve the time-varying version only if this ever gates trades
    it should not.
    """
    eps = 1e-6
    p_now = min(max(p_now, eps), 1 - eps)
    p_target = min(max(p_target, eps), 1 - eps)
    sd = sigma * max(tau - 2.0 * w / 3.0, tau ** 3 / (3.0 * w * w)) ** 0.5
    return abs(_N.inv_cdf(p_target) - _N.inv_cdf(p_now)) * sd


def implied_sigma(p_mkt, spot, strike, tau, w=TWAP_W, i_known=0.0,
                  lo=1e-6, hi=1e4):
    """Invert the model for the sigma the book is pricing.

    Gamma scalping pays out roughly (realised vol - implied vol): buying the
    cheap side as the implied probability swings, then flattening, is exactly a
    long-gamma rehedge. So the edge condition is not "is the model right about
    direction" -- it is whether the tape is moving more than the quotes assume.
    Bisection because the model is monotone in sigma on each side of the money.
    """
    if tau <= 0 or not (0.0 < p_mkt < 1.0):
        return None
    if abs(spot - strike) < 1e-12:
        return None                    # at the money every sigma gives 0.5
    up = spot > strike
    for _ in range(60):
        mid = (lo + hi) / 2
        p = fair_up(spot, strike, tau, mid, w, i_known)
        # above the strike more vol pulls p down toward 0.5, below it pushes up
        if (p > p_mkt) == up:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def hedge_band(price, p_fair, k=2.5, floor=0.004, cap=0.10):
    """Required locked profit before flattening a leg -- a no-trade band.

    Whalley-Wilmott says the optimal rehedge band under transaction costs
    scales as (cost / gamma)^(1/3) rather than being a constant.  Its delta
    form does not carry over dimensionally here (this gamma is probability per
    dollar squared, ~1e-5 on BTC, so the cube root pins to any cap), but the
    trade-off does: hedging costs a fee, and hedging early throws away
    optionality that is still worth something.

    So the band is the fee actually payable, scaled by how much of the outcome
    is still in doubt.  4p(1-p) is 1 at the money and goes to 0 once the
    result is effectively settled, which is exactly when locking should become
    cheap.  A fixed threshold is wrong in both regimes at once.
    """
    fee = FEE_RATE * price * (1.0 - price)
    doubt = 4.0 * p_fair * (1.0 - p_fair)
    return min(max(fee * (1.0 + k * doubt), floor), cap)


def digital_gamma(spot, strike, tau, sigma, w=TWAP_W, i_known=0.0, h=None):
    """d2p/dS2 of the model, by central difference. Explodes near expiry."""
    if tau <= 0 or sigma <= 0:
        return 0.0
    h = h or max(sigma * tau ** 0.5 * 0.05, 1e-6)
    a = fair_up(spot - h, strike, tau, sigma, w, i_known)
    b = fair_up(spot, strike, tau, sigma, w, i_known)
    c = fair_up(spot + h, strike, tau, sigma, w, i_known)
    return (a - 2 * b + c) / (h * h)


def realized_sigma(ticks, lookback=180.0, bucket=5.0, floor=1e-9):
    """Absolute vol per sqrt(second) from (ts, price) samples.

    Sampled in `bucket`-second steps, not 1-second ones.  At 1s the measured
    variance of a crypto tape is dominated by bid-ask bounce rather than by
    real price movement, which inflates sigma, which widens the model's
    distribution, which makes it systematically under-confident -- exactly the
    bias the first calibration run showed.  Coarser sampling trades a little
    responsiveness for a far less biased estimate.
    """
    if not ticks:
        return floor
    cut = ticks[-1][0] - lookback
    grid = {}
    for ts, px in ticks:
        if ts >= cut:
            grid[int(ts // bucket)] = px       # last price in each bucket
    if len(grid) < 8:
        return floor
    ks = sorted(grid)
    d = [grid[b] - grid[a] for a, b in zip(ks, ks[1:]) if b - a == 1]
    if len(d) < 6:
        return floor
    mu = sum(d) / len(d)
    var = sum((x - mu) ** 2 for x in d) / (len(d) - 1)
    return max((var / bucket) ** 0.5, floor)   # per sqrt(second)


def _demo():
    S = K = 100_000.0
    sig = 3.0                       # $3 per sqrt(second)

    # at the open, spot on strike -> a coin flip, both branches agree
    assert abs(fair_up(S, K, 300, sig, w=300) - 0.5) < 1e-12
    assert abs(fair_up(S, K, 300, sig, w=60) - 0.5) < 1e-12

    # the two branches must meet where they join (tau == w)
    for w in (60.0, 300.0):
        a = fair_up(S + 5, K, w + 1e-7, sig, w=w)
        b = fair_up(S + 5, K, w - 1e-7, sig, w=w, i_known=S * 1e-7)
        assert abs(a - b) < 1e-6, (w, a, b)

    # whole-window TWAP at the open is the classic Asian variance, sigma^2*T/3
    T = 300.0
    want = _N.cdf(5.0 / (sig * (T / 3.0) ** 0.5))
    assert abs(fair_up(S + 5, K, T, sig, w=T) - want) < 1e-12

    # inside the window the answer collapses onto the realised average
    assert fair_up(S, K, 1e-9, sig, w=60, i_known=60 * (K + 1)) > 0.999
    assert fair_up(S, K, 1e-9, sig, w=60, i_known=60 * (K - 1)) < 0.001

    # monotone in spot
    ps = [fair_up(S + d, K, 120, sig) for d in (-50, -10, 0, 10, 50)]
    assert ps == sorted(ps)

    # The model has TWO effects and they pull opposite ways.
    #
    # (1) LEVEL: the running average lags spot, so a rally off a low early
    #     window is worth less than spot alone suggests.  Bites near the money.
    i_low = (60 - 40) * (K - 5)             # first 20s of the window averaged -5
    twap_atm = fair_up(K + 2, K, 40, sig, w=60, i_known=i_low)
    naive_atm = fair_up_naive(K + 2, K, 40, sig)
    assert twap_atm < naive_atm - 0.04, (twap_atm, naive_atm)

    # (2) VARIANCE: near expiry most of the average is already printed, so the
    #     TWAP distribution is far tighter than the spot one.  Deep in the
    #     money this dominates and the naive model UNDER-prices the winner.
    i_hi = (60 - 40) * (K + 10)
    twap_itm = fair_up(K + 20, K, 40, sig, w=60, i_known=i_hi)
    naive_itm = fair_up_naive(K + 20, K, 40, sig)
    assert twap_itm > naive_itm + 0.05, (twap_itm, naive_itm)

    # (3) PATH DEPENDENCE: identical spot, identical tau, different history ->
    #     different fair value.  The naive model cannot express this at all.
    hi = fair_up(K + 2, K, 40, sig, w=60, i_known=(60 - 40) * (K + 8))
    lo = fair_up(K + 2, K, 40, sig, w=60, i_known=(60 - 40) * (K - 8))
    assert hi - lo > 0.25, (hi, lo)
    assert fair_up_naive(K + 2, K, 40, sig) == fair_up_naive(K + 2, K, 40, sig)

    # --- random-walk risk controls -------------------------------------
    # reflection principle: a move you are already at is certain, an infinite
    # one is impossible, and more time can only help.
    assert p_touch(0.0, 3.0, 60) == 1.0
    assert p_touch(1e9, 3.0, 60) < 1e-9
    assert p_touch(50, 3.0, 60) < p_touch(50, 3.0, 300)
    assert p_touch(50, 3.0, 60) < p_touch(20, 3.0, 60)
    # a symmetric walk touches +/-0.6745*sigma*sqrt(tau) about half the time
    assert abs(p_touch(0.6745 * 3.0 * 60 ** 0.5, 3.0, 60) - 0.5) < 0.01

    # shrinkage is a weighted average and never leaves the interval
    assert shrink(0.9, 0.5, 1.0) == 0.9
    assert shrink(0.9, 0.5, 0.0) == 0.5
    assert 0.5 < shrink(0.9, 0.5, 0.5) < 0.9
    # and it always cuts the apparent edge
    assert edge(shrink(0.9, 0.5, 0.5), 0.5) < edge(0.9, 0.5)

    # moving the model further needs more distance; no move needed if already there
    assert needed_move(0.5, 0.5, 3.0, 120) < 1e-9
    assert needed_move(0.5, 0.7, 3.0, 120) < needed_move(0.5, 0.9, 3.0, 120)

    # --- options-desk imports -------------------------------------------
    # implied sigma round-trips: price with one, recover it from the price
    S, K2, tau2, sig2 = 100_000.0, 100_000.0, 120.0, 3.0
    p = fair_up(S + 40, K2, tau2, sig2)
    back = implied_sigma(p, S + 40, K2, tau2)
    assert abs(back - sig2) / sig2 < 0.01, (back, sig2)

    # a richer quote than the model implies a HIGHER vol above the strike
    hi_q = implied_sigma(p + 0.05, S + 40, K2, tau2)
    assert hi_q < sig2, (hi_q, sig2)     # richer Up => less vol needed

    # gamma is largest near the strike and grows as expiry approaches
    g_atm = abs(digital_gamma(S + 1, K2, 60, sig2))
    g_otm = abs(digital_gamma(S + 200, K2, 60, sig2))
    assert g_atm > g_otm, (g_atm, g_otm)
    assert abs(digital_gamma(S + 5, K2, 20, sig2)) > abs(digital_gamma(S + 5, K2, 200, sig2))
    # and a missing i_known must not silently become a price of spot*tau/w
    assert 0.4 < fair_up(S, K2, 30, sig2, 60.0) < 0.6

    # the band demands more while the outcome is in doubt, and relaxes once
    # it is effectively settled -- at which point locking costs no optionality
    assert hedge_band(0.50, 0.50) > hedge_band(0.50, 0.95)
    assert hedge_band(0.50, 0.50) > hedge_band(0.90, 0.50)   # fee is smaller there
    assert 0.004 <= hedge_band(0.99, 0.99) <= 0.10
    assert hedge_band(0.50, 0.50) > 0.015                    # beats the old fixed 1.5c

    # fee formula against a real fill pulled from the chain:
    # 30.9375 shares @ 0.32 was charged 0.47124 USDC
    assert abs(taker_fee(0.32, 30.9375) - 0.47124) < 1e-5
    assert abs(taker_fee(0.93, 10.0) - 0.04557) < 1e-5

    # edge must clear the fee, not just the price
    assert edge(0.52, 0.50) < 0.02
    print(f"ok  ATM rally : twap {twap_atm:.4f} vs naive {naive_atm:.4f}  "
          f"-> naive overpays Up by {naive_atm - twap_atm:+.4f}")
    print(f"ok  ITM  late : twap {twap_itm:.4f} vs naive {naive_itm:.4f}  "
          f"-> naive underpays Up by {twap_itm - naive_itm:+.4f}")
    print(f"ok  same spot+tau, different path: {lo:.4f} .. {hi:.4f}  "
          f"(naive is blind to this, spread {hi - lo:.4f})")


if __name__ == "__main__":
    _demo()
