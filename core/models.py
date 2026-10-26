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


class LedgerAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(WalletIdentity, null=True, blank=True, on_delete=models.PROTECT, db_column="owner_id")
    asset = models.CharField(max_length=16)
    chain = models.CharField(max_length=32)
    kind = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_accounts"
        constraints = [models.UniqueConstraint(fields=["owner", "asset", "chain", "kind"], name="ledger_account_owner_asset_chain_kind")]


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
    asset = models.CharField(max_length=16)
    chain = models.CharField(max_length=32)
    amount_base_units = models.DecimalField(max_digits=78, decimal_places=0)
    destination = models.CharField(max_length=64)
    status = models.CharField(max_length=32)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "funding_intents"


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


class Execution(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_identity = models.ForeignKey(WalletIdentity, null=True, on_delete=models.PROTECT, db_column="user_identity_id")
    slug = models.CharField(max_length=80)
    state = models.CharField(max_length=32)
    expected_net_bps = models.IntegerField(null=True, blank=True)
    expected_profit_usd = models.DecimalField(max_digits=30, decimal_places=8, null=True, blank=True)
    quote_hash = models.CharField(max_length=128, null=True, blank=True)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "executions"
