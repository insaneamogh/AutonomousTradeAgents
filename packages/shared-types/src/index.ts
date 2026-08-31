// Wire-format types shared between apps/mobile and apps/api.
//
// IMPORTANT: these are the contract. When the Pydantic side changes shape,
// this file changes too — in the same PR. CI will eventually enforce this
// with a JSON-schema cross-check; for now, discipline.
//
// Convention: camelCase on the wire. Pydantic side uses `alias_generator`
// to serialize Python's snake_case fields to camelCase.

// ─────────────────────────────────────────────────────────────────────
// Veto rule labels — the one canonical copy of risk_veto_rule → human
// label. Plain JSON (not .ts) so packages/engine's Python drift test can
// `json.load()` it directly. See vetoRuleLabels.json for the identifiers
// this must cover — packages/engine/engine/risk/rules/*.py's `veto_rule=`
// literals plus live_trading_gate.py's `risk_veto_rule=`.
// ─────────────────────────────────────────────────────────────────────

import vetoRuleLabelsJson from './vetoRuleLabels.json';

export const vetoRuleLabels: Record<string, string> = vetoRuleLabelsJson;

// ─────────────────────────────────────────────────────────────────────
// Enums (string literal unions — narrower than `string`, no runtime cost)
// ─────────────────────────────────────────────────────────────────────

export type Verdict = 'STRONG_BUY' | 'BUY' | 'HOLD' | 'SELL' | 'STRONG_SELL';
export type Horizon = 'intraday' | 'short' | 'mid' | 'long';
export type Side = 'BUY' | 'SELL';
export type OrderType = 'MARKET' | 'LIMIT' | 'STOP' | 'STOP_LIMIT';
export type OrderStatus =
  | 'pending'
  | 'submitted'
  | 'accepted'
  | 'partially_filled'
  | 'filled'
  | 'rejected'
  | 'canceled'
  | 'expired';

export type AccountStatus = 'connected' | 'disconnected' | 'expiring';
export type ActivityKind = 'proposal' | 'approved' | 'declined' | 'filled' | 'vetoed' | 'hold';
export type DecisionOutcome = 'approved' | 'declined' | 'expired';
export type RiskLevel = 1 | 2 | 3 | 4 | 5;

// ─────────────────────────────────────────────────────────────────────
// /api/v1/account
// ─────────────────────────────────────────────────────────────────────

export interface AccountResponse {
  equity: number;
  cash: number;
  buyingPower: number;
  todayPnl: number;
  todayPnlPct: number;
  openPositions: number;
  status: AccountStatus;
  brokerName: string;
  isPaper: boolean;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/activity
// ─────────────────────────────────────────────────────────────────────

export interface ActivityEntryDto {
  id: string;
  kind: ActivityKind;
  symbol: string;
  /** null for "hold" — a HOLD that never became a proposal has no side. */
  side: Side | null;
  qty?: number;
  price?: number;
  verdict?: Verdict;
  headline: string;
  /** ISO 8601 string. Mobile parses with `new Date()`. */
  timestamp: string;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/approvals
// ─────────────────────────────────────────────────────────────────────

export interface ApprovalProposalDto {
  id: string;
  symbol: string;
  side: Side;
  /** "long" for a BUY-to-open, "short" for a SELL-to-open. Defaults to
   * "long" on the wire when absent (pre-short-support rows). */
  direction?: 'long' | 'short';
  /** True when `side` is a SELL that opens or extends a short position
   * (as opposed to a SELL that closes a held long). */
  opensShort?: boolean;
  qty: number;
  orderType: 'MARKET' | 'LIMIT';
  limitPrice?: number;
  estimatedNotional: number;
  /** Broker's shortable/easy-to-borrow flags for the asset. Only meaningful
   * when `opensShort` is true; null/undefined means "never verified" —
   * the risk engine treats that as a veto, not as false. */
  shortable?: boolean | null;
  easyToBorrow?: boolean | null;
  // ── Options facts (Phase A: long calls/puts only) ──────────────────
  /** True for an options proposal. `side` stays 'BUY' even for a bearish
   * ("short" `direction`) thesis — that thesis buys a PUT, it never sells
   * anything to open. Absent/false means a plain equity proposal. */
  isOption?: boolean;
  /** Always 'buy_to_open' in Phase A — no short option legs. */
  optionAction?: 'buy_to_open' | 'sell_to_close' | null;
  occSymbol?: string | null;
  strike?: number | null;
  /** ISO 8601 date string. */
  expiryDate?: string | null;
  contractType?: 'call' | 'put' | null;
  /** 100 for standard US equity options. */
  multiplier?: number;
  /** Option market snapshot at propose-time — for the UI, not re-derived
   * from `estimatedNotional`/`qty` (which are premium x qty x multiplier,
   * not a per-contract price). */
  openInterest?: number | null;
  volume?: number | null;
  bid?: number | null;
  ask?: number | null;
  impliedVolatility?: number | null;
  daysToEarnings?: number | null;
  /** Initial stop price. Derived from ATR by `engine.sizing.atr_position_size`.
   * Always null for an options proposal — Alpaca has no bracket for options. */
  stopLoss?: number;
  /** Take-profit price (entry + stop_distance × R-multiple). */
  targetPrice?: number;
  /** Exit plan: the agent closes after this many days if neither stop nor
   * target hit first. Mirrors the ghost evaluator's horizon window. */
  timeStopDays?: number;
  /** Reward:risk of the plan — (target − entry) / (entry − stop). */
  rMultiple?: number | null;
  /** Non-blocking signals from the risk engine. Known values:
   *    "wash_sale_warning"  IRS wash-sale risk on this name
   *    "sector_unknown"     sector classification missing
   * UI dispatches on the literal — don't pass free-form strings. */
  informationalFlags?: string[];
  rationale: string;
  bullCase: string;
  bearCase: string;
  riskLevel: RiskLevel;
  convictionLevel: RiskLevel;
  /** ISO 8601 string. */
  proposedAt: string;
  /** ISO 8601 string. Null/undefined means no auto-decline. */
  expiresAt?: string;
}

/** Per-position close delegation, chosen on the approval card.
 * 'agent': bracket stop/target at the broker + time-stop + signal exits.
 * 'manual': the user owns the close entirely — the agent never touches it. */
export type ExitMode = 'agent' | 'manual';

export interface DecisionRequest {
  outcome: 'approved' | 'declined';
  /** Defaults to 'agent' server-side when omitted. */
  exitMode?: ExitMode;
  /** Free-form note from the user. Stored on the AgentDecision row (Phase 1+). */
  note?: string;
}

/** Order summary returned when an approval executes server-side. */
export interface ExecutedOrderDto {
  id: string;
  proposalId: string;
  brokerOrderId?: string | null;
  clientOrderId: string;
  symbol: string;
  side: Side;
  qty: number;
  requestedQty: number;
  orderType: string;
  limitPrice?: number | null;
  status: string;
  filledQty: number;
  avgFillPrice?: number | null;
  isPaper: boolean;
  /** ISO 8601 string. */
  submittedAt: string;
}

export interface DecisionResponse {
  proposalId: string;
  outcome: DecisionOutcome;
  /** ISO 8601 string. */
  decidedAt: string;
  /** True when the approval executed server-side (order placed / filled). */
  executed?: boolean;
  order?: ExecutedOrderDto | null;
  /** True when the last-line risk re-check refused — the proposal STAYS
   * pending so the user can retry once the condition clears. */
  riskBlocked?: boolean;
  riskVetoRule?: string | null;
  riskReason?: string | null;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/watchlist
// ─────────────────────────────────────────────────────────────────────

/** "option" is Phase A only — long calls/puts, gated separately by
 * ALLOW_OPTIONS server-side (see docs/OPTIONS_PLAN.md). A preference here
 * does not itself bypass that gate. */
export type WatchlistAssetClass = 'equity' | 'option';

export interface WatchlistItemDto {
  id: string;
  symbol: string;
  assetClass: WatchlistAssetClass;
  active: boolean;
  /** ISO 8601 string. */
  createdAt: string;
}

export interface AddWatchlistRequest {
  symbol: string;
  /** Defaults to 'equity' server-side when omitted. */
  assetClass?: WatchlistAssetClass;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/positions — open agent positions + user-initiated close
// ─────────────────────────────────────────────────────────────────────

export interface OpenPositionDto {
  /** null for an unmanaged position — there is no decision to close it
   * through, so the client must not offer a close button. */
  decisionId: string | null;
  /** False when the agent did not open this position (opened directly at
   * the broker, or before this deployment's decision history). It still
   * counts against the account, so it is listed rather than hidden. */
  managed: boolean;
  /** "pending_fill": the order was placed but hasn't filled yet (common
   * outside market hours) — no entry price, no live mark, nothing to
   * close yet, only an order working at the broker. "open": a real,
   * filled position. Unmanaged rows are always "open". Value stays
   * snake_case on the wire — Pydantic's camelCase alias generator only
   * renames JSON keys, not Literal string values. */
  status: 'open' | 'pending_fill';
  symbol: string;
  side: Side;
  /** "long" or "short" — derived server-side from the entry proposal's
   * direction (or `side === 'SELL'` as a fallback for older rows). Always
   * populated by the backend. */
  direction: 'long' | 'short';
  qty: number;
  avgEntryPrice: number | null;
  /** Live mark from the latest reconciler snapshot, when available. */
  lastPrice: number | null;
  unrealizedPnl: number | null;
  exitMode: ExitMode;
  stopLoss: number | null;
  targetPrice: number | null;
  timeStopDays: number | null;
  /** ISO 8601 string. */
  openedAt: string;
  // ── Options facts (Phase A) — all optional/absent for an equity
  // position. Mirrors packages/broker/broker/types.py's Position.is_option
  // /.multiplier; contractType/strike/expiryDate/occSymbol are derived
  // server-side from the OCC symbol (never parsed client-side). NOT YET
  // populated by /positions for a real broker position as of this
  // widening — the service that builds this dto is a separate track's
  // scope (see the options build notes) — these fields exist so the UI
  // and the wire contract are ready the moment that track wires them.
  isOption?: boolean;
  contractType?: 'call' | 'put' | null;
  strike?: number | null;
  /** ISO 8601 date string. */
  expiryDate?: string | null;
  occSymbol?: string | null;
  /** 100 for standard US equity options, 1 (or absent) for equity. */
  multiplier?: number;
}

export interface ClosePositionResponse {
  decisionId: string;
  closed: boolean;
  /** null on success; otherwise not_found / already_closed / close_in_flight / risk_vetoed / … */
  error: string | null;
  detail: string | null;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/circuit-breaker — drawdown halt banner
// ─────────────────────────────────────────────────────────────────────

export interface CircuitBreakerResponse {
  halted: boolean;
  reason: string | null;
  /** ISO 8601 string. */
  haltedAt: string | null;
  observedDrawdownPct: number | null;
  thresholdPct: number | null;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/agent/run
// ─────────────────────────────────────────────────────────────────────

export interface AgentRunRequest {
  symbol: string;
  horizon?: Horizon;
  /** Phase A options trading — still gated by ALLOW_OPTIONS on the agent
   * side; requesting 'option' does nothing unless that flag is also on. */
  instrumentPreference?: 'equity' | 'option' | null;
}

export interface AgentRunResponse {
  /** Null when the council holds or risk vetoes. */
  proposal: ApprovalProposalDto | null;
  finalAction: 'BUY' | 'SELL' | 'HOLD' | 'VETOED';
  riskApproved: boolean;
  riskReason: string;
  riskVetoRule?: string | null;
  regime?: string | null;
  /** True if the LLM ran in mock mode (no ANTHROPIC_API_KEY set). */
  llmMock: boolean;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/agent/run/start + /run/{id}/progress — council theater
// ─────────────────────────────────────────────────────────────────────

export type CouncilNode =
  | 'router'
  | 'technical'
  | 'fundamental'
  | 'macro'
  | 'selector'
  | 'drafter'
  | 'risk_officer';

export interface AgentRunStartResponse {
  runId: string;
  symbol: string;
}

export interface CouncilProgressEvent {
  seq: number;
  node: CouncilNode;
  status: 'started' | 'completed' | 'skipped';
  /** ISO 8601 string. */
  at: string;
  /** Deterministic per-node summary. Shape varies by node — analysts carry
   * {score, confidence, thesis}; risk_officer carries {approved, vetoRule, thesis}. */
  summary: Record<string, unknown> | null;
}

export interface CouncilProgressResponse {
  runId: string;
  status: 'running' | 'completed' | 'failed';
  events: CouncilProgressEvent[];
  result: AgentRunResponse | null;
  error?: string | null;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/decisions/{id}/timeline — trade biography
// ─────────────────────────────────────────────────────────────────────

export interface TimelineEventDto {
  kind:
    | 'proposed'
    | 'risk_verdict'
    | 'user_decision'
    | 'order_submitted'
    | 'filled'
    | 'closed'
    | 'review_grade'
    | 'reflection'
    | 'ghost';
  /** ISO 8601 string, null when the source row had no timestamp. */
  at: string | null;
  title: string;
  detail: string;
  data: Record<string, unknown>;
}

export interface DecisionTimelineResponse {
  decisionId: string;
  symbol: string;
  side: string | null;
  status: 'pending' | 'approved' | 'declined' | 'expired' | 'vetoed' | 'closed';
  events: TimelineEventDto[];
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/ghost/summary + /api/v1/risk/vetoes — regret analytics
// ─────────────────────────────────────────────────────────────────────

export interface GhostBucketDto {
  count: number;
  ghostPnl: number;
  pendingCount: number;
}

export interface GhostSummaryResponse {
  windowDays: number;
  asOf: string;
  vetoed: GhostBucketDto;
  declined: GhostBucketDto;
  /** What finalized vetoed picks would have LOST (>=0). */
  savedUsd: number;
  /** What finalized declined/expired picks would have MADE (>=0). */
  missedUsd: number;
}

export interface VetoRuleDto {
  rule: string;
  count: number;
  blockedNotional: number;
  ghostPnl?: number | null;
  preventedLossUsd?: number | null;
  lastAt?: string | null;
}

/** A rule that resized a trade rather than blocking it — kept separate
 * from `VetoRuleDto`: a trim let a smaller trade through, a veto let
 * nothing through. */
export interface TrimRuleDto {
  rule: string;
  count: number;
}

export interface VetoLedgerResponse {
  windowDays: number;
  totalVetoes: number;
  totalBlockedNotional: number;
  rules: VetoRuleDto[];
  trims: TrimRuleDto[];
  totalTrims: number;
  /** `reasoning["risk_profile"]` in force over this window — not yet sent
   * by the API as of this writing (backend tracked separately), so this is
   * optional/absent rather than a value the client can rely on. */
  riskProfile?: string | null;
}

/**
 * GET /api/v1/risk/vetoes/{rule}/exemplar — the single most extreme
 * finalized refusal under one rule (docs/IMPL_REFUSAL_LEDGER.md §2.2).
 * NOT YET BUILT server-side as of this writing — shape documented here
 * ahead of that endpoint landing, mirroring the doc's own worked example
 * (the NVDA260918C00225000 / max_premium_pct story trade).
 */
export interface VetoExemplarResponse {
  rule: string;
  /** False when every ghost under this rule in the window is still
   * pending/partial — there is no finalized exemplar to show yet. */
  found: boolean;
  decisionId?: string | null;
  symbol?: string | null;
  occSymbol?: string | null;
  side?: string | null;
  qty?: number | null;
  price?: number | null;
  estimatedNotional?: number | null;
  notionalPctOfEquity?: number | null;
  capPct?: number | null;
  bullCase?: string | null;
  bearCase?: string | null;
  rationale?: string | null;
  markPrice?: number | null;
  tradingDaysElapsed?: number | null;
  ghostPnl?: number | null;
  preventedLossUsd?: number | null;
  /** ISO 8601 string. */
  triggeredAt?: string | null;
  /** ISO 8601 string. */
  finalizedAt?: string | null;
  riskProfile?: string | null;
  /** 'ask' or 'auto' — see `DecisionSummaryDto.approvalMode`. */
  approvalMode?: string | null;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/insights/funnel — the contract funnel
// ─────────────────────────────────────────────────────────────────────

export interface FunnelStageDto {
  key: string;
  label: string;
  survivors: number;
  /** previous stage's survivors minus this stage's — never negative. */
  dropped: number;
}

export interface FunnelRunDto {
  decisionId: string;
  symbol: string;
  /** ISO 8601 string. */
  triggeredAt: string;
  stages: FunnelStageDto[];
  rejectionReason: string | null;
  /** Which stage's count hit zero — null when the run bought a contract. */
  rejectionStage: string | null;
  selectedOcc: string | null;
  outcome: 'bought' | 'held';
}

export interface FunnelAggregateDto {
  /** Summed across the window — the headline number. */
  stages: FunnelStageDto[];
  runs: number;
  bought: number;
  topRejectionReasons: Array<{ reason: string; count: number }>;
}

export interface FunnelResponse {
  windowDays: number;
  aggregate: FunnelAggregateDto;
  recent: FunnelRunDto[];
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/review/scorecard — calibration scorecard
// ─────────────────────────────────────────────────────────────────────

export interface ScorecardMonth {
  /** YYYY-MM of reviewed_at. */
  month: string;
  totalReviewed: number;
  agreementPct: number;
}

export interface OverrideStats {
  count: number;
  operatorWins: number;
  reflectionWins: number;
  operatorWinRatePct: number;
}

export interface ScorecardResponse {
  windowDays: number;
  agreementPct: number;
  months: ScorecardMonth[];
  overrides: OverrideStats;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/decisions — the browsable decision list
// ─────────────────────────────────────────────────────────────────────

export interface DecisionSummaryDto {
  id: string;
  symbol: string;
  finalAction: string;
  /** ISO 8601 string. */
  triggeredAt: string;
  riskApproved: boolean;
  riskVetoRule: string | null;
  selectedStrategy: string | null;
  selectorConfidence: number;
  selectorRationale: string;
  regime: string | null;
  analystSubset: string[] | null;
  userResponse: string | null;
  /** 'ask' (human-approved, the default) or 'auto' (the auto-approve
   * sweeper executed it with no human in the loop). Not present on
   * ApprovalProposalDto — a still-pending proposal has never been
   * decided, so it can only ever be 'ask' there. */
  approvalMode: string;
}

export interface DecisionListResponse {
  decisions: DecisionSummaryDto[];
  total: number;
  limit: number;
  offset: number;
}
