'use client';

import { Icons } from '@/components/icons';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { relativeTime } from '@/features/grid/api';
import { useMarketRegime } from '@/features/grid/queries';
import { REGIME_EFFECTS, regimeReasonLabel } from '@/features/grid/regime';
import { cn } from '@/lib/utils';
import Link from 'next/link';

const PHASE_STYLES = {
  warning: {
    title: 'BTC regime: warning',
    className:
      'border-amber-500/40 bg-amber-500/10 text-amber-900 dark:text-amber-200 [&>svg]:text-amber-600'
  },
  bearish: {
    title: 'BTC regime: bearish',
    className:
      'border-rose-500/40 bg-rose-500/10 text-rose-900 dark:text-rose-200 [&>svg]:text-rose-600'
  }
} as const;

/** Shown on every page that trades while the BTC regime restricts the book. */
export default function RegimeBanner() {
  const { data } = useMarketRegime();
  if (!data || !data.enabled || data.phase === 'normal') return null;

  const style = PHASE_STYLES[data.phase];
  const reasons = data.reasons.map(regimeReasonLabel).join(' · ');

  return (
    <Alert className={cn(style.className)}>
      {data.phase === 'bearish' ? <Icons.trendingDown /> : <Icons.warning />}
      <AlertTitle>{style.title}</AlertTitle>
      <AlertDescription className='text-current/80'>
        <p>
          {REGIME_EFFECTS[data.phase]}
          {reasons ? ` Triggered by: ${reasons}` : ''}
          {data.entered_at ? ` (since ${relativeTime(data.entered_at)}).` : '.'}
        </p>
        <Link href='/admin/strategies' className='font-medium underline underline-offset-2'>
          View BTC regime details
        </Link>
      </AlertDescription>
    </Alert>
  );
}
