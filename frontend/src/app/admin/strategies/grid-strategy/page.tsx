import PageContainer from '@/components/layout/page-container';
import CatalogSectionPage from '@/features/strategy-admin/components/catalog-section';

export const metadata = {
  title: 'Admin: Grid strategy'
};

export default function GridStrategyPage() {
  return (
    <PageContainer
      pageTitle='Grid Strategy'
      pageDescription='Ladder and range construction, adaptive overlays, rebalancing, and the risk caps that override every adjustment.'
    >
      <CatalogSectionPage sectionId='grid-strategy' />
    </PageContainer>
  );
}
