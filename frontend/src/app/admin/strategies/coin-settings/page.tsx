import PageContainer from '@/components/layout/page-container';
import CoinSettingsView from '@/features/strategy-admin/components/coin-settings-view';

export const metadata = {
  title: 'Admin: Coin settings'
};

export default function CoinSettingsPage() {
  return (
    <PageContainer
      pageTitle='Coin-specific settings'
      pageDescription='Thresholds from the latest completed run and the ladder, range, overlay, and rebalancing parameters selected for each coin.'
    >
      <CoinSettingsView />
    </PageContainer>
  );
}
