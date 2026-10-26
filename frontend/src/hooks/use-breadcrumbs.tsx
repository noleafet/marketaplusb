'use client';

import { usePathname } from 'next/navigation';
import { useMemo } from 'react';

type BreadcrumbItem = {
  title: string;
  link: string;
};

const routeMapping: Record<string, BreadcrumbItem[]> = {
  '/dashboard': [{ title: 'Overview', link: '/dashboard' }],
  '/dashboard/rankings': [
    { title: 'Dashboard', link: '/dashboard' },
    { title: 'Coin rankings', link: '/dashboard/rankings' }
  ],
  '/dashboard/coins': [
    { title: 'Dashboard', link: '/dashboard' },
    { title: 'Coin universe', link: '/dashboard/coins' }
  ],
  '/dashboard/paper': [
    { title: 'Dashboard', link: '/dashboard' },
    { title: 'Paper sessions', link: '/dashboard/paper' }
  ],
  '/dashboard/live': [
    { title: 'Dashboard', link: '/dashboard' },
    { title: 'Live safety', link: '/dashboard/live' }
  ],
  '/dashboard/reports': [
    { title: 'Dashboard', link: '/dashboard' },
    { title: 'Reports', link: '/dashboard/reports' }
  ],
  '/dashboard/jobs': [
    { title: 'Dashboard', link: '/dashboard' },
    { title: 'Jobs', link: '/dashboard/jobs' }
  ]
};

export function useBreadcrumbs() {
  const pathname = usePathname();

  return useMemo(() => {
    if (routeMapping[pathname]) {
      return routeMapping[pathname];
    }

    if (pathname.startsWith('/dashboard/coin/')) {
      const symbol = pathname.split('/').pop()?.toUpperCase() ?? '';
      return [
        { title: 'Dashboard', link: '/dashboard' },
        { title: 'Coin universe', link: '/dashboard/coins' },
        { title: symbol, link: pathname }
      ];
    }

    if (pathname.startsWith('/dashboard/paper/')) {
      return [
        { title: 'Dashboard', link: '/dashboard' },
        { title: 'Paper sessions', link: '/dashboard/paper' },
        { title: 'Session', link: pathname }
      ];
    }

    const segments = pathname.split('/').filter(Boolean);
    return segments.map((segment, index) => ({
      title: segment.charAt(0).toUpperCase() + segment.slice(1),
      link: `/${segments.slice(0, index + 1).join('/')}`
    }));
  }, [pathname]);
}
