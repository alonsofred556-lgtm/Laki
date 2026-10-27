import uuid
from django.db import models


class WalletChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chain = models.CharField(max_length=32)
    address = models.CharField(max_length=64)
    message = models.TextField()
    nonce = models.CharField(max_length=96, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "wallet_challenges"


class WalletIdentity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chain = models.CharField(max_length=32)
    address = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    last_authenticated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wallet_identities"
        constraints = [models.UniqueConstraint(fields=["chain", "address"], name="wallet_identity_chain_address")]


class BotWallet(models.Model):
    """Per-user bot-wallet identity; no private key is stored in Django."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(WalletIdentity, on_delete=models.PROTECT, related_name="bot_wallets", db_column="owner_id")
    chain = models.CharField(max_length=32)
    address = models.CharField(max_length=64, blank=True)
    mode = models.CharField(max_length=24, default="external-bot")
    custody = models.CharField(max_length=24, default="audited-signer")
    status = models.CharField(max_length=32, default="awaiting-signer-provision")
    source_address = models.CharField(max_length=64, blank=True)
    signer_key_ref = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bot_wallets"
        constraints = [
            models.UniqueConstraint(fields=["owner", "chain", "mode"], name="bot_wallet_owner_chain_mode"),
            models.UniqueConstraint(fields=["chain", "address"], condition=~models.Q(address=""), name="bot_wallet_chain_address_once"),
        ]
        indexes = [models.Index(fields=["owner", "status"], name="bot_wallet_owner_status")]


class BotWalletProvisionRequest(models.Model):
    """Durable lease for an audited signer/MPC wallet-provisioning request."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bot_wallet = models.OneToOneField(BotWallet, on_delete=models.PROTECT, related_name="provision_request")
    status = models.CharField(max_length=24, default="pending")
    attempt_count = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "bot_wallet_provision_requests"
        indexes = [models.Index(fields=["status", "lease_until"], name="bot_provision_status_lease")]


class BotWalletTransfer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=128, unique=True)
    bot_wallet = models.ForeignKey(BotWallet, on_delete=models.PROTECT, related_name="transfers")
    owner = models.ForeignKey(WalletIdentity, on_delete=models.PROTECT, db_column="owner_id")
    direction = models.CharField(max_length=16)
    asset = models.CharField(max_length=16)
    chain = models.CharField(max_length=32)
    amount_base_units = models.DecimalField(max_digits=78, decimal_places=0)
    destination = models.CharField(max_length=64)
    state = models.CharField(max_length=32, default="pending-approval")
    tx_hash = models.CharField(max_length=160, blank=True)
    failure_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bot_wallet_transfers"
        indexes = [models.Index(fields=["bot_wallet", "created_at"], name="bot_transfer_wallet_created")]


class BotWalletTransferEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=128, unique=True)
    transfer = models.ForeignKey(BotWalletTransfer, on_delete=models.PROTECT, related_name="events")
    event = models.CharField(max_length=24)
    tx_hash = models.CharField(max_length=160, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bot_wallet_transfer_events"
        indexes = [models.Index(fields=["transfer", "created_at"], name="bot_transfer_event_created")]


class LedgerAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(WalletIdentity, null=True, blank=True, on_delete=models.PROTECT, db_column="owner_id")
    bot_wallet = models.ForeignKey("BotWallet", null=True, blank=True, on_delete=models.PROTECT, related_name="ledger_accounts")
    asset = models.CharField(max_length=16)
    chain = models.CharField(max_length=32)
    kind = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_accounts"
        constraints = [
            models.UniqueConstraint(fields=["owner", "asset", "chain", "kind"], condition=models.Q(bot_wallet__isnull=True, owner__isnull=False), name="ledger_user_asset_chain_kind"),
            models.UniqueConstraint(fields=["bot_wallet", "asset", "kind"], condition=models.Q(bot_wallet__isnull=False), name="ledger_bot_asset_kind"),
            models.UniqueConstraint(fields=["asset", "chain", "kind"], condition=models.Q(owner__isnull=True, bot_wallet__isnull=True), name="ledger_system_asset_chain_kind"),
        ]


class LedgerEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference_type = models.CharField(max_length=64)
    reference_id = models.CharField(max_length=128)
    debit_account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="debits", db_column="debit_account")
    credit_account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="credits", db_column="credit_account")
    asset = models.CharField(max_length=16)
    amount_base_units = models.DecimalField(max_digits=78, decimal_places=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_entries"
        constraints = [models.UniqueConstraint(fields=["reference_type", "reference_id", "debit_account", "credit_account"], name="ledger_entry_idempotency")]


class FundingIntent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet_identity = models.ForeignKey(WalletIdentity, null=True, on_delete=models.PROTECT, db_column="wallet_identity_id")
    bot_wallet = models.ForeignKey("BotWallet", null=True, blank=True, on_delete=models.PROTECT, related_name="funding_intents")
    asset = models.CharField(max_length=16)
    chain = models.CharField(max_length=32)
    amount_base_units = models.DecimalField(max_digits=78, decimal_places=0)
    destination = models.CharField(max_length=64)
    status = models.CharField(max_length=32)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "funding_intents"


class DepositObservation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_key = models.CharField(max_length=180, unique=True)
    funding_intent = models.ForeignKey(FundingIntent, null=True, blank=True, on_delete=models.PROTECT, db_column="funding_intent_id")
    wallet_identity = models.ForeignKey(WalletIdentity, null=True, blank=True, on_delete=models.PROTECT, db_column="wallet_identity_id")
    asset = models.CharField(max_length=16)
    chain = models.CharField(max_length=32)
    sender = models.CharField(max_length=64)
    destination = models.CharField(max_length=64)
    tx_hash = models.CharField(max_length=160)
    event_index = models.IntegerField(default=0)
    block_number = models.BigIntegerField(null=True, blank=True)
    block_hash = models.CharField(max_length=160, null=True, blank=True)
    amount_base_units = models.DecimalField(max_digits=78, decimal_places=0)
    confirmations = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=32, default="pending-verification")
    rejection_code = models.CharField(max_length=64, null=True, blank=True)
    raw_event = models.JSONField(default=dict)
    observed_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    credited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "deposit_observations"
        indexes = [
            models.Index(fields=["status", "chain"], name="deposit_status_chain"),
            models.Index(fields=["wallet_identity", "observed_at"], name="deposit_wallet_observed"),
        ]


class ChainCursor(models.Model):
    """Durable cursor for a single chain scanner; never advance on a failed batch."""

    key = models.CharField(max_length=64, primary_key=True)
    chain = models.CharField(max_length=32)
    cursor_number = models.BigIntegerField(null=True, blank=True)
    cursor_hash = models.CharField(max_length=160, null=True, blank=True)
    status = models.CharField(max_length=32, default="never-run")
    last_scan_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "chain_cursors"
        indexes = [models.Index(fields=["chain", "status"], name="cursor_chain_status")]


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=96)
    actor_type = models.CharField(max_length=32, default="system")
    actor_id = models.CharField(max_length=128, null=True, blank=True)
    subject_type = models.CharField(max_length=64, null=True, blank=True)
    subject_id = models.CharField(max_length=128, null=True, blank=True)
    result = models.CharField(max_length=32, default="success")
    request_id = models.CharField(max_length=128, null=True, blank=True)
    metadata = models.JSONField(default=dict)
    metadata_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audit_events"
        indexes = [
            models.Index(fields=["action", "created_at"], name="audit_action_created"),
            models.Index(fields=["subject_type", "subject_id"], name="audit_subject"),
        ]


class ReconciliationRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    worker = models.CharField(max_length=64, default="ledger-reconciler")
    status = models.CharField(max_length=32, default="running")
    checked_count = models.PositiveIntegerField(default=0)
    anomaly_count = models.PositiveIntegerField(default=0)
    details = models.JSONField(default=dict)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "reconciliation_runs"
        indexes = [models.Index(fields=["status", "started_at"], name="recon_status_started")]


class WithdrawalRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=128, unique=True)
    wallet_identity = models.ForeignKey(WalletIdentity, on_delete=models.PROTECT, db_column="wallet_identity_id")
    asset = models.CharField(max_length=16)
    chain = models.CharField(max_length=32)
    amount_base_units = models.DecimalField(max_digits=78, decimal_places=0)
    destination = models.CharField(max_length=64)
    status = models.CharField(max_length=32, default="pending-approval")
    nonce = models.CharField(max_length=96, unique=True)
    tx_hash = models.CharField(max_length=160, null=True, blank=True)
    failure_code = models.CharField(max_length=64, null=True, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    approved_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "withdrawal_requests"
        indexes = [
            models.Index(fields=["wallet_identity", "status"], name="withdrawal_wallet_status"),
            models.Index(fields=["status", "requested_at"], name="withdrawal_status_requested"),
        ]


class FeeEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_identity = models.ForeignKey(WalletIdentity, null=True, on_delete=models.PROTECT, db_column="user_identity_id")
    execution_id = models.CharField(max_length=128, null=True, blank=True)
    asset = models.CharField(max_length=16)
    chain = models.CharField(max_length=32)
    amount_base_units = models.DecimalField(max_digits=78, decimal_places=0)
    recipient = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default="accrued")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fee_events"


class BotControl(models.Model):
    scope = models.CharField(max_length=32, primary_key=True)
    enabled = models.BooleanField(default=False)
    paused = models.BooleanField(default=True)
    min_net_bps = models.IntegerField(default=50)
    max_notional_usd = models.DecimalField(max_digits=30, decimal_places=8, default=25)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "bot_controls"


class UserBotControl(models.Model):
    """Per-identity bot preference; the global operator gate still wins."""

    owner = models.OneToOneField(WalletIdentity, primary_key=True, on_delete=models.PROTECT, related_name="bot_control", db_column="owner_id")
    wallet_mode = models.CharField(max_length=24, default="external-bot")
    enabled = models.BooleanField(default=True)
    paused = models.BooleanField(default=False)
    min_net_bps = models.IntegerField(default=100)
    max_notional_usd = models.DecimalField(max_digits=30, decimal_places=8, default=25)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_bot_controls"


class Execution(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_identity = models.ForeignKey(WalletIdentity, null=True, on_delete=models.PROTECT, db_column="user_identity_id")
    bot_wallet = models.ForeignKey(BotWallet, null=True, blank=True, on_delete=models.PROTECT, related_name="executions")
    sell_bot_wallet = models.ForeignKey(BotWallet, null=True, blank=True, on_delete=models.PROTECT, related_name="sell_executions")
    opportunity = models.ForeignKey("ArbitrageOpportunity", null=True, blank=True, on_delete=models.PROTECT, related_name="executions")
    slug = models.CharField(max_length=80)
    state = models.CharField(max_length=32)
    expected_net_bps = models.IntegerField(null=True, blank=True)
    expected_profit_usd = models.DecimalField(max_digits=30, decimal_places=8, null=True, blank=True)
    realized_profit_usd = models.DecimalField(max_digits=30, decimal_places=8, null=True, blank=True)
    realized_at = models.DateTimeField(null=True, blank=True)
    quote_hash = models.CharField(max_length=128, null=True, blank=True)
    quote_artifact = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "executions"
        constraints = [models.UniqueConstraint(fields=["opportunity", "user_identity"], condition=models.Q(opportunity__isnull=False), name="execution_user_opportunity_once")]


class ExecutionLeg(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution = models.ForeignKey(Execution, on_delete=models.PROTECT, related_name="legs")
    kind = models.CharField(max_length=16)
    chain = models.CharField(max_length=64)
    state = models.CharField(max_length=32, default="awaiting-signer")
    quote_hash = models.CharField(max_length=128, blank=True)
    tx_hash = models.CharField(max_length=160, blank=True)
    bridge_guid = models.CharField(max_length=128, blank=True)
    amount_in_base_units = models.DecimalField(max_digits=78, decimal_places=0, default=0)
    amount_out_base_units = models.DecimalField(max_digits=78, decimal_places=0, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "execution_legs"
        constraints = [models.UniqueConstraint(fields=["execution", "kind"], name="execution_leg_kind_once")]
        indexes = [models.Index(fields=["execution", "state"], name="execution_leg_state")]


class ExecutionEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=128, unique=True)
    execution = models.ForeignKey(Execution, on_delete=models.PROTECT, related_name="events")
    leg = models.ForeignKey(ExecutionLeg, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    event = models.CharField(max_length=32)
    tx_hash = models.CharField(max_length=160, blank=True)
    bridge_guid = models.CharField(max_length=128, blank=True)
    realized_profit_usd = models.DecimalField(max_digits=30, decimal_places=8, null=True, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "execution_events"
        indexes = [models.Index(fields=["execution", "created_at"], name="execution_event_created")]


class ArbitrageOpportunity(models.Model):
    """Immutable-ish decision snapshot produced by the opportunity scanner.

    This is deliberately separate from Execution: an opportunity can be
    rejected, expire, or be used for paper simulation without ever becoming
    an instruction for a signer.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dedupe_key = models.CharField(max_length=180, unique=True)
    slug = models.CharField(max_length=96)
    symbol = models.CharField(max_length=32)
    buy_chain = models.CharField(max_length=32)
    sell_chain = models.CharField(max_length=32)
    buy_token = models.CharField(max_length=128, null=True, blank=True)
    sell_token = models.CharField(max_length=128, null=True, blank=True)
    notional_usd = models.DecimalField(max_digits=30, decimal_places=8)
    gross_profit_usd = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    total_cost_usd = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    net_profit_usd = models.DecimalField(max_digits=30, decimal_places=8, default=0)
    net_bps = models.IntegerField(default=0)
    state = models.CharField(max_length=32, default="detected")
    quote_hash = models.CharField(max_length=128, null=True, blank=True)
    decision = models.JSONField(default=dict)
    quote_attempts = models.PositiveIntegerField(default=0)
    quote_available_at = models.DateTimeField(null=True, blank=True)
    quote_lease_until = models.DateTimeField(null=True, blank=True)
    quote_last_error = models.CharField(max_length=160, blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "arbitrage_opportunities"
        indexes = [
            models.Index(fields=["slug", "detected_at"], name="arb_slug_detected"),
            models.Index(fields=["state", "detected_at"], name="arb_state_detected"),
            models.Index(fields=["quote_hash", "quote_available_at"], name="arb_quote_queue"),
        ]


class ProfitSettlement(models.Model):
    """A realized-profit payout instruction; it is not a withdrawal shortcut."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    execution = models.OneToOneField(Execution, on_delete=models.PROTECT, related_name="profit_settlement")
    user_identity = models.ForeignKey(WalletIdentity, on_delete=models.PROTECT, db_column="user_identity_id")
    realized_profit_usd = models.DecimalField(max_digits=30, decimal_places=8)
    settlement_asset = models.CharField(max_length=16, default="USDG")
    settlement_chain = models.CharField(max_length=32, default="robinhood")
    settlement_token = models.CharField(max_length=128)
    settlement_price_usd = models.DecimalField(max_digits=30, decimal_places=12)
    amount_base_units = models.DecimalField(max_digits=78, decimal_places=0)
    recipient = models.CharField(max_length=64)
    state = models.CharField(max_length=32, default="pending-approval")
    quote_hash = models.CharField(max_length=128, null=True, blank=True)
    tx_hash = models.CharField(max_length=160, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "profit_settlements"
        indexes = [models.Index(fields=["state", "created_at"], name="profit_state_created")]


class ProfitSettlementEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=128, unique=True)
    settlement = models.ForeignKey(ProfitSettlement, on_delete=models.PROTECT, related_name="events")
    event = models.CharField(max_length=24)
    tx_hash = models.CharField(max_length=160, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "profit_settlement_events"
        indexes = [models.Index(fields=["settlement", "created_at"], name="profit_event_created")]
