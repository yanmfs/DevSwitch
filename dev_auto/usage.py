from dataclasses import asdict, dataclass
import time


@dataclass
class Availability:
    status: str = "unknown"
    used_percent: float | None = None
    resets_at: float | None = None
    retry_at: float = 0
    observed_at: float = 0
    quota_observed_at: float = 0
    source: str = "not_observed"
    failures: int = 0

    def dump(self):
        return asdict(self)

    def eligible(self, now=None):
        now = time.time() if now is None else now
        if self.status in {"auth_required", "missing"}:
            return False
        return self.retry_at <= now

    def reject(self, reset=None, now=None, source="cli_rate_limit"):
        now = time.time() if now is None else now
        self.failures += 1
        self.status = "limited"
        self.observed_at, self.source = now, source
        if isinstance(reset, (int, float)) and reset > now:
            self.resets_at = reset
            self.retry_at = reset + 5
        else:
            self.resets_at = None
            self.retry_at = now + min(3600, 900 * 2 ** min(self.failures - 1, 2))

    def success(self, now=None):
        if self.status in {"limited", "near_limit"}:
            # A successful retry confirms access, not a new usage percentage.
            self.used_percent = None
            self.resets_at = None
            self.quota_observed_at = 0
        self.status = "available"
        self.retry_at = 0
        self.failures = 0
        self.observed_at = time.time() if now is None else now
        self.source = "successful_cli_step"

    def claude_event(self, event, threshold=90):
        if event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info", {})
            # Optional utilization is a ratio, never the context-window percentage.
            utilization = info.get("utilization")
            self.used_percent = None
            if isinstance(utilization, (int, float)) and 0 <= utilization <= 1:
                self.used_percent = utilization * 100
            self.source, self.observed_at = "claude_rate_limit_event", time.time()
            self.quota_observed_at = self.observed_at
            reset = info.get("resetsAt")
            if isinstance(reset, (int, float)):
                self.resets_at = reset
            if info.get("status") == "rejected":
                self.reject(reset, source="claude_rate_limit_event")
                return "rejected"
            if self.used_percent is not None and self.used_percent >= threshold:
                self.reject(reset, source="claude_rate_limit_event")
                self.status = "near_limit"
                return "near_limit"
        if event.get("type") == "system" and event.get("subtype") == "api_retry" and event.get("error") == "rate_limit":
            self.reject(source="claude_api_retry")
            return "rejected"
        if event.get("type") == "assistant" and event.get("error") == "rate_limit":
            self.reject(source="claude_assistant_error")
            return "rejected"

    def codex_snapshot(self, result):
        limits = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits") or {}
        windows = [limits[k] for k in ("primary", "secondary") if isinstance(limits.get(k), dict)]
        windows = [w for w in windows if isinstance(w.get("usedPercent"), (int, float))]
        if not windows:
            return
        maximum = max(w["usedPercent"] for w in windows)
        self.used_percent, self.source, self.observed_at = maximum, "codex_account_rateLimits_read", time.time()
        self.quota_observed_at = self.observed_at
        exhausted = [w for w in windows if w["usedPercent"] >= 100]
        relevant = exhausted or [w for w in windows if w["usedPercent"] == maximum]
        self.resets_at = max((w.get("resetsAt") or 0 for w in relevant), default=0) or None
        if exhausted or limits.get("rateLimitReachedType") or limits.get("spendControlReached") or result.get("ordinaryUsageAllowed") is False:
            self.reject(self.resets_at, source="codex_account_rateLimits_read")
        else:
            self.status, self.retry_at, self.failures = "available", 0, 0


def choose(usage, preferred=None, now=None):
    order = [preferred] if preferred else ["claude", "codex"]
    return next((name for name in order if usage[name].eligible(now)), None)
