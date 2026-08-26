/**
 * /api/v1/scanner/status hook — drives the Scanner card/tile on both
 * dashboards.
 *
 * Mirrors `useHealthFull.ts` exactly in style: local TS interfaces (not
 * `@app/shared-types`) because this is a single-purpose monitoring hook,
 * same as health. Polls every 30s; the endpoint just reads an in-memory
 * scheduler singleton, so the cadence costs nothing.
 */

import { useQuery } from '@tanstack/react-query';

import { request } from '@/lib/api';
import { useAuthStore } from '@/stores/authStore';

export type ScanDirection = 'bullish' | 'bearish';

export interface ScanSignalDto {
  symbol: string;
  rule: string;
  direction: ScanDirection;
  strength: number;
  detail: string;
  observedAt: string;
  context: Record<string, number | null>;
}

export interface ScannerStatusResponse {
  schedulerEnabled: boolean;
  scannerEnabledFlag: boolean;
  triggerLoopArmed: boolean;
  marketOpen: boolean | null;
  lastScanAt: string | null;
  scanIntervalMinutes: number | null;
  maxCouncilRunsPerScan: number | null;
  watchlistSize: number;
  signals: ScanSignalDto[];
  triggeredSymbols: string[];
  suppressedCount: number;
  lastCouncilRunAt: string | null;
  lastCouncilRunSymbols: string[];
  generatedAt: string;
}

export const scannerStatusKey = (userId: string | null | undefined) =>
  ['scanner', 'status', userId ?? 'anon'] as const;

export function useScannerStatus() {
  const userId = useAuthStore((s) => s.user?.userId ?? null);
  const isAuthed = useAuthStore((s) => s.status === 'authenticated');

  return useQuery({
    queryKey: scannerStatusKey(userId),
    queryFn: () => request<ScannerStatusResponse>('/api/v1/scanner/status'),
    enabled: isAuthed,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}
