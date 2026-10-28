import PageContainer from '@/components/layout/page-container';
import CatalogSectionPage from '@/features/strategy-admin/components/catalog-section';

export const metadata = {
  title: 'Admin: Backtesting'
};

export default function BacktestingPage() {
  return (
    <PageContainer
      pageTitle='Backtesting & Validation'
      pageDescription='Walk-forward design, the 96-candidate search space, and the paper-to-live ladder every strategy must clear.'
    >
      <CatalogSectionPage sectionId='backtesting-validation' />
    </PageContainer>
  );
}
