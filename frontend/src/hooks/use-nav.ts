'use client';

import { useMemo } from 'react';
import type { NavGroup, NavItem } from '@/types';

export function useFilteredNavItems(items: NavItem[]) {
  return items;
}

export function useFilteredNavGroups(groups: NavGroup[]) {
  return useMemo(() => groups, [groups]);
}
