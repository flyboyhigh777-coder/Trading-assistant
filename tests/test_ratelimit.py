import pytest

from uzinvest.ratelimit import RateLimiter


def test_allows_up_to_the_limit_then_blocks():
    limiter = RateLimiter(limit=3, window=60.0)
    assert all(limiter.check(1, now=t).allowed for t in (0.0, 1.0, 2.0))
    blocked = limiter.check(1, now=3.0)
    assert not blocked.allowed
    assert blocked.retry_after == pytest.approx(57.0)


def test_the_window_slides():
    limiter = RateLimiter(limit=2, window=10.0)
    limiter.check(1, now=0.0)
    limiter.check(1, now=1.0)
    assert not limiter.check(1, now=5.0).allowed
    assert limiter.check(1, now=10.5).allowed   # the first hit has aged out


def test_users_have_separate_buckets():
    # The whole point of keying on the user: one noisy user must not throttle
    # everyone sharing their IP.
    limiter = RateLimiter(limit=1, window=60.0)
    assert limiter.check(1, now=0.0).allowed
    assert not limiter.check(1, now=0.1).allowed
    assert limiter.check(2, now=0.2).allowed


def test_reset_clears_one_user_or_everyone():
    limiter = RateLimiter(limit=1, window=60.0)
    limiter.check(1, now=0.0)
    limiter.check(2, now=0.0)
    limiter.reset(1)
    assert limiter.check(1, now=0.1).allowed
    assert not limiter.check(2, now=0.1).allowed
    limiter.reset()
    assert limiter.check(2, now=0.2).allowed


@pytest.mark.parametrize("limit,window", [(0, 60.0), (5, 0.0), (5, -1.0)])
def test_rejects_nonsense_configuration(limit, window):
    with pytest.raises(ValueError):
        RateLimiter(limit=limit, window=window)
