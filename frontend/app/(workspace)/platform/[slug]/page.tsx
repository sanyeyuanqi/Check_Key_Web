'use client';

import { notFound, usePathname } from 'next/navigation';
import Dashboard from '@/app/dashboard';
import { platformBySlug } from '@/lib/platforms';

export default function PlatformPage() {
  const slug = usePathname().split('/')[2];
  const platform = platformBySlug(slug);
  if (!platform) notFound();
  return <Dashboard key={platform.slug} initialProvider={platform.provider} />;
}
