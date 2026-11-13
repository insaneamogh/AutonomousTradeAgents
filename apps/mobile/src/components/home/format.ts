export type Currency = 'USD' | 'INR';

/** `$1,204` / `₹1,20,450` in the amount's own currency. Never hard-code `$`. */
export function formatMoney(value: number, currency: Currency = 'USD', fractionDigits = 0): string {
  return value.toLocaleString(currency === 'INR' ? 'en-IN' : 'en-US', {
    style: 'currency',
    currency,
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

/** "in 12m", "in 2h 5m", or "due now" for a past/near instant. */
export function formatCountdown(targetIso: string, now: number = Date.now()): string | null {
  const t = Date.parse(targetIso);
  if (Number.isNaN(t)) return null;
  const mins = Math.ceil((t - now) / 60_000);
  if (mins <= 0) return 'due now';
  if (mins < 60) return `in ${mins}m`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m === 0 ? `in ${h}h` : `in ${h}h ${m}m`;
}
