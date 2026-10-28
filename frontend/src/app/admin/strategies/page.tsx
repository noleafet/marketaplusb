import PageContainer from '@/components/layout/page-container';
import { Badge } from '@/components/ui/badge';
import StrategiesView from '@/features/strategy-admin/components/strategies-view';

export const metadata = {
  title: 'Admin: Strategies'
};

export default function StrategiesPage() {
  return (
    <PageContainer
      pageTitle='Strategies & metrics'
      pageDescription='Every analysis, grid, backtest, and risk strategy the engine runs, plus the parameters selected per coin.'
      pageHeaderAction={
        <Badge variant='outline' className='text-muted-foreground'>
          Internal · unlinked
        </Badge>
      }
    >
      <StrategiesView />
    </PageContainer>
  );
}
