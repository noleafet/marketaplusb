import PageContainer from '@/components/layout/page-container';
import { Badge } from '@/components/ui/badge';
import StrategyOverview from '@/features/strategy-admin/components/strategy-overview';

export const metadata = {
  title: 'Admin: Strategies'
};

export default function StrategiesPage() {
  return (
    <PageContainer
      pageTitle='Strategies & metrics'
      pageDescription='Summary of every analysis, grid, backtest, and risk strategy the engine runs.'
      pageHeaderAction={
        <Badge variant='outline' className='text-muted-foreground'>
          Internal
        </Badge>
      }
    >
      <StrategyOverview />
    </PageContainer>
  );
}
