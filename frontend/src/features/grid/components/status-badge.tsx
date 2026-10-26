import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

const variants: Record<string, { label: string; className: string }> = {
  prime: {
    label: 'Prime',
    className: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
  },
  viable: {
    label: 'Viable',
    className: 'border-sky-500/30 bg-sky-500/10 text-sky-600 dark:text-sky-400'
  },
  watch: {
    label: 'Watch',
    className: 'border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400'
  },
  avoid: {
    label: 'Avoid',
    className: 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
  },
  ineligible: {
    label: 'Ineligible',
    className: 'border-muted-foreground/20 bg-muted text-muted-foreground'
  },
  active: {
    label: 'Active',
    className: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
  },
  paused: {
    label: 'Paused',
    className: 'border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400'
  },
  stopped: {
    label: 'Stopped',
    className: 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
  },
  queued: {
    label: 'Queued',
    className: 'border-sky-500/30 bg-sky-500/10 text-sky-600 dark:text-sky-400'
  },
  running: {
    label: 'Running',
    className: 'border-violet-500/30 bg-violet-500/10 text-violet-600 dark:text-violet-400'
  },
  succeeded: {
    label: 'Succeeded',
    className: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
  },
  failed: {
    label: 'Failed',
    className: 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
  },
  cancelled: {
    label: 'Cancelled',
    className: 'border-muted-foreground/20 bg-muted text-muted-foreground'
  },
  pending: {
    label: 'Pending',
    className: 'border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400'
  },
  approved: {
    label: 'Approved',
    className: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
  },
  revoked: {
    label: 'Revoked',
    className: 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
  },
  rejected: {
    label: 'Rejected',
    className: 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
  },
  ranking: {
    label: 'Ranking',
    className: 'border-sky-500/30 bg-sky-500/10 text-sky-600 dark:text-sky-400'
  },
  performance: {
    label: 'Performance',
    className: 'border-violet-500/30 bg-violet-500/10 text-violet-600 dark:text-violet-400'
  },
  risk: {
    label: 'Risk',
    className: 'border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400'
  },
  methodology: {
    label: 'Methodology',
    className: 'border-muted-foreground/20 bg-muted text-muted-foreground'
  }
};

export default function StatusBadge({
  value,
  className
}: {
  value: string | null | undefined;
  className?: string;
}) {
  if (!value) return null;
  const config = variants[value] ?? {
    label: value.replace(/_/g, ' '),
    className: ''
  };
  return (
    <Badge variant='outline' className={cn(config.className, 'capitalize', className)}>
      {config.label}
    </Badge>
  );
}
