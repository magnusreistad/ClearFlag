import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean, stdev

from app.models import Transaction

# Earth's mean radius in miles, for the haversine formula below.
EARTH_RADIUS_MILES = 3958.8

# SCRUM-18: mirrors amount deviation's (SCRUM-19) and new-merchant risk's
# (SCRUM-20) z-score approach, applied to a transaction's distance from the
# user's "home cluster" -- the centroid of their prior transaction locations
# -- rather than to spend amount. The design doc's wording ("distance
# exceeds threshold from typical location cluster") is satisfied by treating
# that threshold as adaptive per user rather than a single fixed-mile value:
# a user whose life spans a wide region (frequent travel) and a user who
# never leaves one neighborhood need different absolute thresholds for
# "far", and an adaptive stdev-based cutoff gives that without hardcoding
# per-user behavior.
DEFAULT_STD_DEV_THRESHOLD = 3

# Need enough prior transactions to make a "typical location cluster"
# meaningful. Mirrors amount deviation / new-merchant risk's MIN_HISTORY_COUNT.
MIN_HISTORY_COUNT = 5

# Floor for the denominator in the z-score, same rationale as amount
# deviation's MIN_STD_DEV_FLOOR: a user with a tightly clustered history
# (e.g. transactions consistently within a few miles of home) would
# otherwise have a small, ordinary trip across town register as an enormous
# number of "standard deviations" and trip the rule on noise. 10 miles
# tolerates normal same-metro variation (a different neighborhood, the next
# town over) while a genuinely distant excursion still clears the bar by a
# wide margin.
MIN_STD_DEV_FLOOR_MILES = 10.0


@dataclass(frozen=True)
class RuleHit:
    transaction_id: int
    rationale: str


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_MILES * c


def evaluate_geographic_anomaly(
    transactions: Sequence[Transaction],
    *,
    std_dev_threshold: int = DEFAULT_STD_DEV_THRESHOLD,
    min_history: int = MIN_HISTORY_COUNT,
) -> list[RuleHit]:
    """Flag a transaction when its distance from the user's typical location
    cluster is more than `std_dev_threshold` standard deviations above the
    user's own historical distance-from-cluster.

    Transactions are walked in timestamp order. For each one, its "typical
    location cluster" is the centroid (mean lat, mean lon) of the
    transactions that precede it -- computed fresh at each point in time, so
    a user's cluster can drift as their history grows. A transaction is
    skipped (never flagged) until at least `min_history` prior transactions
    exist.

    The centroid is a plain arithmetic mean of lat/lon, not a true spherical
    centroid -- a reasonable simplification for a single user's locally
    clustered shopping history, though it would misbehave near the
    antimeridian or poles.

    Only unusually *far* distances are flagged -- an unusually close
    distance isn't a fraud signal.

    Expects `transactions` to already be scoped to a single user.
    """
    ordered = sorted(transactions, key=lambda t: t.timestamp)

    hits: list[RuleHit] = []
    for i, t in enumerate(ordered):
        history = ordered[:i]
        if len(history) < min_history:
            continue

        centroid_lat = mean(h.latitude for h in history)
        centroid_lon = mean(h.longitude for h in history)

        historical_distances = [
            _haversine_miles(centroid_lat, centroid_lon, h.latitude, h.longitude) for h in history
        ]
        current_distance = _haversine_miles(centroid_lat, centroid_lon, t.latitude, t.longitude)

        historical_mean = mean(historical_distances)
        if current_distance <= historical_mean:
            continue

        historical_stdev = max(stdev(historical_distances), MIN_STD_DEV_FLOOR_MILES)
        z_score = (current_distance - historical_mean) / historical_stdev
        if z_score > std_dev_threshold:
            hits.append(
                RuleHit(
                    transaction_id=t.id,
                    rationale=(
                        f"Flagged: this transaction occurred in {t.location_label}, "
                        f"far from where you usually shop."
                    ),
                )
            )

    return hits
