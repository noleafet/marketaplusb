import { Card, CardContent } from '@/components/ui/card';
import type { ReactNode } from 'react';

export default function EmptyPanel({
  title,
  description,
  action
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <Card>
      <CardContent className='flex flex-col items-center justify-center gap-2 py-12 text-center'>
        <p className='font-medium'>{title}</p>
        {description ? (
          <p className='text-muted-foreground max-w-md text-sm'>{description}</p>
        ) : null}
        {action ? <div className='mt-2'>{action}</div> : null}
      </CardContent>
    </Card>
  );
}
