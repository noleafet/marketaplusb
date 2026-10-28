import { Icons } from '@/components/icons';
import { ThemeModeToggle } from '@/components/themes/theme-mode-toggle';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { SidebarTrigger } from '@/components/ui/sidebar';
import Link from 'next/link';

export default function AdminHeader() {
  return (
    <header className='bg-background/60 sticky top-0 z-20 flex h-16 shrink-0 items-center justify-between gap-2 backdrop-blur-md md:h-14'>
      <div className='flex items-center gap-2 px-4'>
        <SidebarTrigger className='-ml-1' />
        <Separator orientation='vertical' className='mr-2 h-4 data-vertical:self-center' />
        <div className='leading-tight'>
          <p className='text-sm font-semibold'>Strategy administration</p>
          <p className='text-muted-foreground text-xs'>
            Internal reference · not in dashboard navigation
          </p>
        </div>
      </div>

      <div className='flex items-center gap-2 px-4'>
        <ThemeModeToggle />
        <Button
          variant='outline'
          size='sm'
          nativeButton={false}
          render={<Link href='/dashboard' aria-label='Back to dashboard' />}
        >
          <Icons.dashboard data-icon='inline-start' />
          <span className='hidden sm:inline'>Dashboard</span>
        </Button>
      </div>
    </header>
  );
}
