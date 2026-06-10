// Wire-format types shared between apps/mobile and apps/api.
//
// IMPORTANT: these are the contract. When the Pydantic side changes shape,
// this file changes too — in the same PR. CI will eventually enforce this
// with a JSON-schema cross-check; for now, discipline.
//
// Convention: camelCase on the wire. Pydantic side uses `alias_generator`
// to serialize Python's snake_case fields to camelCase.

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
export type ActivityKind = 'proposal' | 'approved' | 'declined' | 'filled' | 'vetoed';
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
  side: Side;
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
  qty: number;
  orderType: 'MARKET' | 'LIMIT';
  limitPrice?: number;
  estimatedNotional: number;
  /** Initial stop price. Derived from ATR by `engine.sizing.atr_position_size`. */
  stopLoss?: number;
  /** Take-profit price (entry + stop_distance × R-multiple). */
  targetPrice?: number;
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

export interface DecisionRequest {
  outcome: 'approved' | 'declined';
  /** Free-form note from the user. Stored on the AgentDecision row (Phase 1+). */
  note?: string;
}

export interface DecisionResponse {
  proposalId: string;
  outcome: DecisionOutcome;
  /** ISO 8601 string. */
  decidedAt: string;
}

// ─────────────────────────────────────────────────────────────────────
// /api/v1/agent/run
// ─────────────────────────────────────────────────────────────────────

export interface AgentRunRequest {
  symbol: string;
  horizon?: Horizon;
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

export interface VetoLedgerResponse {
  windowDays: number;
  totalVetoes: number;
  totalBlockedNotional: number;
  rules: VetoRuleDto[];
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
