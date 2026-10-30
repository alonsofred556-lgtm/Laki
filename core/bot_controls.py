"""Per-user bot policy layered beneath the global operator kill switch."""

from decimal import Decimal

from django.conf import settings

from .models import BotControl, UserBotControl


BOT_MODES = {"external-bot", "internal-bot"}


def global_bot_control():
    control, _ = BotControl.objects.get_or_create(
        scope="global",
        defaults={"compound_profit_bps": int(getattr(settings, "CONI_DEFAULT_COMPOUND_PROFIT_BPS", 7000))},
    )
    return control


def ensure_user_bot_control(identity):
    control, _ = UserBotControl.objects.get_or_create(owner=identity)
    return control


def effective_user_policy(identity, global_control=None):
    global_control = global_control or global_bot_control()
    control = ensure_user_bot_control(identity)
    return {
        "walletMode": control.wallet_mode,
        "enabled": bool(control.enabled),
        "paused": bool(control.paused),
        "minNetBps": max(int(global_control.min_net_bps), int(control.min_net_bps)),
        "maxNotionalUsd": float(min(Decimal(global_control.max_notional_usd), Decimal(control.max_notional_usd))),
        "compoundProfitBps": max(0, min(10_000, int(getattr(global_control, "compound_profit_bps", 7000)), int(getattr(control, "compound_profit_bps", 7000)))),
        "userEnabled": bool(control.enabled),
        "userPaused": bool(control.paused),
        "globalEnabled": bool(global_control.enabled),
        "globalPaused": bool(global_control.paused),
    }
