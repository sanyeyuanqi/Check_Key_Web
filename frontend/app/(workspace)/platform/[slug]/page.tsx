'use client';

import { notFound, useParams } from 'next/navigation';
import Dashboard from '@/app/dashboard';
import { platformBySlug } from '@/lib/platforms';

export default function PlatformPage() {
  const { slug } = useParams<{ slug: string }>();
  const platform = platformBySlug(slug);
  if (!platform) notFound();
  return <Dashboard key={platform.slug} initialProvider={platform.provider} />;
}
