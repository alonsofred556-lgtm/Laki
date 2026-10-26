"""Per-user bot policy layered beneath the global operator kill switch."""

from decimal import Decimal

from .models import BotControl, UserBotControl


BOT_MODES = {"external-bot", "internal-bot"}


def ensure_user_bot_control(identity):
    control, _ = UserBotControl.objects.get_or_create(owner=identity)
    return control


def effective_user_policy(identity, global_control=None):
    global_control = global_control or BotControl.objects.get_or_create(scope="global")[0]
    control = ensure_user_bot_control(identity)
    return {
        "walletMode": control.wallet_mode,
        "enabled": bool(control.enabled),
        "paused": bool(control.paused),
        "minNetBps": max(int(global_control.min_net_bps), int(control.min_net_bps)),
        "maxNotionalUsd": float(min(Decimal(global_control.max_notional_usd), Decimal(control.max_notional_usd))),
        "userEnabled": bool(control.enabled),
        "userPaused": bool(control.paused),
        "globalEnabled": bool(global_control.enabled),
        "globalPaused": bool(global_control.paused),
    }
