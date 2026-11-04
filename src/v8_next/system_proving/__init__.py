"""Full-chain system proving ground — Python port of v8-core/src/system_proving
(V8.5 M3, D-147/D-149, Rules 19-20).

The Rust subsystem is quarantined in v8-core ("sole live consumer is
tests/system_proving_ground.rs; growth into production gates requires authority"), so this
port comes over as an instrument, not as a gate: it measures a run and attributes failures,
and it cannot by itself certify anything.
"""

from v8_next.system_proving.attribution import (
    FailureAttributionBreakdown,
    FailureDomain,
)
from v8_next.system_proving.metrics import SystemRobustnessVector
from v8_next.system_proving.receipt import SystemProvingGroundReceipt

__all__ = [
    "FailureAttributionBreakdown",
    "FailureDomain",
    "SystemProvingGroundReceipt",
    "SystemRobustnessVector",
]
