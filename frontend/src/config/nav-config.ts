import type { NavGroup } from '@/types';

export const navGroups: NavGroup[] = [
  {
    label: 'Overview',
    items: [
      {
        title: 'Dashboard',
        url: '/dashboard',
        icon: 'dashboard',
        shortcut: ['d', 'd']
      },
      {
        title: 'Coin rankings',
        url: '/dashboard/rankings',
        icon: 'chart',
        shortcut: ['r', 'r']
      },
      {
        title: 'Coin universe',
        url: '/dashboard/coins',
        icon: 'coins',
        shortcut: ['c', 'c']
      }
    ]
  },
  {
    label: 'Trading',
    items: [
      {
        title: 'Paper sessions',
        url: '/dashboard/paper',
        icon: 'flask',
        shortcut: ['p', 'p']
      },
      {
        title: 'Live safety',
        url: '/dashboard/live',
        icon: 'shield',
        shortcut: ['l', 'l']
      }
    ]
  },
  {
    label: 'Engine',
    items: [
      {
        title: 'Reports',
        url: '/dashboard/reports',
        icon: 'post',
        shortcut: ['e', 'e']
      },
      {
        title: 'Jobs',
        url: '/dashboard/jobs',
        icon: 'clock',
        shortcut: ['j', 'j']
      }
    ]
  }
];
