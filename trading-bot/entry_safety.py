"""Fail-closed gate for NEW orders. Does not interfere with closing positions."""
import os

class EntryBlocked(RuntimeError):
    pass

def require_entry_permission(*, stop_loss=None, lot=None, market=False):
    """Explicit opt-in plus mandatory broker-side stop for any new exposure.

    This is an interim safety interlock, NOT a complete live risk gate.
    """
    if os.environ.get('RH_ALLOW_NEW_ORDERS', '').strip() != 'YES_I_ACCEPT_RISK':
        raise EntryBlocked('new orders disabled: RH_ALLOW_NEW_ORDERS is not explicitly enabled')
    try:
        volume = float(lot)
        sl = float(stop_loss)
    except (TypeError, ValueError, OverflowError):
        raise EntryBlocked('new orders require a valid lot and broker-side stop loss')
    if not (0 < volume <= 0.01):
        raise EntryBlocked('lot exceeds interim 0.01 cap or is invalid')
    if not (sl > 0 and sl < float('inf')):
        raise EntryBlocked('new orders require a positive broker-side stop loss')
    if market:
        raise EntryBlocked('market entries blocked until stop-loss and reconciliation are integrated')
    # No live broker snapshot/reconciliation is wired into this interlock yet.
    # Deliberately refuse ALL new entries even when the legacy opt-in is set.
    raise EntryBlocked('new orders locked until live risk gate and broker reconciliation are integrated')
