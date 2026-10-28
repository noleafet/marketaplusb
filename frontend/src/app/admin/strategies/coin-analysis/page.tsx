import PageContainer from '@/components/layout/page-container';
import CatalogSectionPage from '@/features/strategy-admin/components/catalog-section';

export const metadata = {
  title: 'Admin: Coin analysis'
};

export default function CoinAnalysisPage() {
  return (
    <PageContainer
      pageTitle='Coin Analysis'
      pageDescription='Eligibility gates, ranking factors, regime robustness, and the diagnostics that decide which coins survive.'
    >
      <CatalogSectionPage sectionId='coin-analysis' />
    </PageContainer>
  );
}
