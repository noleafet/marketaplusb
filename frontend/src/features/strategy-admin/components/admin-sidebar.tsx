'use client';

import { Icons } from '@/components/icons';
import { Badge } from '@/components/ui/badge';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail
} from '@/components/ui/sidebar';
import { COIN_SETTINGS_PATH, SECTION_PATHS } from '@/features/strategy-admin/catalog';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

const NAV_ITEMS: Array<{ title: string; url: string; icon: keyof typeof Icons }> = [
  { title: 'Overview', url: '/admin/strategies', icon: 'flask' },
  { title: 'Coin settings', url: COIN_SETTINGS_PATH, icon: 'settings' },
  { title: 'Coin Analysis', url: SECTION_PATHS['coin-analysis'], icon: 'activity' },
  { title: 'Grid Strategy', url: SECTION_PATHS['grid-strategy'], icon: 'grid' },
  { title: 'Backtesting', url: SECTION_PATHS['backtesting-validation'], icon: 'chart' }
];

export default function AdminSidebar() {
  const pathname = usePathname();

  return (
    <Sidebar collapsible='icon'>
      <SidebarHeader className='group-data-[collapsible=icon]:pt-4'>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              size='lg'
              tooltip='Strategy administration'
              render={<Link href='/admin/strategies' aria-label='Strategy administration' />}
            >
              <div className='bg-sidebar-primary text-sidebar-primary-foreground flex aspect-square size-8 shrink-0 items-center justify-center rounded-lg'>
                <Icons.flask className='size-4' />
              </div>
              <div className='grid flex-1 text-left text-sm leading-tight group-data-[collapsible=icon]:hidden'>
                <span className='truncate font-semibold'>Strategy admin</span>
                <span className='text-muted-foreground truncate text-xs'>Internal reference</span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent className='overflow-x-hidden'>
        <SidebarGroup className='py-0'>
          <SidebarGroupLabel>Strategy sections</SidebarGroupLabel>
          <SidebarMenu>
            {NAV_ITEMS.map((item) => {
              const Icon = Icons[item.icon];
              const isActive =
                item.url === '/admin/strategies'
                  ? pathname === item.url
                  : pathname === item.url || pathname.startsWith(`${item.url}/`);
              return (
                <SidebarMenuItem key={item.url}>
                  <SidebarMenuButton
                    render={<Link href={item.url} aria-label={item.title} />}
                    tooltip={item.title}
                    isActive={isActive}
                  >
                    <Icon />
                    <span>{item.title}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              );
            })}
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter>
        <div className='text-muted-foreground space-y-1 px-2 pb-2 text-xs group-data-[collapsible=icon]:hidden'>
          <Badge variant='outline'>Internal docs</Badge>
          <p>Not linked from the dashboard navigation. Use the header link to return.</p>
        </div>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}
