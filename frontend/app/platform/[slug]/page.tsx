'use client';

import { notFound, useParams } from 'next/navigation';
import AuthenticatedShell from '@/app/authenticated-shell';
import Dashboard from '@/app/dashboard';
import { platformBySlug } from '@/lib/platforms';

export default function PlatformPage() {
  const { slug } = useParams<{ slug: string }>();
  const platform = platformBySlug(slug);
  if (!platform) notFound();
  return <AuthenticatedShell activeItem={platform.slug}>{() => <Dashboard initialProvider={platform.provider} />}</AuthenticatedShell>;
}
