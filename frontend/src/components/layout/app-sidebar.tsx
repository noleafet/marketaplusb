'use client';
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
import { Badge } from '@/components/ui/badge';
import { navGroups } from '@/config/nav-config';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Icons } from '../icons';

export default function AppSidebar() {
  const pathname = usePathname();

  return (
    <Sidebar collapsible='icon'>
      <SidebarHeader className='group-data-[collapsible=icon]:pt-4'>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              size='lg'
              tooltip='Grid Engine'
              render={<Link href='/dashboard' aria-label='Grid Engine' />}
            >
              <div className='bg-sidebar-primary text-sidebar-primary-foreground flex aspect-square size-8 shrink-0 items-center justify-center rounded-lg'>
                <Icons.grid className='size-4' />
              </div>
              <div className='grid flex-1 text-left text-sm leading-tight group-data-[collapsible=icon]:hidden'>
                <span className='truncate font-semibold'>Grid Engine</span>
                <span className='text-muted-foreground truncate text-xs'>
                  USDT-M futures research
                </span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent className='overflow-x-hidden'>
        {navGroups.map((group) => (
          <SidebarGroup key={group.label} className='py-0'>
            <SidebarGroupLabel>{group.label}</SidebarGroupLabel>
            <SidebarMenu>
              {group.items.map((item) => {
                const Icon = item.icon ? Icons[item.icon] : Icons.logo;
                const isActive =
                  pathname === item.url ||
                  (item.url !== '/dashboard' && pathname.startsWith(`${item.url}/`));
                return (
                  <SidebarMenuItem key={item.title}>
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
        ))}
      </SidebarContent>
      <SidebarFooter>
        <div className='text-muted-foreground px-2 pb-2 text-xs group-data-[collapsible=icon]:hidden'>
          <Badge variant='outline' className='mb-2'>
            Paper mode only
          </Badge>
          <p>Live orders are gated and disabled by default.</p>
        </div>
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}
