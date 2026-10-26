import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import type { ReactNode } from 'react';

export default function MetricCard({
  title,
  value,
  hint,
  icon,
  valueClassName
}: {
  title: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  valueClassName?: string;
}) {
  return (
    <Card className='gap-2'>
      <CardHeader className='flex flex-row items-center justify-between pb-1'>
        <CardTitle className='text-muted-foreground text-xs font-medium tracking-wide uppercase'>
          {title}
        </CardTitle>
        {icon ? <div className='text-muted-foreground'>{icon}</div> : null}
      </CardHeader>
      <CardContent>
        <div className={cn('text-2xl font-semibold tabular-nums', valueClassName)}>{value}</div>
        {hint ? <CardDescription className='mt-1 text-xs'>{hint}</CardDescription> : null}
      </CardContent>
    </Card>
  );
}
