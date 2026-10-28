import { cn } from '@/lib/utils';
import type { ReactNode } from 'react';
import type { CatalogTone } from '../catalog';

export const toneText: Record<CatalogTone, string> = {
  good: 'text-emerald-600 dark:text-emerald-400',
  bad: 'text-rose-600 dark:text-rose-400',
  warn: 'text-amber-600 dark:text-amber-400',
  info: 'text-sky-600 dark:text-sky-400',
  violet: 'text-violet-600 dark:text-violet-400',
  neutral: 'text-muted-foreground'
};

export const toneBadge: Record<CatalogTone, string> = {
  good: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  bad: 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-400',
  warn: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  info: 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-400',
  violet: 'border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-400',
  neutral: 'border-border bg-muted text-muted-foreground'
};

export const toneSurface: Record<CatalogTone, string> = {
  good: 'border-emerald-500/25 bg-emerald-500/5',
  bad: 'border-rose-500/25 bg-rose-500/5',
  warn: 'border-amber-500/25 bg-amber-500/5',
  info: 'border-sky-500/25 bg-sky-500/5',
  violet: 'border-violet-500/25 bg-violet-500/5',
  neutral: 'border-border bg-card'
};

export const toneBar: Record<CatalogTone, string> = {
  good: 'bg-emerald-500',
  bad: 'bg-rose-500',
  warn: 'bg-amber-500',
  info: 'bg-sky-500',
  violet: 'bg-violet-500',
  neutral: 'bg-muted-foreground/30'
};

const toneEdge: Record<CatalogTone, string> = {
  good: 'border-l-emerald-500',
  bad: 'border-l-rose-500',
  warn: 'border-l-amber-500',
  info: 'border-l-sky-500',
  violet: 'border-l-violet-500',
  neutral: 'border-l-muted-foreground/30'
};

export const toneGradient: Record<CatalogTone, string> = {
  good: 'border-emerald-500/20 bg-gradient-to-br from-emerald-500/10 via-emerald-500/5 to-transparent',
  bad: 'border-rose-500/20 bg-gradient-to-br from-rose-500/10 via-rose-500/5 to-transparent',
  warn: 'border-amber-500/20 bg-gradient-to-br from-amber-500/10 via-amber-500/5 to-transparent',
  info: 'border-sky-500/20 bg-gradient-to-br from-sky-500/10 via-sky-500/5 to-transparent',
  violet:
    'border-violet-500/20 bg-gradient-to-br from-violet-500/10 via-violet-500/5 to-transparent',
  neutral: 'border-border bg-card'
};

export const toneTopBorder: Record<CatalogTone, string> = {
  good: 'border-t-2 border-t-emerald-500/50',
  bad: 'border-t-2 border-t-rose-500/50',
  warn: 'border-t-2 border-t-amber-500/50',
  info: 'border-t-2 border-t-sky-500/50',
  violet: 'border-t-2 border-t-violet-500/50',
  neutral: 'border-t-2 border-t-border'
};

export function ToneBadge({
  tone = 'neutral',
  className,
  children
}: {
  tone?: CatalogTone;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium whitespace-nowrap',
        toneBadge[tone],
        className
      )}
    >
      {children}
    </span>
  );
}

export function ToneNote({
  tone,
  title,
  children,
  icon
}: {
  tone: CatalogTone;
  title: ReactNode;
  children: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className={cn('flex gap-2.5 rounded-lg border border-l-4 p-3', toneSurface[tone], toneEdge[tone])}>
      {icon ? <div className={cn('mt-0.5 shrink-0', toneText[tone])}>{icon}</div> : null}
      <div className='min-w-0 space-y-0.5'>
        <p className={cn('text-sm font-medium', toneText[tone])}>{title}</p>
        <div className='text-muted-foreground text-xs leading-relaxed'>{children}</div>
      </div>
    </div>
  );
}

export function Meter({
  value,
  max = 100,
  tone = 'info',
  className,
  label
}: {
  value: number;
  max?: number;
  tone?: CatalogTone;
  className?: string;
  label?: ReactNode;
}) {
  const safeMax = max > 0 ? max : 100;
  const pct = Math.max(0, Math.min(100, (value / safeMax) * 100));
  return (
    <div className={cn('flex min-w-0 items-center gap-2', className)}>
      <div
        className='bg-muted h-1.5 min-w-0 flex-1 overflow-hidden rounded-full'
        role='progressbar'
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className={cn('h-full rounded-full', toneBar[tone])} style={{ width: `${pct}%` }} />
      </div>
      {label ? (
        <span className='text-muted-foreground w-14 shrink-0 text-right text-xs tabular-nums'>
          {label}
        </span>
      ) : null}
    </div>
  );
}

export function DistributionBar({
  segments,
  className
}: {
  segments: Array<{ label: string; value: number; tone: CatalogTone }>;
  className?: string;
}) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);
  if (!total) {
    return <div className={cn('bg-muted h-3 rounded-full', className)} />;
  }
  return (
    <div className={cn('bg-muted flex h-3 overflow-hidden rounded-full', className)}>
      {segments
        .filter((segment) => segment.value > 0)
        .map((segment) => (
          <div
            key={segment.label}
            title={`${segment.label}: ${segment.value}`}
            className={cn('h-full', toneBar[segment.tone])}
            style={{ width: `${(segment.value / total) * 100}%` }}
          />
        ))}
    </div>
  );
}

export function StatTile({
  label,
  value,
  hint,
  tone = 'neutral'
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: CatalogTone;
}) {
  return (
    <div className={cn('rounded-lg border p-2.5', toneSurface[tone])}>
      <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
        {label}
      </p>
      <p className={cn('mt-0.5 text-sm font-semibold tabular-nums', toneText[tone])}>{value}</p>
      {hint ? <p className='text-muted-foreground mt-0.5 text-[11px]'>{hint}</p> : null}
    </div>
  );
}
