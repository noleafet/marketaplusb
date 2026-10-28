import { InfoSidebar } from '@/components/layout/info-sidebar';
import { InfobarProvider } from '@/components/ui/infobar';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import AdminHeader from '@/features/strategy-admin/components/admin-header';
import AdminSidebar from '@/features/strategy-admin/components/admin-sidebar';
import type { Metadata } from 'next';
import { cookies } from 'next/headers';

export const metadata: Metadata = {
  title: 'Strategy administration',
  description: 'Internal catalog of analysis, grid, backtest, and risk strategies',
  robots: {
    index: false,
    follow: false
  }
};

export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const cookieStore = await cookies();
  const defaultOpen = cookieStore.get('sidebar_state')?.value === 'true';
  return (
    <SidebarProvider defaultOpen={defaultOpen}>
      <a
        href='#main-content'
        className='bg-background ring-ring sr-only rounded-md px-3 py-2 text-sm font-medium shadow focus:not-sr-only focus:absolute focus:top-2 focus:start-2 focus:z-50 focus:ring-2'
      >
        Skip to content
      </a>
      <AdminSidebar />
      <SidebarInset id='main-content' tabIndex={-1} className='scroll-mt-16'>
        <AdminHeader />
        <InfobarProvider defaultOpen={false}>
          {children}
          <InfoSidebar side='right' />
        </InfobarProvider>
      </SidebarInset>
    </SidebarProvider>
  );
}
